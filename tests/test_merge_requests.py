import asyncio
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from textual.widgets import Input, OptionList, Static

from gitlab_board import merge_requests as mrs
from gitlab_board.client import Client, mentions
from gitlab_board.demo import DemoClient
from gitlab_board.glab import LOGIN, Glab, GlabError
from gitlab_board.merge_requests import MergeRequestsScreen, repository
from gitlab_board.target import TargetError
from test_gitlab import FAKE, FakeGlab, demo_app, fake_glab, settle  # noqa: F401  (fake_glab is a fixture)
from youtrack_board.app import BoardApp, Column, CommentDialog
from youtrack_board.demo import DemoClient as YouTrackDemo
from youtrack_board.demo import demo_gitlab, demo_state
from youtrack_board.prefs import Prefs

HOST = 'gitlab.example.com'


def mr(iid, path='g/app', **fields):
    return {'iid': str(iid), 'title': f'MR {iid}', 'webUrl': f'https://{HOST}/{path}/-/merge_requests/{iid}', 'state': 'opened',
            'draft': False, 'conflicts': False, 'detailedMergeStatus': 'MERGEABLE', 'sourceBranch': 'feature', 'targetBranch': 'main',
            'project': {'fullPath': path}, 'sourceProject': {'fullPath': path},
            'headPipeline': {'status': 'SUCCESS', 'detailedStatus': {'label': 'passed'}}, **fields}


def hint(screen):
    return str(screen.query_one('#mr-hint').content)


# GitLab: what GitLab links to an issue, and how it is read

async def test_issue_merge_requests_are_read_as_the_issue_page_reads_them():
    fork = mr(7, sourceProject={'fullPath': 'someone/app'})
    answer = {'workItem': {'widgets': [{}, {
        'closingMergeRequests': {'pageInfo': {'hasNextPage': False}, 'nodes': [{'mergeRequest': fork}, {'mergeRequest': None}]},
        'relatedMergeRequests': {'pageInfo': {'hasNextPage': True}, 'nodes': [fork, mr(2, 'g/lib'), mr(3, state='merged')]}}]}}
    fake = FakeGlab(GlabError("Field 'approvalsLeft' doesn't exist on type 'MergeRequest'"), answer)
    found = await Client(HOST, fake).issue_merge_requests({'id': 'gid://gitlab/Issue/9', 'reference': 'g/app#3'})
    assert 'workItem(id: "gid://gitlab/WorkItem/9")' in fake.documents[0] and 'approvalsLeft' in fake.documents[0]
    # Community Edition has no approval fields: asked again without them.
    assert 'approvalsLeft' not in fake.documents[1]
    assert 'closingMergeRequests(first: 100)' in fake.documents[1] and 'relatedMergeRequests(first: 100)' in fake.documents[1]
    assert [(m['project']['fullPath'], m['iid'], m['why']) for m in found['mrs']] == [
        ('g/app', '7', 'Closes this issue when merged'), ('g/lib', '2', 'Mentions this issue'), ('g/app', '3', 'Mentions this issue')]
    assert found['mrs'][0]['sourceProject'] == {'fullPath': 'someone/app'} and 'first 100' in found['more']
    with pytest.raises(GlabError, match='hidden from you'):
        await Client(HOST, FakeGlab({'workItem': None})).issue_merge_requests({'id': 'gid://gitlab/Issue/1', 'reference': 'g/app#1'})


async def test_merge_request_reads_go_through_glab_on_stdin(fake_glab, monkeypatch):
    runner, calls = fake_glab
    monkeypatch.setenv('FAKE_OUT', json.dumps({'data': {'workItem': {'widgets': [{'type': 'DEVELOPMENT'}]}}}))
    found = await Client(HOST, runner).issue_merge_requests({'id': 'gid://gitlab/Issue/42', 'reference': 'secret/project#1'})
    assert found == {'mrs': [], 'more': False}
    call = calls()[-1]
    assert call['argv'] == ['api', 'graphql', '--hostname', HOST, '-F', 'query=@-']
    assert 'gid://gitlab/WorkItem/42' in call['stdin'] and call['env']['GLAB_DEBUG_HTTP'] is None


def test_status_words_never_claim_more_than_gitlab_says():
    assert mrs.summary(mr(1)) == 'pipeline passed · ready to merge'
    assert mrs.summary(mr(1, headPipeline=None, detailedMergeStatus='UNCHECKED')) == 'no pipeline · mergeability not checked yet'
    assert mrs.summary(mr(1, detailedMergeStatus='CHECKING', headPipeline={'status': 'RUNNING', 'detailedStatus': {}})) == \
        'pipeline running · checking mergeability'
    assert mrs.summary(mr(1, draft=True, detailedMergeStatus='DRAFT_STATUS', conflicts=True)) == 'draft · pipeline passed · conflicts'
    assert mrs.summary(mr(1, detailedMergeStatus='CONFLICT', conflicts=True)) == 'pipeline passed · conflicts'
    assert mrs.summary(mr(1, state='locked', detailedMergeStatus=None)) == 'being merged · pipeline passed · merge status unknown'
    assert mrs.summary(mr(1, detailedMergeStatus='SECURITY_POLICIES_VIOLATIONS')) == 'pipeline passed · security policies violations'
    assert mrs.approvals(mr(1)) is None and mrs.approvals(mr(1, approvalsRequired=0, approvalsLeft=0)) is None
    assert mrs.approvals(mr(1, approvalsRequired=2, approvalsLeft=0)) == 'approved'
    shown = mrs.details(mr(1, sourceBranch='a\x1b]0;x\x07[b]b', sourceProject=None, why='[red]x')).plain
    assert '\x1b' not in shown and '[b]b → main' in shown and 'fork' not in shown and '[red]x' in shown


def test_only_merge_request_pages_on_the_known_host_open():
    assert mrs.web_url(mr(4, 'g/lib'), HOST) == f'https://{HOST}/g/lib/-/merge_requests/4'  # another project, same host
    for bad in ('http://gitlab.example.com/g/app/-/merge_requests/4', 'https://evil.example.net/g/app/-/merge_requests/4',
                'https://me@gitlab.example.com/g/app/-/merge_requests/4', 'https://gitlab.example.com/g/other/-/merge_requests/4',
                'https://gitlab.example.com/g/app/-/merge_requests/4?x=1', 'javascript:alert(1)', None):
        assert mrs.web_url(mr(4, webUrl=bad), HOST) is None


# GitLab board: the panel

async def test_panel_lists_open_linked_merge_requests_and_returns_to_the_card(monkeypatch):
    monkeypatch.setattr(Glab, 'run', lambda *a, **k: pytest.fail('the demo ran glab'))
    opened = []
    monkeypatch.setattr('webbrowser.open', lambda url: opened.append(url) or True)
    for size in ((80, 24), (60, 20)):
        app, _ = demo_app()
        async with app.run_test(size=size) as pilot:
            await settle(app, pilot)
            await pilot.press('right', 'right', 'down')  # #3, in Doing
            column, card, scroll = app.focused, app.selected, app.query_one('#board').scroll_offset
            await pilot.press('g')
            await settle(app, pilot)
            screen = app.screen
            assert isinstance(screen, MergeRequestsScreen)
            assert [mrs.reference(m, screen.home) for m in screen.mrs] == ['!1', '!2', 'demo/api!1']  # another project in full
            assert hint(screen).startswith('3 open · 1 merged or closed not shown') and 'Esc back' in hint(screen)
            assert screen.query_one(OptionList).size.height >= 3 and screen.query_one('#mr-details').size.height >= 3
            assert screen.query_one('#mr-hint').region.bottom <= screen.query_one('#mr-dialog').region.bottom
            details = str(screen.query_one('#mr-details').content)
            assert 'Closes this issue when merged' in details and 'Approvals: 1 of 2 still needed' in details
            await pilot.press('down')
            assert 'from the fork sam/portal' in str(screen.query_one('#mr-details').content)
            await pilot.press('enter')
            assert opened[-1] == f'https://{HOST}/demo/portal/-/merge_requests/2'
            await pilot.press('escape')
            assert app.screen is not screen and app.focused is column and app.selected is card
            assert app.query_one('#board').scroll_offset == scroll


class SlowMergeRequests(DemoClient):
    """Answers only when released; counts what it was asked and what was cancelled."""

    def __init__(self):
        super().__init__()
        self.asked, self.release, self.cancelled = [], asyncio.Event(), 0

    async def issue_merge_requests(self, issue):
        self.asked.append(issue['reference'])
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return await super().issue_merge_requests(issue)


async def test_closing_the_panel_cancels_its_request_and_refresh_never_reads_merge_requests():
    client = SlowMergeRequests()
    app, _ = demo_app(client=client)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press('right', 'right', 'down', 'g')
        await pilot.pause()
        assert hint(app.screen).startswith('Loading')
        app.tick()  # auto-refresh waits while the panel is open
        await pilot.press('escape')
        await pilot.pause()
        assert client.cancelled == 1 and app.selected['reference'] == 'demo/portal#3'
        await pilot.press('up', 'g')  # another card: only its answer can land
        client.release.set()
        await settle(app, pilot)
        assert client.asked == ['demo/portal#3', 'demo/portal#1']
        assert [m['title'] for m in app.screen.mrs] == ['Arrow keys across lanes']
        assert 'conflicts' in mrs.summary(app.screen.mrs[0]) and 'no pipeline' in mrs.summary(app.screen.mrs[0])
        await pilot.press('r')
        await settle(app, pilot)
        await pilot.press('escape')
        for _ in range(3):
            app.tick()
            await settle(app, pilot)
        assert len(client.asked) == 3  # opened twice and reloaded once; board refreshes read none


async def test_panel_explains_errors_and_empty_results_and_retries():
    client = DemoClient()
    errors = [GlabError(LOGIN.format(host=HOST))]
    original = client.issue_merge_requests

    async def failing(issue):
        if errors:
            raise errors.pop()
        return await original(issue)
    client.issue_merge_requests = failing
    app, _ = demo_app(client=client)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press('right', 'g')  # #2 has no merge request
        await settle(app, pilot)
        assert 'glab auth login --hostname gitlab.example.com' in hint(app.screen)
        assert app.screen.query_one('#mr-hint').has_class('error') and not app.screen.mrs
        await pilot.press('o')  # nothing selected: nothing opens
        await pilot.press('r')
        await settle(app, pilot)
        assert hint(app.screen).startswith('No open') and not app.screen.query_one('#mr-hint').has_class('error')
        assert 'No open merge request is linked.' in str(app.screen.query_one(OptionList).get_option_at_index(0).prompt)
        await pilot.press('u')  # choosing a repository is the YouTrack board's; here it does nothing
        assert isinstance(app.screen, MergeRequestsScreen)


# YouTrack: open MRs that name the story, in the repository chosen for its project

def test_story_ids_match_as_whole_tokens_only():
    def where(**fields):
        return mentions('DEMO-1', {'title': '', 'sourceBranch': '', 'description': None, **fields})
    assert where(title='[DEMO-1] Fix', sourceBranch='feature/demo-1-fix') == 'title, branch'
    assert where(description='Done.\nCloses DEMO-1, see DEMO-12') == 'description'
    for other in ('DEMO-10', 'XDEMO-1', 'DEMO-1a', 'DEMO-11-fix', 'demo-100', 'ODEMO-1'):
        assert where(title=other, sourceBranch=other, description=other) == ''


def test_repository_urls_name_one_project():
    assert repository('https://gitlab.liip.ch/a/b/app') == ('gitlab.liip.ch', 'a/b/app')
    assert repository(' gitlab.liip.ch/a/app/-/merge_requests/3?x=1#y ') == ('gitlab.liip.ch', 'a/app')
    assert repository('https://GitLab.example.com:8443/a/app.git') == ('gitlab.example.com:8443', 'a/app')
    for bad in ('https://gitlab.com/groups/a', 'https://me:pw@gitlab.com/a/b', 'https://gitlab.com/', 'ftp://gitlab.com/a/b',
                'https://gitlab.com/a/../b'):
        with pytest.raises(TargetError):
            repository(bad)


def page(*mrs_, more=None):
    return {'project': {'mergeRequests': {'pageInfo': {'hasNextPage': bool(more), 'endCursor': more}, 'nodes': list(mrs_)}}}


async def test_project_scan_pages_by_cursor_then_reads_the_status_of_matches_only(monkeypatch):
    light = [{'iid': '9', 'title': 'Export (DEMO-10)', 'sourceBranch': 'x', 'description': ''},
             {'iid': '5', 'title': 'y', 'sourceBranch': 'demo-1-lanes', 'description': None}]
    fake = FakeGlab(page(light[0], more='c1'), page(light[1]), {'project': {'mergeRequests': {'nodes': [mr(5)]}}})
    found = await Client(HOST, fake).project_merge_requests('g/app', 'DEMO-1')
    assert 'project(fullPath: "g/app") { mergeRequests(state: opened, first: 100)' in fake.documents[0]
    assert 'after: "c1"' in fake.documents[1] and 'description' in fake.documents[1]
    assert 'mergeRequests(iids: ["5"], first: 1)' in fake.documents[2] and 'headPipeline' in fake.documents[2]
    assert [(m['iid'], m['why']) for m in found['mrs']] == [('5', 'Names DEMO-1 in its branch')] and not found['more']
    nothing = FakeGlab(page(light[0]))
    assert await Client(HOST, nothing).project_merge_requests('g/app', 'DEMO-1') == {'mrs': [], 'more': False}
    assert len(nothing.documents) == 1  # no match: no status query
    monkeypatch.setattr('gitlab_board.client.OPEN_PAGES', 2)
    capped = FakeGlab(page(more='c1'), page(more='c2'))
    assert 'first 200 open merge requests of g/app' in (await Client(HOST, capped).project_merge_requests('g/app', 'X-1'))['more']
    with pytest.raises(GlabError, match='no project g/gone'):
        await Client(HOST, FakeGlab({'project': None})).project_merge_requests('g/gone', 'X-1')


async def test_project_scan_keeps_all_matches_across_status_query_pages():
    light = [{'iid': str(i), 'title': 'DEMO-1', 'sourceBranch': 'fix', 'description': ''} for i in range(101)]
    fake = FakeGlab(page(*light[:100], more='next'), page(light[100]),
                    page(*(mr(i) for i in range(100))), page(mr(100)))
    found = await Client(HOST, fake).project_merge_requests('g/app', 'DEMO-1')
    assert len(found['mrs']) == 101 and not found['more']
    assert {item['iid'] for item in found['mrs']} == {str(i) for i in range(101)}


async def test_project_scan_runs_glab_with_the_story_on_stdin_only(fake_glab, monkeypatch):
    runner, calls = fake_glab
    monkeypatch.setenv('FAKE_OUT', json.dumps({'data': page()}))
    assert (await Client(HOST, runner).project_merge_requests('team/secret-app', 'SEC-7'))['mrs'] == []
    call = calls()[-1]
    assert call['argv'] == ['api', 'graphql', '--hostname', HOST, '-F', 'query=@-']
    assert 'team/secret-app' in call['stdin'] and 'SEC-7' not in call['stdin']  # matching happens here, not on GitLab


def test_repository_choices_are_kept_per_server_and_project(tmp_path):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps({'https://other': {'repos': {'0-1': 'https://gitlab.com/x/y'}},
                                'https://a': {'repos': {'0-1': 'https://gitlab.com/a/b', '0-2': 7}}}))
    prefs = Prefs(str(path), 'https://a')
    assert prefs.data['repos'] == {'0-1': 'https://gitlab.com/a/b'}  # a malformed value is dropped
    prefs.data['repos']['0-3'] = 'https://gitlab.com/a/c'
    prefs.save()
    saved = json.loads(path.read_text())
    assert saved['https://other'] == {'repos': {'0-1': 'https://gitlab.com/x/y'}}
    assert saved['https://a']['repos'] == {'0-1': 'https://gitlab.com/a/b', '0-3': 'https://gitlab.com/a/c'}


async def pick(app, pilot, key):
    """Select a story's card on the board, as arrow keys would."""
    column = next(c for c in app.query(Column) if any(card.issue['idReadable'] == key for card in c.children))
    column.focus()
    column.index = [card.issue['idReadable'] for card in column.children].index(key)
    await pilot.pause()
    assert app.selected['idReadable'] == key


async def test_youtrack_panel_shows_open_merge_requests_naming_the_story(monkeypatch):
    monkeypatch.setattr(Glab, 'run', lambda *a, **k: pytest.fail('the demo ran glab'))
    opened = []
    monkeypatch.setattr('webbrowser.open', lambda url: opened.append(url) or True)
    for size in ((80, 24), (60, 20)):
        client = YouTrackDemo()
        app = BoardApp(client, Prefs(None, client.base_url, demo_state()), gitlab=demo_gitlab)
        async with app.run_test(size=size) as pilot:
            await settle(app, pilot)
            await pick(app, pilot, 'DEMO-3')
            column, card = app.focused, app.selected
            await pilot.press('g')
            await settle(app, pilot)
            screen = app.screen
            assert isinstance(screen, MergeRequestsScreen) and screen.home == 'demo/portal'
            assert [(mrs.reference(m, screen.home), m['why']) for m in screen.mrs] == [
                ('!1', 'Names DEMO-3 in its title, branch'), ('!2', 'Names DEMO-3 in its description')]  # merged !4 left out
            assert hint(screen).startswith('2 open') and 'u repository' in hint(screen)
            assert screen.query_one('#mr-hint').region.bottom <= screen.query_one('#mr-dialog').region.bottom
            await pilot.press('o')
            assert opened[-1] == f'https://{HOST}/demo/portal/-/merge_requests/1'
            await pilot.press('escape')
            assert app.focused is column and app.selected is card
            await pick(app, pilot, 'DEMO-1')
            await pilot.press('g')
            await settle(app, pilot)
            assert [m['title'] for m in app.screen.mrs] == ['Arrow keys across lanes']  # not the DEMO-10 one
            await pilot.press('escape')


class CountingGitLab(DemoClient):
    places = 0

    async def place(self, path, kind=''):
        self.places += 1
        return await super().place(path, kind)


async def test_first_use_asks_for_the_repository_then_validates_saves_and_reuses_it(tmp_path):
    client = YouTrackDemo()
    path = tmp_path / 'state.json'
    path.write_text(json.dumps({'https://other.youtrack.cloud': {'repos': {'0-2': 'https://gitlab.com/keep/this'}}}))
    gitlab = CountingGitLab()
    app = BoardApp(client, Prefs(str(path), client.base_url), gitlab=lambda host: demo_gitlab(host) and gitlab)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, 'WEB-2')
        await pilot.press('g')
        assert isinstance(app.screen, CommentDialog) and 'GitLab repository of WEB' in str(app.screen.query_one('Label').content)
        await pilot.press('escape')  # cancelled: nothing saved, nothing asked of GitLab
        assert app.prefs.data['repos'] == {} and gitlab.places == 0 and app.selected['idReadable'] == 'WEB-2'
        await pilot.press('g', *'https://gitlab.example.com/groups/demo', 'enter')
        assert 'Not a GitLab project URL' in str(app.query_one('#status', Static).content) and not app.prefs.data['repos']
        await pilot.press('g', *'gitlab.example.com/demo/gone', 'enter')
        await settle(app, pilot)
        assert 'no project or group demo/gone' in hint(app.screen) and app.screen.query_one('#mr-hint').has_class('error')
        assert app.prefs.data['repos'] == {}  # GitLab did not confirm it: not saved
        await pilot.press('u')  # change it: the dialog starts from the last try
        assert app.screen.query_one(Input).value == f'https://{HOST}/demo/gone'
        app.screen.query_one(Input).value = f'https://{HOST}/demo/api/-/merge_requests'
        await pilot.press('enter')
        await settle(app, pilot)
        assert [m['title'] for m in app.screen.mrs] == ['Prices endpoint for the pricing page']
        assert app.prefs.data['repos'] == {'0-2': f'https://{HOST}/demo/api'} and gitlab.places == 2
        await pilot.press('escape', 'g')  # reused: no question, no new check
        await settle(app, pilot)
        assert isinstance(app.screen, MergeRequestsScreen) and gitlab.places == 2 and len(app.screen.mrs) == 1
        await pilot.press('escape')
        await pick(app, pilot, 'DEMO-3')
        await pilot.press('g')  # another project of the same server has its own choice
        assert isinstance(app.screen, CommentDialog)
        await pilot.press('escape')
    saved = json.loads(path.read_text())
    assert saved['https://other.youtrack.cloud'] == {'repos': {'0-2': 'https://gitlab.com/keep/this'}}
    assert saved[client.base_url]['repos'] == {'0-2': f'https://{HOST}/demo/api'}


async def test_a_broken_saved_repository_is_asked_for_again():
    client = YouTrackDemo()
    app = BoardApp(client, Prefs(None, client.base_url, {'repos': {'0-1': 'not a url/'}}), gitlab=demo_gitlab)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, 'DEMO-3')
        await pilot.press('g')
        assert isinstance(app.screen, CommentDialog) and app.screen.query_one(Input).value == 'not a url/'


async def test_glab_missing_or_signed_out_is_explained_and_nothing_is_saved(tmp_path, monkeypatch):
    client = YouTrackDemo()
    app = BoardApp(client, Prefs(None, client.base_url))  # glab's GitLab, as outside the demo
    monkeypatch.setattr('shutil.which', lambda name: None)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, 'DEMO-3')
        await pilot.press('g')
        assert not app.screen.is_modal and 'brew install glab' in str(app.query_one('#status', Static).content)
    monkeypatch.undo()
    script = tmp_path / 'glab'
    script.write_text(FAKE.format(python=sys.executable))
    script.chmod(0o755)
    monkeypatch.setenv('PATH', f'{tmp_path}{os.pathsep}{os.environ["PATH"]}')
    monkeypatch.setenv('FAKE_LOG', str(tmp_path / 'log.jsonl'))
    monkeypatch.setenv('FAKE_EXIT', '1')
    monkeypatch.setenv('FAKE_ERR', 'glab: 401 Unauthorized (HTTP 401)')
    app = BoardApp(client, Prefs(None, client.base_url))
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, 'DEMO-3')
        await pilot.press('g', *'https://gitlab.example.com/demo/portal', 'enter')
        await settle(app, pilot)
        assert 'glab auth login --hostname gitlab.example.com' in hint(app.screen) and app.prefs.data['repos'] == {}
    call = json.loads((tmp_path / 'log.jsonl').read_text().splitlines()[-1])
    assert call['argv'] == ['api', 'graphql', '--hostname', HOST, '-F', 'query=@-'] and 'demo/portal' in call['stdin']


def test_the_youtrack_board_starts_without_gitlab_code():
    out = subprocess.run([sys.executable, '-c', 'import sys, youtrack_board.app; print(sorted(m for m in sys.modules '
                          'if m.startswith("gitlab_board")))'], capture_output=True, text=True, check=True).stdout
    assert out.strip() == '[]'


def test_the_package_ships_every_gitlab_module_the_youtrack_board_imports():
    """Built artifacts (wheel and sdist) must hold what g imports; the GitLab board app itself stays out."""
    out = subprocess.run([sys.executable, '-c', 'import sys, youtrack_board.app, youtrack_board.demo as d; '
                          'd.demo_gitlab("gitlab.example.com"); import gitlab_board.merge_requests; '
                          'print(" ".join(sorted(m for m in sys.modules if m.startswith("gitlab_board."))))'],
                         capture_output=True, text=True, check=True).stdout.split()
    needed = {f'gitlab_board/{m.split(".")[1]}.py' for m in out} | {'gitlab_board/__init__.py'}
    build = tomllib.loads((Path(__file__).parent.parent / 'pyproject.toml').read_text())['tool']['hatch']['build']['targets']
    for kind in ('wheel', 'sdist'):
        shipped = {p for p in build[kind]['include'] if p.startswith('gitlab_board')}
        assert shipped == needed and 'gitlab_board/app.py' not in shipped, kind
