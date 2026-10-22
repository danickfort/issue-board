"""Jira Board: the REST client, ADF rendering, credentials bound to their site, the board app and the launcher."""
import asyncio
import base64
import getpass
import io
import json
import logging
import os
import sys
from functools import partial

import httpx
import keyring.errors
import pytest
from rich.console import Console
from textual.widgets import Input, Select

from issueboard import app as issueboard
from issueboard.prefs import Prefs, default_path, write_json
from issueboard.widgets import Column, CommentDialog, More
from jira_board import credentials
from jira_board.app import BacklogScreen, Card, JiraApp, main
from jira_board.client import Client, JiraError, adf_paragraphs, check_url, group_issues, required_fields
from jira_board.credentials import LEGACY_SITE, SetupError, config_path
from jira_board.demo import DemoClient, demo_state
from jira_board.markup import adf_markdown, render

SITE = 'https://example.atlassian.net'
REAL_KEYCHAIN_ITEM = credentials.keychain_item  # before the conftest guard replaces it


async def settle(app, pilot):
    for _ in range(4):
        await pilot.pause()
        await app.workers.wait_for_complete()
    await pilot.pause()


def client_for(respond, url=SITE):
    return Client(url, 'me@example.com', 'secret-token', transport=httpx.MockTransport(respond))


def board_app(client=None, visited=None):
    client = client or DemoClient()
    return JiraApp(client, Prefs(None, client.base_url, {'visited': visited} if visited else demo_state()))


def shown(text_renderable, width=80):
    console = Console(width=width, record=True, color_system=None, file=io.StringIO())
    console.print(text_renderable)
    return console.export_text()


# Client

def test_only_a_bare_https_site_address_is_accepted():
    check_url('https://example.atlassian.net/')
    for bad in ('http://example.atlassian.net', 'https://example.atlassian.net/jira/boards/1', 'https://u:p@x.net',
                'https://x.net?a=1', 'not a url'):
        with pytest.raises(JiraError):
            check_url(bad)


async def test_requests_use_basic_auth_follow_every_page_and_keep_the_rank_order():
    requests = []

    def respond(request):
        requests.append(request)
        path, params = request.url.path, request.url.params
        if path == '/rest/agile/1.0/board':
            start = int(params['startAt'])
            return httpx.Response(200, json={'values': [{'id': start + 1}, {'id': start + 2}][:3 - start],
                                             'isLast': start >= 2, 'total': 3})
        if path.endswith('/issue'):
            token = params.get('nextPageToken')
            issues = {None: ['A-3', 'A-1'], 't1': ['A-2']}[token]
            return httpx.Response(200, json={'issues': [{'id': k[-1], 'key': k, 'fields': {'status': {'id': 1}}}
                                                        for k in issues], 'nextPageToken': 't1', 'isLast': token == 't1'})
        raise AssertionError(path)

    client = client_for(respond)
    try:
        assert [b['id'] for b in await client.boards('WEB')] == [1, 2, 3]
        assert requests[0].url.params['projectKeyOrId'] == 'WEB' and requests[1].url.params['startAt'] == '2'
        expected = base64.b64encode(b'me@example.com:secret-token').decode()
        assert requests[0].headers['Authorization'] == f'Basic {expected}'
        board = {'id': 7, 'config': {'subQuery': {'query': 'fixVersion is EMPTY'}}}
        assert [i['key'] for i in await client.issues(board)] == ['A-3', 'A-1', 'A-2']
        assert requests[-1].url.path == '/rest/software/1.0/board/7/issue'
        assert requests[-1].url.params['jql'] == 'fixVersion is EMPTY'  # the kanban sub-filter, as Jira wrote it
        assert requests[-1].url.params['nextPageToken'] == 't1'
        await client.issues(board, {'id': 66})
        assert requests[-1].url.path == '/rest/software/1.0/board/7/sprint/66/issue' and 'jql' not in requests[-1].url.params
    finally:
        await client.close()


async def test_redirects_errors_and_rate_limits_never_leak_or_repeat_writes():
    calls = []

    def respond(request):
        calls.append(request.method)
        if request.url.path == '/rest/api/3/myself':
            return httpx.Response(302, headers={'Location': 'https://evil.example/login'})
        if request.url.path.endswith('/transitions'):
            return httpx.Response(429, headers={'Retry-After': '0'})
        if request.url.path == '/rest/agile/1.0/board':
            if len(calls) < 4:
                return httpx.Response(429, headers={'Retry-After': '0'})
            return httpx.Response(200, json={'values': [], 'isLast': True})
        return httpx.Response(400, json={'errorMessages': ['Bad secret-token here'], 'errors': {'resolution': 'required'}})

    client = client_for(respond)
    try:
        with pytest.raises(JiraError, match='evil.example; the request was not followed'):
            await client.me()
        assert await client.boards() == [] and calls[1:4] == ['GET'] * 3  # two retries of a read
        calls.clear()
        with pytest.raises(JiraError, match='429') as error:
            await client.transition({'key': 'A-1'}, '41')
        assert calls == ['POST'] and error.value.status == 429  # a write is never repeated
        with pytest.raises(JiraError) as error:
            await client.assign({'key': 'A-1'}, 'acc')
        assert 'resolution: required' in str(error.value) and 'secret-token' not in str(error.value)
    finally:
        await client.close()
    offline = client_for(lambda request: (_ for _ in ()).throw(httpx.ConnectError('down')))
    with pytest.raises(JiraError, match='Cannot reach Jira at example.atlassian.net') as error:
        await offline.me()
    assert error.value.status is None
    await offline.close()


async def test_writes_send_documented_payloads_and_sprints_are_optional():
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith('/sprint'):
            return httpx.Response(400, json={'errorMessages': ['The board does not support sprints']})
        if request.url.path.endswith('/assignable/search'):
            start = int(request.url.params.get('startAt', 0))
            return httpx.Response(200, json=[{'accountId': 'a1', 'displayName': 'A'}, {'accountId': 'x', 'active': False}]
                                  if start == 0 else [])
        return httpx.Response(200, json={'id': 3, 'type': 'simple'} if request.method == 'GET' else None)

    client = client_for(respond)
    issue = {'key': 'WEB-1'}
    try:
        board = await client.board('3')
        assert board['sprints'] is None and board['config'] == {'id': 3, 'type': 'simple'}
        await client.transition(issue, '41', {'resolution': '10000'})
        assert requests[-1].url.path == '/rest/api/3/issue/WEB-1/transitions'
        assert json.loads(requests[-1].content) == {'transition': {'id': '41'}, 'fields': {'resolution': {'id': '10000'}}}
        await client.assign(issue, None)
        assert requests[-1].method == 'PUT' and json.loads(requests[-1].content) == {'accountId': None}
        await client.comment(issue, 'First line\nsecond\n\nNew paragraph')
        assert json.loads(requests[-1].content)['body'] == adf_paragraphs('First line\nsecond\n\nNew paragraph')
        assert adf_paragraphs('a\nb')['content'] == [{'type': 'paragraph', 'content': [
            {'type': 'text', 'text': 'a'}, {'type': 'hardBreak'}, {'type': 'text', 'text': 'b'}]}]
        with pytest.raises(JiraError, match='empty'):
            await client.comment(issue, '  ')
        assert await client.assignable(issue, 'a1') == ([{'accountId': 'a1', 'displayName': 'A'}], False)
    finally:
        await client.close()


def test_columns_hold_many_statuses_keep_empty_ones_and_lose_no_issue():
    config = {'columnConfig': {'columns': [{'name': 'Backlog', 'statuses': []},
                                           {'name': 'To Do', 'statuses': [{'id': '1'}, {'id': '2'}]},
                                           {'name': 'Done', 'statuses': [{'id': 3}]}]}}
    issues = [{'key': k, 'status': {'id': s}} for k, s in [('A-2', '2'), ('A-9', '9'), ('A-1', '1'), ('A-3', '3')]]
    groups = [(name, [i['key'] for i in cards]) for name, cards in group_issues(config, issues)]
    assert groups == [('Backlog', []), ('To Do', ['A-2', 'A-1']), ('Done', ['A-3']), ('Not on a board column', ['A-9'])]


def test_only_required_single_choice_fields_can_be_filled_in_here():
    transition = {'fields': {'resolution': {'required': True, 'allowedValues': [{'id': '1'}], 'schema': {'type': 'resolution'}},
                             'labels': {'required': True, 'allowedValues': [{'id': 'x'}], 'schema': {'type': 'array'}},
                             'worklog': {'required': True, 'name': 'Time spent'},
                             'comment': {'required': False}}}
    choosable, other = required_fields(transition)
    assert list(choosable) == ['resolution'] and other == ['labels', 'Time spent']


# ADF

def test_adf_keeps_its_structure_and_never_becomes_live_markup():
    doc = {'type': 'doc', 'content': [
        {'type': 'heading', 'attrs': {'level': 3}, 'content': [{'type': 'text', 'text': 'Plan'}]},
        {'type': 'paragraph', 'content': [{'type': 'text', 'text': '*not bold* [b]x[/b] '},
                                          {'type': 'text', 'text': 'bold', 'marks': [{'type': 'strong'}]},
                                          {'type': 'hardBreak'}, {'type': 'text', 'text': 'next', 'marks': [{'type': 'code'}]},
                                          {'type': 'mention', 'attrs': {'text': '@Ann'}}]},
        {'type': 'orderedList', 'attrs': {'order': 3}, 'content': [
            {'type': 'listItem', 'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': 'three'}]},
                                             {'type': 'bulletList', 'content': [{'type': 'listItem', 'content': [
                                                 {'type': 'paragraph', 'content': [{'type': 'text', 'text': 'nested'}]}]}]}]}]},
        {'type': 'table', 'content': [
            {'type': 'tableRow', 'content': [{'type': 'tableHeader', 'content': [{'type': 'paragraph', 'content': [
                {'type': 'text', 'text': 'a|b'}]}]}]},
            {'type': 'tableRow', 'content': [{'type': 'tableCell', 'content': [{'type': 'paragraph', 'content': [
                {'type': 'text', 'text': 'cell'}]}]}]}]},
        {'type': 'mediaGroup', 'content': [{'type': 'media', 'attrs': {'alt': 'spec.pdf'}}]},
        {'type': 'mysteryBlock'},
        {'type': 'panel', 'attrs': {'panelType': 'note'}, 'content': [{'type': 'paragraph', 'content': [
            {'type': 'text', 'text': 'careful', 'marks': [{'type': 'link', 'attrs': {'href': 'https://example.com/a b'}}]}]}]},
    ]}
    source = adf_markdown(doc, escape=False)
    assert source.split('\n')[:4] == ['### Plan', '', '*not bold* [b]x[/b] **bold**', '`next`@Ann']
    assert '3. three\n   - nested' in source and '| a\\|b |\n| --- |\n| cell |' in source
    assert '[attachment: spec.pdf]' in source and '[mysteryBlock]' in source
    assert '> **Note:**' in source and '[careful](https://example.com/a%20b)' in source
    text = shown(render(doc, 'empty'))
    assert '*not bold* [b]x[/b] bold' in text and 'careful (https://example.com/a%20b)' in text
    assert 'nested' in text and 'spec.pdf' in text and 'Plan' in text
    assert shown(render(doc, 'empty', source=True)).startswith('### Plan')
    assert shown(render(None, 'No description.')).strip() == 'No description.'
    assert '\x1b' not in shown(render({'type': 'doc', 'content': [{'type': 'paragraph', 'content': [
        {'type': 'text', 'text': 'bell\x07 \x1b[31mred'}]}]}, ''))


# Credentials

class Keychain:
    """Stands in for keyring's macOS backend, which is what the allowlist checks for."""
    name = 'macOS Keyring'

    def __init__(self, saved=None):
        self.saved = dict(saved or {})

    def get_password(self, service, url):
        assert service == 'jira-board'  # never anyone else's keychain items
        return self.saved.get(url)

    def set_password(self, service, url, token):
        assert service == 'jira-board'
        self.saved[url] = token

    def delete_password(self, service, url):
        assert service == 'jira-board'
        if url not in self.saved:
            raise keyring.errors.PasswordDeleteError('not found')
        del self.saved[url]


Keychain.__module__ = 'keyring.backends.macOS'
TOKENS = {'good-token': 'Jane', 'als-token': 'Ext Dev'}


@pytest.fixture
def jira(monkeypatch):
    """A Jira on every site that knows two tokens. Every request must be the read-only sign-in check."""
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == 'GET' and request.url.path == '/rest/api/3/myself'
        if request.url.host == 'down.atlassian.net':
            raise httpx.ConnectError('down')
        _, _, token = base64.b64decode(request.headers['Authorization'].removeprefix('Basic ')).decode().partition(':')
        if token in TOKENS:
            return httpx.Response(200, json={'accountId': 'acc', 'displayName': TOKENS[token]})
        return httpx.Response(401, json={'errorMessages': ['Client must be authenticated']})

    monkeypatch.setattr(credentials, 'Client', partial(Client, transport=httpx.MockTransport(respond)))
    return requests


@pytest.fixture
def terminal(monkeypatch):
    answers = {'line': [], 'secret': []}

    def take(kind, prompt=''):
        if not answers[kind]:
            raise EOFError
        return answers[kind].pop(0)

    monkeypatch.setattr(sys, 'stdin', type('Tty', (), {'isatty': lambda self: True})())
    monkeypatch.setattr('builtins.input', partial(take, 'line'))
    monkeypatch.setattr(getpass, 'getpass', partial(take, 'secret'))
    return answers


def als(monkeypatch, item=('dev@ext.example', 'als-token')):
    monkeypatch.setattr(credentials, 'keychain_item', lambda service, password=True: (
        item and (item[0], item[1] if password else None)) if service == 'als-atlassian-api' else None)


def test_the_als_keychain_item_is_used_for_its_own_site_only(monkeypatch, jira):
    als(monkeypatch)
    account, line = credentials.connect(interactive=False)
    assert (account.url, account.email) == (LEGACY_SITE, 'dev@ext.example')
    assert line == 'richemont.atlassian.net as Ext Dev · API token for dev@ext.example from the ALS keychain item ' \
                   '"als-atlassian-api".'
    assert {r.url.host for r in jira} == {'richemont.atlassian.net'}
    jira.clear()
    with pytest.raises(SetupError, match='No usable Jira credentials for https://other.atlassian.net'):
        credentials.connect('https://other.atlassian.net', interactive=False)
    assert not jira  # the ALS token never went anywhere else
    assert credentials.connect('richemont.atlassian.net/', interactive=False)[0].url == LEGACY_SITE


def test_the_als_item_is_read_with_apples_tool_and_its_token_is_never_an_argument(monkeypatch):
    monkeypatch.setattr(credentials, 'keychain_item', REAL_KEYCHAIN_ITEM)  # the tool itself is replaced below
    calls = []

    def run(args, **options):
        calls.append(args)
        if '-w' in args:
            return type('Done', (), {'returncode': 0, 'stdout': 'ATATT-secret\n'})()
        return type('Done', (), {'returncode': 0, 'stdout': 'attributes:\n    "acct"<blob>="dev@ext.example"\n'})()
    monkeypatch.setattr(credentials.sys, 'platform', 'darwin')
    monkeypatch.setattr(credentials.os.path, 'exists', lambda path: path == '/usr/bin/security')
    monkeypatch.setattr(credentials.subprocess, 'run', run)
    assert credentials.keychain_item('als-atlassian-api') == ('dev@ext.example', 'ATATT-secret')
    assert calls == [['/usr/bin/security', 'find-generic-password', '-s', 'als-atlassian-api'],
                     ['/usr/bin/security', 'find-generic-password', '-s', 'als-atlassian-api', '-a', 'dev@ext.example', '-w']]
    assert credentials.keychain_item('als-atlassian-api', password=False) == ('dev@ext.example', None) and len(calls) == 3


def test_verbose_logs_redact_the_token_and_the_basic_credentials():
    client = client_for(lambda request: httpx.Response(200))
    app = JiraApp(client, verbose=True)
    try:
        basic = client.http.headers['Authorization']
        logging.getLogger('board').error('echo %s %s %s', client.secrets[0], basic, 'ATATT3xFfGF0abcdefghijklmnopqrstuvwxyz')
        assert [text.split(' ', 2)[2] for _, text in app.log_buffer.lines] == ['echo [redacted] [redacted] [redacted]']
    finally:
        logging.getLogger('board').removeHandler(app.log_buffer)


def test_environment_comes_first_must_be_complete_and_must_match_the_site(monkeypatch, jira):
    als(monkeypatch)
    monkeypatch.setenv('JIRA_URL', 'Example.Atlassian.net')
    monkeypatch.setenv('JIRA_EMAIL', 'jane@example.com')
    monkeypatch.setenv('JIRA_API_TOKEN', 'good-token')
    account, line = credentials.connect(interactive=False)
    assert account.url == SITE and 'from the environment' in line
    with pytest.raises(SetupError, match='JIRA_URL is https://example.atlassian.net, but this board is on'):
        credentials.connect(LEGACY_SITE, interactive=False)
    monkeypatch.setenv('JIRA_API_TOKEN', 'revoked-token')
    with pytest.raises(SetupError, match='rejected JIRA_EMAIL and JIRA_API_TOKEN') as stop:
        credentials.connect(interactive=False)
    assert 'revoked-token' not in str(stop.value)
    monkeypatch.delenv('JIRA_API_TOKEN')
    with pytest.raises(SetupError, match='JIRA_API_TOKEN is not set'):
        credentials.connect(interactive=False)
    monkeypatch.setenv('JIRA_API_TOKEN', 'good-token')
    monkeypatch.setenv('JIRA_URL', 'https://down.atlassian.net')
    assert credentials.connect(interactive=False)[1].endswith('Connection not verified; press r to retry.')


def test_saved_tokens_are_per_site_and_a_broken_store_falls_back(monkeypatch, jira):
    als(monkeypatch, None)
    keychain = Keychain({SITE: 'good-token', 'https://b.atlassian.net': 'revoked'})
    monkeypatch.setattr('keyring.get_keyring', lambda: keychain)
    write_json(config_path(), {'url': SITE, 'sites': {SITE: 'jane@example.com', 'https://b.atlassian.net': 'b@x'}})
    assert credentials.connect(interactive=False)[0].url == SITE
    with pytest.raises(SetupError, match='rejected the API token saved in macOS Keyring'):
        credentials.connect('https://b.atlassian.net', interactive=False)
    assert {r.url.host for r in jira} == {'example.atlassian.net', 'b.atlassian.net'}

    def locked():
        raise RuntimeError('keychain locked')
    monkeypatch.setattr('keyring.get_keyring', locked)
    als(monkeypatch)
    with pytest.raises(SetupError, match='Could not read the saved token: keychain locked'):
        credentials.connect(interactive=False, site=SITE)
    assert credentials.connect(LEGACY_SITE, interactive=False)[0].email == 'dev@ext.example'
    write_json(config_path(), {'url': 'x'})
    assert credentials.read_config()[1].startswith('Ignoring')


def test_setup_checks_then_saves_the_token_in_the_store_only_and_logout_removes_only_it(monkeypatch, jira, terminal):
    als(monkeypatch)
    keychain = Keychain()
    monkeypatch.setattr('keyring.get_keyring', lambda: keychain)
    terminal['line'] = ['n', 'example.atlassian.net', 'jane@example.com', 'n', '', '']
    terminal['secret'] = ['bad-token', 'good-token']
    account, line = credentials.connect(force_setup=True)
    assert account.url == SITE and keychain.saved == {SITE: 'good-token'}
    assert json.loads(open(config_path()).read()) == {'url': SITE, 'sites': {SITE: 'jane@example.com'}}
    assert 'good-token' not in open(config_path()).read() and 'saved in macOS Keyring' in line
    terminal['line'] = ['', '']  # the ALS item, taken as it is: nothing is copied
    assert credentials.connect(force_setup=True)[0].url == LEGACY_SITE and keychain.saved == {SITE: 'good-token'}
    out = credentials.logout()
    assert keychain.saved == {} and 'ALS keychain item "als-atlassian-api"' in out and 'left as they are' in out
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    with pytest.raises(SetupError, match='Run issueboard jira --setup in a terminal'):
        credentials.setup()


def test_an_unusable_store_keeps_the_token_for_the_session_without_saving(monkeypatch, jira, terminal, capsys):
    als(monkeypatch, None)

    class Broken(Keychain):
        def set_password(self, service, url, token):
            raise RuntimeError(f'refused {token}')
    Broken.__module__ = 'keyring.backends.macOS'
    monkeypatch.setattr('keyring.get_keyring', lambda: Broken())
    terminal['line'] = [SITE, 'jane@example.com']
    terminal['secret'] = ['good-token']
    account, _ = credentials.connect(force_setup=True)
    assert account.source == 'entered in setup, for this session only'
    out = capsys.readouterr().out
    assert 'good-token' not in out and '[redacted]' in out
    assert not credentials.read_config()[0]


def test_the_command_line_reports_auth_status_and_never_opens_without_credentials(monkeypatch, jira, capsys):
    als(monkeypatch)
    main(['--auth-status'])
    assert 'richemont.atlassian.net as Ext Dev' in capsys.readouterr().out
    als(monkeypatch, None)
    with pytest.raises(SystemExit, match='jira-board: No usable Jira credentials'):
        main(['--auth-status'])


# The board

async def test_a_scrum_board_opens_its_active_sprint_with_columns_of_several_statuses():
    app = board_app()
    async with app.run_test(size=(140, 40)) as pilot:
        await settle(app, pilot)
        assert app.sprint_data['name'] == 'Sprint 14'
        assert [c.border_title for c in app.query(Column)] == [
            'To Do · 2', 'In Progress · 2', 'In Review · 1', 'Done · 2', 'Not on a board column · 1']
        assert [c.issue['key'] for c in app.query(Column)[1].children] == ['WEB-3', 'WEB-4']
        assert app.selected['key'] == 'WEB-1'
        assert 'Enter expands the details and reads the description' in shown(app.details(app.selected))
        await pilot.press('enter')
        await settle(app, pilot)
        assert 'Acceptance' in shown(app.details(app.selected), 120)
        await pilot.press('escape', 'right', 'down')
        assert app.selected['key'] == 'WEB-4'
        await pilot.press('M')
        assert {c.issue['key'] for c in app.query(Card)} == {'WEB-1', 'WEB-3', 'WEB-7'}
        await pilot.press('x', 'slash', 'b', 'l', 'o', 'c', 'k')
        assert [c.issue['key'] for c in app.query(Card)] == ['WEB-4']  # its status, not only its text
        assert not app.check_action('merge_requests', ())
        assert 'merge' not in app.DETAILS


async def test_without_an_active_sprint_nothing_is_shown_as_current_until_one_is_chosen():
    class Spy(DemoClient):
        asked = []

        async def issues(self, board, sprint=None):
            self.asked.append(sprint and sprint['name'])
            return await super().issues(board, sprint)

    client = Spy()
    app = board_app(client, {'3': 1})
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        assert app.board_data['name'] == 'Mobile' and not client.asked and not app.query(Card)
        assert 'No active sprint on Mobile' in str(app.query_one('#board > .empty').render())
        selector = app.query_one('#sprints', Select)
        assert selector.prompt == 'No active sprint'
        assert [label for label, _ in selector._options[1:]] == ['Mobile 3 (future)', 'Mobile 2 (closed)']
        selector.value = '3'
        await settle(app, pilot)
        assert client.asked == ['Mobile 3'] and {c.issue['key'] for c in app.query(Card)} == {'MOB-1', 'MOB-2'}
        app.tick()  # auto-refresh reads the chosen sprint only
        await settle(app, pilot)
        assert set(client.asked) == {'Mobile 3'}


async def test_a_kanban_board_has_no_sprints_keeps_empty_columns_and_has_no_backlog():
    app = board_app(visited={'2': 1})
    async with app.run_test(size=(140, 40)) as pilot:
        await settle(app, pilot)
        assert not app.query_one('#sprints').display
        assert [c.border_title for c in app.query(Column)] == ['Backlog · 0', 'Draft · 1', 'Ready · 1', 'In Progress · 1',
                                                              'Done · 1']
        assert 'kanban sub-filter applied' in str(app.query_one('#status').render())
        await pilot.press('B')
        await settle(app, pilot)
        assert isinstance(app.screen, BacklogScreen)
        assert 'Jira has no backlog for this board' in str(app.screen.query_one('#details').render())


async def test_the_backlog_pages_in_rank_order_and_its_details_load_on_enter():
    app = board_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        await pilot.press('B')
        await settle(app, pilot)
        backlog = app.screen
        assert len(backlog.issues) == 50 and isinstance(backlog.query_one('#backlog').children[-1], More)
        await pilot.press('n')
        await settle(app, pilot)
        assert len(backlog.issues) == 60 and not backlog.more
        assert backlog.issues[0]['summary'] == 'Polish the login form'
        assert '60 issues, in rank order' in str(backlog.query_one('#status').render())


async def test_moving_asks_for_the_required_resolution_and_explains_complex_forms():
    app = board_app()
    client = app.client
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        await pilot.press('right')  # WEB-3, In Progress
        await pilot.press('m')
        await settle(app, pilot)
        picker = app.screen
        assert [c[0]['name'] for c in picker.shown] == ['Send to review', 'Block', 'Done', 'Log work and close']
        assert picker.shown[picker.query_one('OptionList').highlighted][0]['name'] == 'Send to review'
        picker.query_one(Input).value = 'log work'
        await pilot.press('enter')
        await pilot.pause()
        assert 'needs Time spent' in str(app.query_one('#status').render())
        assert client.find({'key': 'WEB-3'})['fields']['status']['name'] == 'In Progress'
        await pilot.press('m')
        await settle(app, pilot)
        app.screen.query_one(Input).value = 'done'
        await pilot.press('enter')
        await pilot.pause()
        assert 'Resolution (required)' in str(app.screen.query_one('Label').render())
        await pilot.press('down', 'enter')  # Won't Do
        await settle(app, pilot)
        assert client.find({'key': 'WEB-3'})['fields']['status']['name'] == 'Done'
        assert [c.issue['key'] for c in app.query(Column)[3].children] == ['WEB-3', 'WEB-6', 'WEB-7']


async def test_assigning_offers_me_first_and_comment_failures_keep_the_text():
    class Flaky(DemoClient):
        async def comment(self, issue, text):
            raise JiraError('Cannot reach Jira at example.atlassian.net.')

    app = board_app(Flaky())
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        await pilot.press('down', 'a')  # WEB-2, Jordan's
        await settle(app, pilot)
        names = [str(c[1]) for c in app.screen.shown]
        assert names[0] == 'Alex Morgan  · you' and names[-1] == 'Unassigned'
        assert 'Kim Park' not in ' '.join(names)  # inactive
        await pilot.press('enter')
        await settle(app, pilot)
        assert app.client.find({'key': 'WEB-2'})['fields']['assignee']['displayName'] == 'Alex Morgan'
        await pilot.press('c')
        app.screen.query_one(Input).value = 'Looks good'
        await pilot.press('enter')
        await settle(app, pilot)
        assert 'Your comment is kept' in str(app.query_one('#status').render())
        await pilot.press('c')
        assert isinstance(app.screen, CommentDialog) and app.screen.query_one(Input).value == 'Looks good'


async def test_switching_boards_quickly_discards_the_slower_answer():
    class Slow(DemoClient):
        release = asyncio.Event()

        async def board(self, board_id):
            if board_id == '1':
                await self.release.wait()
            return await super().board(board_id)

    client = Slow()
    app = board_app(client)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.open_board('2')
        for _ in range(10):
            await pilot.pause()
        client.release.set()
        for _ in range(10):
            await pilot.pause()
        assert app.board_data['name'] == 'Design' and {c.issue['key'][:3] for c in app.query(Card)} == {'DES'}


async def test_the_board_picker_lists_the_scope_and_can_widen_it():
    app = board_app()
    app.prefs.data['project'] = 'DES'
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        await pilot.press('b')
        await settle(app, pilot)
        assert [c[0] for c in app.screen.shown] == ['2', '*']
        await pilot.press('down', 'enter')
        await settle(app, pilot)
        assert app.prefs.data['project'] is None and [c[0] for c in app.screen.shown] == ['1', '2', '3']


async def test_comments_show_author_visibility_and_newest_first():
    app = board_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        await pilot.press('right', 'C')  # WEB-3
        await settle(app, pilot)
        thread = [str(w.render()) for w in app.screen.query('#thread > .comment')]
        assert len(thread) == 3 and 'Pagination now follows' in shown(app.comment_view((await app.client.comments(
            {'key': 'WEB-3'}))[0]))
        assert 'visible to role Developers' in shown(app.comment_view((await app.client.comments({'key': 'WEB-3'}))[1]))


# Launcher

@pytest.fixture
def run(monkeypatch):
    started = []
    monkeypatch.setattr(JiraApp, 'run', lambda self: started.append(self))
    monkeypatch.setattr(issueboard, 'Jira', lambda url, email, token: DemoClient())
    return started


def test_issueboard_sets_up_a_jira_folder_and_reopens_it_on_its_own_site(tmp_path, monkeypatch, run, capsys):
    monkeypatch.chdir(tmp_path)
    folder = str(tmp_path.resolve())
    account = credentials.Credentials(SITE, 'jane@example.com', 'not-a-real-token', 'saved in a test keyring')
    asked_sites = []
    monkeypatch.setattr(credentials, 'connect', lambda site=None, **_: asked_sites.append(site) or (account, 'demo'))
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    answers = ['3']
    monkeypatch.setattr('builtins.input', lambda prompt='': answers.pop(0))
    picks = iter([1, 0])  # project WEB (after All projects), then its only board
    monkeypatch.setattr(issueboard.SetupPicker, 'run', lambda self: next(picks))
    issueboard.main([])
    saved = json.loads(open(issueboard.config_path()).read())['directories']
    assert saved == {folder: {'backend': 'jira', 'url': SITE, 'project': 'WEB', 'board': '1', 'name': 'Web team'}}
    assert 'Jira board Web team on https://example.atlassian.net' in capsys.readouterr().out
    assert run[0].prefs.data['project'] == 'WEB' and '1' in run[0].prefs.data['visited']
    issueboard.main([])
    assert asked_sites == [None, SITE] and len(run) == 2
    assert not open(issueboard.config_path()).read().count('token')


def test_a_jira_folder_never_opens_with_credentials_of_another_site(tmp_path, monkeypatch, run):
    monkeypatch.chdir(tmp_path)
    als(monkeypatch)
    monkeypatch.setattr(credentials, 'Client', lambda *_, **__: pytest.fail('a token was sent'))
    write_json(issueboard.config_path(), {'directories': {str(tmp_path.resolve()): {
        'backend': 'jira', 'url': 'https://other.atlassian.net', 'project': None, 'board': '1'}}})
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    with pytest.raises(SystemExit, match='No usable Jira credentials for https://other.atlassian.net'):
        issueboard.main([])
    assert not run


def test_the_jira_demo_starts_offline(monkeypatch, run):
    monkeypatch.setattr(credentials, 'connect', lambda *_, **__: pytest.fail('the demo looked for credentials'))
    issueboard.main(['--demo', 'jira'])
    assert type(run[0].client).__name__ == 'DemoClient' and not run[0].prefs.path
    assert not os.path.exists(default_path('jira-board'))



def test_cancelling_jira_setup_saves_nothing(tmp_path, monkeypatch, run):
    monkeypatch.chdir(tmp_path)
    account = credentials.Credentials(SITE, 'jane@example.com', 'not-a-real-token', 'saved in a test keyring')
    monkeypatch.setattr(credentials, 'connect', lambda site=None, **_: (account, 'demo'))
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda prompt='': '3')
    monkeypatch.setattr(issueboard.SetupPicker, 'run', lambda self: None)  # Esc in the project list
    with pytest.raises(SystemExit, match='cancelled'):
        issueboard.main([])
    assert not run and not os.path.exists(issueboard.config_path())
