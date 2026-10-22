import asyncio
import io
import json
import os
import sys

import pytest
from rich.console import Console
from textual.widgets import Input, OptionList, Static

from gitlab_board import glab, target
from gitlab_board.app import AskDialog, GitLabApp, ListScreen, NotesScreen, Prefs, main
from gitlab_board.client import Client, Enum, board_input, describe_move, literal, move_effects
from gitlab_board.demo import BOARDS, DemoClient, demo_state
from gitlab_board.glab import Glab, GlabError
from issueboard.widgets import Column, Picker

SAMPLE = ('https://gitlab.com/geocity/geocity/-/boards/11511337'
          '?milestone_title=Sprint+9&label_name[]=PolCo&milestone_title=Sprint+10#/')
SCOPE = {'milestone': {'id': 'gid://gitlab/Milestone/7', 'title': 'Sprint 10'}, 'labels': {'nodes': [{'title': 'PolCo'}]}}


def text(renderable, width=80):
    """What a renderable looks like on screen, as plain text."""
    console = Console(width=width, file=io.StringIO(), color_system=None)
    console.print(renderable)
    return console.file.getvalue()


# What a board URL means

def test_sample_url_is_read_like_gitlab_reads_it():
    found = target.parse(SAMPLE)
    assert (found.host, found.kind, found.path, found.board) == ('gitlab.com', 'project', 'geocity/geocity', 11511337)
    # Keys without [] keep their last value on GitLab's board page; the dropped one is remembered to say so.
    assert found.params == {'milestone_title': 'Sprint 10', 'label_name': ['PolCo']}
    assert found.repeated == {'milestone_title': ['Sprint 9', 'Sprint 10']}
    params, notes = target.scoped(found.params, SCOPE)
    assert notes == []  # the last value is the board's own scope
    assert target.filters(params) == {'milestoneTitle': 'Sprint 10', 'labelName': ['PolCo']}


def test_board_scope_replaces_single_values_and_adds_labels_as_gitlab_does():
    params, notes = target.scoped({'milestone_title': 'Sprint 9', 'label_name': ['UI']},
                                  {**SCOPE, 'weight': -2, 'assignee': {'username': 'sam'},
                                   'iteration': {'id': 'gid://gitlab/Iteration/-4'},
                                   'iterationCadence': {'id': 'gid://gitlab/Iterations::Cadence/3'}})
    assert target.filters(params) == {
        'milestoneTitle': 'Sprint 10', 'labelName': ['UI', 'PolCo'], 'weight': 'None', 'assigneeUsername': 'sam',
        'iterationWildcardId': 'CURRENT', 'iterationCadenceId': 'gid://gitlab/Iterations::Cadence/3'}
    assert len(notes) == 1 and 'Sprint 9' in notes[0] and 'Sprint 10' in notes[0]
    assert target.scoped({}, {'weight': -1, 'milestone': None})[0] == {}  # "any weight" is no filter


def test_project_group_and_nested_board_urls():
    group = target.parse('https://GitLab.example.com:8443/groups/a/b.c/-/boards/7?group_by=epic#/')
    assert (group.host, group.kind, group.path, group.board, group.key) == (
        'gitlab.example.com:8443', 'group', 'a/b.c', 7, 'groups/a/b.c/-/boards/7')
    assert target.filters(group.params) == {}  # group_by chooses swimlanes, it is no filter
    nested = target.parse('https://gitlab.com/a/b/c/project_1/-/boards/')
    assert (nested.kind, nested.path, nested.board) == ('project', 'a/b/c/project_1', None)
    plain_url = target.parse('https://gitlab.com/geocity')
    assert (plain_url.kind, plain_url.path) == ('', 'geocity')  # project or group: GitLab is asked
    bare = target.parse('team/app/-/boards/3', 'git.example.org')
    assert (bare.host, bare.key) == ('git.example.org', 'team/app/-/boards/3')
    for bad in ('ftp://gitlab.com/a/-/boards/1', 'https://me:pw@gitlab.com/a/-/boards/1', 'https://gitlab.com/a/-/issues/1',
                'https://gitlab.com/a/-/boards/x', 'https://gitlab.com/', 'https://gitlab.com/a/../b/-/boards/1',
                'https://gitlab.com/a%2Fb/-/boards/1', 'https://gitlab.com/a/-/boards/1?label_name=%E0%A4%A'):
        with pytest.raises(target.TargetError):
            target.parse(bad)


def test_filters_decode_negate_union_and_use_wildcards_like_the_board_page():
    found = target.parse('https://gitlab.com/g/p/-/boards/1?label_name%5B%5D=needs+review&label_name[]=a%26b'
                         '&not[label_name][]=bug&or[label_name][]=x&or[label_name][]=y&milestone_title=None'
                         '&assignee_username=Any&confidential=yes&type[]=2&epic_id=5&iteration_id=Current'
                         '&search=map&broken&weight=3')
    assert target.filters(found.params) == {
        'labelName': ['needs review', 'a&b'], 'not': {'labelName': ['bug']}, 'or': {'labelNames': ['x', 'y']},
        'milestoneWildcardId': 'NONE', 'assigneeWildcardId': 'ANY', 'confidential': True,
        'workItemTypeIds': ['gid://gitlab/WorkItems::Type/2'], 'epicId': 'gid://gitlab/Epic/5',
        'iterationWildcardId': 'CURRENT', 'search': 'map', 'weight': '3'}
    assert target.describe({'milestoneTitle': 'Sprint 10', 'labelName': ['PolCo'], 'not': {'labelName': ['bug']}}) == \
        'milestone Sprint 10 · label PolCo · not label bug'


def test_filters_gitlab_would_drop_are_refused_by_name():
    found = target.parse('https://gitlab.com/g/p/-/boards/1?custom-field[1]=2&not[search]=x&or[milestone_title][]=a'
                         '&iteration_title=I&utm_source=mail&label_name[]=ok')
    with pytest.raises(target.TargetError) as error:
        target.filters(found.params)
    for name in ('custom-field[1]', 'not[search]', 'or[milestone_title]', 'iteration_title', 'utm_source'):
        assert name in str(error.value)
    assert 'label_name' not in str(error.value)


def test_graphql_literals_keep_values_inside_their_strings():
    hostile = 'a"} ) { x } mutation { destroy(input: {id: 1}) } # :repo @/etc/passwd \\ \n é'
    assert json.loads(literal(hostile)) == hostile
    assert literal({'projectPath': 'a/b', 'ids': [1, None, True], 'mode': Enum('REPLACE')}) == \
        '{projectPath: "a/b", ids: [1, null, true], mode: REPLACE}'
    for bad in (Enum('NONE }'), {'bad key': 1}, object()):
        with pytest.raises(GlabError):
            literal(bad)
    marked = board_input({'milestoneWildcardId': 'NONE', 'not': {'milestoneWildcardId': 'ANY'}, 'labelName': ['NONE']})
    assert literal(marked) == '{milestoneWildcardId: NONE, labelName: ["NONE"], not: {milestoneWildcardId: ANY}}'


# Running glab

FAKE = r'''#!{python}
import json, os, sys, time
data = sys.stdin.read()
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write(json.dumps({{'argv': sys.argv[1:], 'stdin': data, 'cwd': os.getcwd(), 'pid': os.getpid(),
                          'env': {{k: os.environ.get(k) for k in ('GLAB_CHECK_UPDATE', 'GLAB_NO_PROMPT', 'GLAB_SEND_TELEMETRY',
                                                                  'GLAB_DEBUG_HTTP', 'NO_COLOR')}}}}) + '\n')
mode = os.environ.get('FAKE_MODE', 'ok')
if mode == 'sleep':
    time.sleep(30)
if mode == 'pages':
    endpoint = sys.argv[sys.argv.index('GET') + 1]
    query = dict(p.split('=') for p in endpoint.split('?')[1].split('&'))
    total, per, page = int(os.environ['FAKE_TOTAL']), int(query['per_page']), int(query['page'])
    print(json.dumps([{{'n': n}} for n in range((page - 1) * per, min(total, page * per))]))
    sys.exit(0)
sys.stdout.write(os.environ.get('FAKE_OUT', ''))
sys.stderr.write(os.environ.get('FAKE_ERR', ''))
sys.exit(int(os.environ.get('FAKE_EXIT', '0')))
'''


@pytest.fixture
def fake_glab(tmp_path, monkeypatch, executable_script):
    script = executable_script(tmp_path / 'glab', FAKE.format(python=sys.executable))
    log = tmp_path / 'log.jsonl'
    monkeypatch.setenv('FAKE_LOG', str(log))
    monkeypatch.setenv('GLAB_DEBUG_HTTP', '1')  # would print request headers; must never reach glab

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return Glab('gitlab.example.com', binary=str(script)), calls


async def test_glab_reads_are_gets_and_graphql_travels_on_stdin(fake_glab, monkeypatch):
    runner, calls = fake_glab
    monkeypatch.setenv('FAKE_OUT', '{"data": {"currentUser": {"username": "alex"}}}')
    document = 'query { project(fullPath: "a/b:repo") { id } }'
    assert await runner.graphql(document) == {'currentUser': {'username': 'alex'}}
    call = calls()[-1]
    assert call['argv'] == ['api', 'graphql', '--hostname', 'gitlab.example.com', '-F', 'query=@-']
    assert call['stdin'] == document and call['cwd'] == os.path.abspath(os.sep)
    assert call['env'] == {'GLAB_CHECK_UPDATE': 'false', 'GLAB_NO_PROMPT': 'true', 'GLAB_SEND_TELEMETRY': 'false',
                           'GLAB_DEBUG_HTTP': None, 'NO_COLOR': '1'}
    monkeypatch.setenv('FAKE_OUT', '[]')
    await runner.get('projects/a%2Fb/issues/3/notes', sort='desc', per_page=100)
    assert calls()[-1]['argv'] == ['api', '--hostname', 'gitlab.example.com', '--method', 'GET',
                                   'projects/a%2Fb/issues/3/notes?sort=desc&per_page=100']
    await runner.post('projects/a%2Fb/issues/3/notes', {'body': 'hi "there"'})
    call = calls()[-1]
    assert call['argv'][3:] == ['--method', 'POST', 'projects/a%2Fb/issues/3/notes', '--input', '-',
                                '--header', 'Content-Type: application/json']
    assert json.loads(call['stdin']) == {'body': 'hi "there"'}
    count = len(calls())
    for unsafe in ('projects/:id/issues', 'projects/a b'):
        with pytest.raises(GlabError, match='unsafe'):
            await runner.get(unsafe)
    assert len(calls()) == count  # refused before glab ran


async def test_glab_errors_say_what_happened_without_secrets(fake_glab, monkeypatch):
    runner, _ = fake_glab
    monkeypatch.setenv('FAKE_EXIT', '1')
    monkeypatch.setenv('FAKE_OUT', '{"message":"404 Project Not Found"}')
    monkeypatch.setenv('FAKE_ERR', 'DEPRECATION WARNING: x\nglab: 404 Project Not Found (HTTP 404)\n')
    with pytest.raises(GlabError, match='404 Project Not Found') as error:
        await runner.get('projects/a%2Fb')
    assert error.value.status == 404
    monkeypatch.setenv('FAKE_OUT', '{"errors":[{"message":"Field \'x\' doesn\'t exist on type \'Issue\'"}]}')
    monkeypatch.setenv('FAKE_ERR', 'glab: Field ...')
    with pytest.raises(GlabError, match="doesn't exist on type"):
        await runner.graphql('query { x }')
    monkeypatch.setenv('FAKE_OUT', '')
    monkeypatch.setenv('FAKE_ERR', 'glab: 401 Unauthorized (HTTP 401) token glpat-abcdefghijklmnopqrstuvwx')
    with pytest.raises(GlabError, match='glab auth login --hostname gitlab.example.com') as error:
        await runner.get('user')
    assert error.value.status == 401 and 'glpat-' not in str(error.value)
    monkeypatch.setenv('FAKE_ERR', 'glab: forbidden for Bearer glpat-abcdefghijklmnopqrstuvwx (HTTP 403)')
    with pytest.raises(GlabError, match='HTTP 403') as error:
        await runner.get('user')
    assert 'glpat-' not in str(error.value) and '[redacted]' in str(error.value)
    monkeypatch.setenv('FAKE_EXIT', '0')
    monkeypatch.setenv('FAKE_OUT', 'not json')
    with pytest.raises(GlabError, match='JSON'):
        await runner.get('user')
    with pytest.raises(GlabError, match='brew install glab'):
        await Glab('gitlab.example.com', binary='/nonexistent/glab').get('user')
    with pytest.raises(GlabError):
        Glab('gitlab.com --method POST')


async def test_cancelled_or_slow_glab_is_stopped(fake_glab, monkeypatch, process_exists):
    runner, calls = fake_glab
    monkeypatch.setenv('FAKE_MODE', 'sleep')

    async def started():
        while not calls():
            await asyncio.sleep(0.05)
        return calls()[-1]['pid']

    task = asyncio.create_task(runner.get('user'))
    pid = await asyncio.wait_for(started(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not process_exists(pid)
    monkeypatch.setattr(glab, 'TIMEOUT', 0.5)
    with pytest.raises(GlabError, match='did not answer'):
        await runner.get('user')
    assert not process_exists(calls()[-1]['pid'])


async def test_rest_pages_stop_at_a_short_page_and_at_the_cap(fake_glab, monkeypatch):
    runner, calls = fake_glab
    monkeypatch.setenv('FAKE_MODE', 'pages')
    monkeypatch.setenv('FAKE_TOTAL', '250')
    items, more = await runner.pages('projects/1/members/all', cap=5)
    assert (len(items), more, len(calls())) == (250, False, 3)
    monkeypatch.setenv('FAKE_TOTAL', '700')
    items, more = await runner.pages('projects/1/members/all', cap=5)
    assert (len(items), more) == (500, True)


# Client requests

class FakeGlab:
    host = 'gitlab.example.com'

    def __init__(self, *answers):
        self.answers, self.documents, self.posts, self.reads = list(answers), [], [], []

    async def graphql(self, document):
        self.documents.append(document)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def pages(self, path, cap, **params):
        self.reads.append((path, cap, params))
        return self.answers.pop(0), False

    async def post(self, path, payload):
        self.posts.append((path, payload))


ISSUE = {'id': 'gid://gitlab/Issue/9', 'iid': '3', 'reference': 'grp/sub/app#3', 'labels': ['Doing', 'backend'],
         'assignees': [{'username': 'alex'}], 'milestone': None, 'iteration': None}
BOARD = {'id': 'gid://gitlab/Board/1', 'name': 'Dev'}
LISTS = [{'id': 'gid://gitlab/List/11', 'title': 'Open', 'listType': 'backlog', 'label': None},
         {'id': 'gid://gitlab/List/13', 'title': 'Doing', 'listType': 'label', 'label': 'Doing'},
         {'id': 'gid://gitlab/List/14', 'title': 'Review', 'listType': 'label', 'label': 'Review'},
         {'id': 'gid://gitlab/List/15', 'title': 'Closed', 'listType': 'closed', 'label': None},
         {'id': 'gid://gitlab/List/16', 'title': 'Sam', 'listType': 'assignee', 'label': None, 'assignee': {'username': 'sam'}},
         {'id': 'gid://gitlab/List/17', 'title': 'Sprint 11', 'listType': 'milestone', 'label': None, 'milestone': 'Sprint 11'},
         {'id': 'gid://gitlab/List/18', 'title': 'In progress', 'listType': 'status', 'label': None}]


async def test_move_sends_gitlabs_board_mutation_for_the_list_the_card_is_in():
    fake = FakeGlab({'issueMoveList': {'errors': []}}, {'issueMoveList': {'errors': ['Not authorized to assign']}})
    client = Client('gitlab.example.com', fake)
    await client.move(BOARD, ISSUE, LISTS[1], LISTS[2])
    assert 'issueMoveList(input: {projectPath: "grp/sub/app", iid: "3", boardId: "gid://gitlab/Board/1", ' \
           'fromListId: "13", toListId: "14"})' in fake.documents[0]
    with pytest.raises(GlabError, match='Not authorized'):
        await client.move(BOARD, ISSUE, LISTS[1], LISTS[4])


async def test_assign_replaces_the_assignees_and_checks_what_gitlab_kept():
    kept = {'issueSetAssignees': {'errors': [], 'issue': {'assignees': {'nodes': [{'username': 'sam'}]}}}}
    fake = FakeGlab(kept, kept)
    client = Client('gitlab.example.com', fake)
    await client.assign(ISSUE, ['sam'])
    assert 'assigneeUsernames: ["sam"], operationMode: REPLACE' in fake.documents[0]
    with pytest.raises(GlabError, match='kept the assignees sam'):
        await client.assign(ISSUE, [])


async def test_comments_post_json_and_never_send_quick_actions():
    fake = FakeGlab()
    client = Client('gitlab.example.com', fake)
    await client.comment(ISSUE, '  Looks good: /path/to/file is fine  ')
    assert fake.posts == [('projects/grp%2Fsub%2Fapp/issues/3/notes', {'body': 'Looks good: /path/to/file is fine'})]
    for text in ('/close', 'ok\n  /assign @kim', '   '):
        with pytest.raises(GlabError):
            await client.comment(ISSUE, text)
    assert len(fake.posts) == 1


async def test_reading_a_board_carries_its_filters_and_adapts_to_what_the_server_offers():
    lists = {'ns': {'board': {'lists': {'nodes': [{
        'id': 'gid://gitlab/List/13', 'title': 'Doing', 'listType': 'label', 'label': {'title': 'Doing'}, 'issuesCount': 120,
        'issues': {'pageInfo': {'hasNextPage': True, 'endCursor': 'c1'}, 'nodes': [{
            'id': 'gid://gitlab/Issue/9', 'iid': '3', 'title': 'T', 'reference': 'grp/app#3', 'labels': {'nodes': [{'title': 'Doing'}]},
            'assignees': {'nodes': []}, 'userPermissions': {'adminIssue': True, 'createNote': False}}]}}]}}}}
    schema = GlabError("Field 'weight' doesn't exist on type 'Issue'")
    fake = FakeGlab(schema, lists, lists)
    client = Client('gitlab.example.com', fake)
    data = await client.lists({'kind': 'group', 'path': 'grp'}, BOARD, {'labelName': ['PolCo'], 'milestoneWildcardId': 'NONE'})
    assert 'group(fullPath: "grp")' in fake.documents[0] and 'weight' in fake.documents[0]
    assert 'weight' not in fake.documents[1] and not client.ee  # a GitLab without EE fields: core fields only
    assert 'filters: {labelName: ["PolCo"], milestoneWildcardId: NONE}' in fake.documents[1]
    assert 'lists(issueFilters: {labelName: ["PolCo"], milestoneWildcardId: NONE})' in fake.documents[1]
    [lst] = data['lists']
    assert (lst['more'], lst['cursor'], lst['label'], lst['issuesCount']) == (True, 'c1', 'Doing', 120)
    assert lst['issues'][0]['listId'] == 'gid://gitlab/List/13' and lst['issues'][0]['labels'] == ['Doing']
    assert (lst['issues'][0]['canAdmin'], lst['issues'][0]['canComment']) == (True, False)
    lanes = FakeGlab(GlabError('The resource does not exist or you do not have permission'), lists)
    data = await Client('gitlab.example.com', lanes).lists({'kind': 'project', 'path': 'grp/app'}, BOARD, {}, epics=True)
    assert 'epics(first: 50' in lanes.documents[0] and 'epics(' not in lanes.documents[1]
    assert data['epics'] is None and 'permission' in data['lanes_error']


async def test_notes_are_newest_first_comments_and_members_are_active_people():
    fake = FakeGlab([{'id': 2, 'body': 'b'}, {'id': 1, 'body': 'changed the label', 'system': True}],
                    [{'username': 'a', 'state': 'active', 'access_level': 30}, {'username': 'b', 'state': 'blocked',
                                                                                'access_level': 30},
                     {'username': 'c', 'state': 'active', 'access_level': 5}])
    client = Client('gitlab.example.com', fake)
    assert [n['id'] for n in await client.notes(ISSUE)] == [2]
    assert fake.reads[0] == ('projects/grp%2Fsub%2Fapp/issues/3/notes', 20,
                             {'sort': 'desc', 'order_by': 'created_at', 'activity_filter': 'only_comments'})
    people, more = await client.members(ISSUE)
    assert [p['username'] for p in people] == ['a'] and fake.reads[1][0] == 'projects/grp%2Fsub%2Fapp/members/all'


def test_move_effects_follow_gitlab_board_rules():
    issue = {**ISSUE, 'labels': ['Doing', 'Review', 'backend'], 'milestone': 'Sprint 11'}
    between = move_effects(LISTS, issue, LISTS[1], LISTS[2])
    assert (between['add'], between['remove'], between['state']) == ([], ['Doing'], None)  # only the list it leaves
    closing = move_effects(LISTS, issue, LISTS[1], LISTS[3])
    assert (closing['state'], closing['remove']) == ('close', ['Doing', 'Review'])  # every list label, not backend
    assert describe_move(closing) == 'closes it, removes ~Doing, removes ~Review'
    reopening = move_effects(LISTS, {**issue, 'labels': ['backend']}, LISTS[3], LISTS[1])
    assert (reopening['state'], reopening['add']) == ('reopen', ['Doing'])
    to_person = move_effects(LISTS, issue, LISTS[1], LISTS[4])
    assert (to_person['assign'], to_person['remove']) == (['sam'], [])  # other list types keep the labels
    assert move_effects(LISTS, {**issue, 'assignees': [{'username': 'sam'}]}, LISTS[4], LISTS[0])['unassign'] == ['sam']
    assert move_effects(LISTS, issue, LISTS[5], LISTS[0])['milestone'] is None
    assert move_effects(LISTS, issue, LISTS[1], LISTS[6])['status'] == 'In progress'
    scoped = [{'id': 'l1', 'title': 'w::doing', 'listType': 'label', 'label': 'w::doing'},
              {'id': 'l2', 'title': 'w::review', 'listType': 'label', 'label': 'w::review'}]
    effects = move_effects(scoped, {**issue, 'labels': ['w::doing', 'w::todo', 'x::y']}, scoped[0], scoped[1])
    assert sorted(effects['remove']) == ['w::doing', 'w::todo'] and effects['add'] == ['w::review']


# The board in the terminal

async def settle(app, pilot):
    for _ in range(4):
        await pilot.pause()
        await app.workers.wait_for_complete()
    await pilot.pause()


def demo_app(url=None, client=None, prefs=None):
    client = client or DemoClient()
    state = demo_state()
    start = target.parse(url or state['project'], client.host)
    return GitLabApp(client, prefs or Prefs(None, client.host, state), start=start), client


def issue(client, reference):
    return next(i for i in client.issues if i['reference'] == reference)


def titles(app):
    return [str(c.border_title) for c in app.query(Column)]


async def test_demo_opens_the_recent_board_with_its_lists_and_shared_cards():
    app, client = demo_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        assert titles(app) == ['Open · 100 of 122', 'To Do · 2', 'Doing · 2', 'Review · 2', 'Closed · 2']
        assert '1 list shows the first 100' in str(app.query_one('#status', Static).content)
        cells = {str(c.border_title).split(' ·')[0]: [card.issue['iid'] for card in c.children] for c in app.query(Column)}
        assert '3' in cells['Doing'] and '3' in cells['Review']  # one issue, two label lists, as on GitLab
        await pilot.press('right', 'right', 'right')
        assert app.selected['iid'] == '3' and app.selected['listId'] == 'gid://gitlab/List/14'
        await pilot.press('m')
        picker = app.screen
        assert isinstance(picker, Picker)
        current = [c for c in picker.shown if c[3]]
        assert [c[0] for c in current] == ['gid://gitlab/List/14']
        closing = next(c for c in picker.shown if c[0] == 'gid://gitlab/List/15')
        assert 'closes it, removes ~Doing, removes ~Review' in str(closing[1])
        await pilot.press(*'closed', 'enter')
        await settle(app, pilot)
        moved = issue(client, 'demo/portal#3')
        assert moved['state'] == 'closed' and moved['labels'] == ['backend', 'Permits']
        assert app.selected['iid'] == '3' and app.selected['listId'] == 'gid://gitlab/List/15'
        assert titles(app)[2:] == ['Doing · 1', 'Review · 1', 'Closed · 3']


async def test_board_url_filters_and_scope_are_shown_and_repeated_values_explained():
    app, _ = demo_app('https://gitlab.example.com/demo/portal/-/boards/2'
                      '?milestone_title=Sprint+9&label_name[]=Permits&milestone_title=Sprint+10')
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        scope = app.query_one('#scope', Static)
        assert str(scope.content).startswith('Board filters: milestone Sprint 10 · label Permits  ⚠ The URL gives milestone_title 2 times')
        assert scope.has_class('warning')
        assert any('uses only the last, “Sprint 10”' in note for note in app.board_data['notes'])
        assert titles(app) == ['Open · 1', 'Ready · 1', 'In progress · 1 · max 2', 'Dev review · 1',
                               'Customer review · 0', 'Closed · 1']
        await pilot.press('b')
        await pilot.press('escape')
        app.board_picked('demo/portal/-/boards/1')  # another board: the URL filters stay with the board they came with
        await settle(app, pilot)
        assert not app.query_one('#scope', Static).display and titles(app)[0] == 'Open · 100 of 122'


async def test_epic_swimlanes_follow_gitlab_order_and_keys_cross_lanes():
    app, _ = demo_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        await pilot.press('e')
        await settle(app, pilot)
        lanes = [str(label.content) for label in app.query('#board > .lane')]
        assert lanes[0].startswith('&1  Online permit requests') and lanes[1].startswith('&2  Accessibility')
        assert lanes[-1].startswith('Issues with no epic assigned')
        assert len(app.query('#heads > Static')) == 5
        assert app.view()['lanes']
        await pilot.press('right', 'right')  # Doing, in the first epic's lane
        assert app.selected['iid'] == '3'
        await pilot.press('down')  # past the lane's last card: the same list in the next lane
        assert app.selected['iid'] == '1' and app.focused.parent is app.query('#board > .lane-row')[1]


async def test_assign_offers_project_members_me_first_and_saves():
    app, client = demo_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press('right', 'a')  # #2, assigned to jordan
        await settle(app, pilot)
        values = [c[0] for c in app.screen.shown]
        assert values == ['alex', 'jordan', 'sam', '']  # me, then people on this board; blocked and minimal access left out
        assert [c[3] for c in app.screen.shown] == [False, True, False, False]
        await pilot.press(*'sam', 'enter')
        await settle(app, pilot)
        assert [a['username'] for a in issue(client, 'demo/portal#2')['assignees']] == ['sam']


async def test_writes_that_gitlab_would_refuse_are_explained_before_sending():
    app, client = demo_app('https://gitlab.example.com/groups/demo/-/boards/3')
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        refs = [card.issue['reference'] for card in app.query(Column).first().children]
        assert app.ref(app.selected) == 'portal#2'  # issues of several projects: the project is part of the reference
        locked = next(i for i in client.issues if not i['canAdmin'] and i['reference'] in refs)
        while app.selected['reference'] != locked['reference']:
            await pilot.press('down')
        await pilot.press('m')
        status = app.query_one('#status', Static)
        assert not isinstance(app.screen, Picker) and 'Planner' in str(status.content) and status.has_class('error')
        await pilot.press('c')
        app.screen.query_one(Input).value = '/close'
        await pilot.press('enter')
        await settle(app, pilot)
        assert 'quick action' in str(status.content) and locked['state'] == 'opened'


async def test_gitlab_text_never_becomes_markup_or_terminal_controls():
    client = DemoClient()
    hostile = 'Fix [bold red]this[/] \x1b]0;pwned\x07 now [unclosed'
    issue(client, 'demo/portal#6').update(title=hostile, labels=['[b]x'])
    app, _ = demo_app(client=client)
    async with app.run_test(size=(100, 30), notifications=True) as pilot:
        await settle(app, pilot)
        card = text(app.query('GitLabCard').first().query_one(Static).content, 90)
        assert 'Fix [bold red]this[/] ]0;pwned now [unclosed' in card and '~[b]x' in card and '\x1b' not in card
        await pilot.press('a')  # the picker title quotes the issue title
        await settle(app, pilot)
        assert '[bold red]' in str(app.screen.query_one('Label').content) and '\x1b' not in str(app.screen.prompt)
        await pilot.press('escape')
        app.notify(f'Moved {hostile}')  # would raise on markup errors if GitLab text were read as markup
        app.status(hostile, error=True)
        assert '\x1b' not in str(app.query_one('#status', Static).content)


async def test_comments_newest_first_marked_internal_and_written_from_the_board():
    client = DemoClient()
    client.threads['demo/portal#3'][0]['body'] = 'Escape \x1b[31mred\x1b[0m and [b]markup[/b]\nsame paragraph'
    app, _ = demo_app(client=client)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        await pilot.press('right', 'right', 'down', 'C')
        await settle(app, pilot)
        assert isinstance(app.screen, NotesScreen)
        shown = [text(w.content, 90) for w in app.screen.query('#thread > .comment')]
        assert [s.split('  ')[0] for s in shown] == ['Alex Morgan', 'Sam Rivera', 'Jordan Lee']
        assert 'internal note' in shown[1] and 'edited' in shown[0]
        assert '\x1b' not in shown[2] and '[31mred[0m' in shown[2] and '[b]markup[/b]' in shown[2]  # inert text
        assert 'markup[/b] same paragraph' in shown[2]  # a single newline stays in the paragraph on GitLab
        await pilot.press('c')
        app.screen.query_one(Input).value = 'Checked in the terminal'
        await pilot.press('enter')
        await settle(app, pilot)
        assert client.threads['demo/portal#3'][-1]['body'] == 'Checked in the terminal'


async def test_expanded_details_show_the_notes_and_the_list_screen_too():
    client = DemoClient()
    reads = []
    original = client.notes

    async def notes(issue):
        reads.append(issue['reference'])
        return await original(issue)
    client.notes = notes
    client.threads['demo/portal#3'][0]['body'] = 'Escape \x1b[31mred\x1b[0m and [b]markup[/b]'
    app, _ = demo_app(client=client)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press('right', 'right', 'down')
        assert reads == []
        await pilot.press('enter')
        await settle(app, pilot)
        shown = text(app.query_one('#details', Static).content, 78)
        assert reads == ['demo/portal#3'] and 'Comments · 3, newest first' in shown
        assert 'internal note, for project members only' in shown and '\x1b' not in shown and '[b]markup[/b]' in shown
        assert app.query_one('#details-pane').max_scroll_y > 0
        await pilot.press('escape', 'B', 'enter')  # the full list's details expand with the notes as well
        await settle(app, pilot)
        assert 'Comments ·' in text(app.screen.query_one('#details', Static).content, 78) and len(reads) == 2


async def test_full_list_pages_in_board_order_reads_only_and_returns_to_the_board():
    app, client = demo_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press('down', 'down')
        selected = app.selected['id']
        await pilot.press('B')
        await settle(app, pilot)
        screen = app.screen
        assert isinstance(screen, ListScreen) and len(screen.issues) == 50
        assert [i['iid'] for i in screen.issues[:2]] == ['6', '8']
        await pilot.press('n')
        await settle(app, pilot)
        await pilot.press('n')
        await settle(app, pilot)
        assert len(screen.issues) == 122 and not screen.more
        assert len({i['id'] for i in screen.issues}) == 122
        await pilot.press('slash', *'map view')
        await settle(app, pilot)
        assert {i['title'] for i in screen.issues if screen.matches(i)} == \
            {f'{a} the map view' for a in ('Polish', 'Speed up', 'Document', 'Add tests for', 'Simplify', 'Translate', 'Cache',
                                           'Log errors in', 'Fix focus in', 'Review', 'Restyle', 'Measure')}
        await pilot.press('enter', 'C')
        await settle(app, pilot)
        await pilot.press('c')  # comments opened from the list only read
        assert isinstance(app.screen, NotesScreen)
        await pilot.press('escape', 'escape')
        assert app.screen is not screen and app.selected['id'] == selected


async def test_auto_refresh_shows_changes_and_keeps_the_selection():
    app, client = demo_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        await pilot.press('right')
        chosen = app.selected['id']
        issue(client, 'demo/portal#4')['title'] = 'Renamed in GitLab'
        app.tick()
        await settle(app, pilot)
        assert any(c.issue['title'] == 'Renamed in GitLab' for c in app.query('GitLabCard'))
        assert app.selected['id'] == chosen and app.focused is app.query(Column)[1]
        cards = list(app.query('GitLabCard'))
        app.tick()
        await settle(app, pilot)
        assert list(app.query('GitLabCard')) == cards  # unchanged data: nothing rebuilt


async def test_details_show_gitlab_markdown_and_the_source():
    app, _ = demo_app()
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(app, pilot)
        await pilot.press('right', 'right')  # #1, the issue with the long description
        assert app.selected['iid'] == '1'
        shown = text(app.details(app.selected), 96)
        assert 'cards, lists and epic swimlanes. This line continues' in shown
        assert '⊘ Drag and drop' in shown and '[image: Board sketch]' in shown and 'graph LR' in shown
        assert 'Note' in shown and '[!note]' not in shown
        await pilot.press('s')
        assert '- [~] Drag and drop' in text(app.details(app.selected), 96)


async def test_small_terminals_keep_the_board_usable():
    for size in ((80, 24), (60, 20)):
        app, _ = demo_app()
        async with app.run_test(size=size) as pilot:
            await settle(app, pilot)
            board = app.query_one('#board')
            assert board.size.height >= size[1] - 10 and app.query_one('#toolbar').size.height == 1
            assert app.query_one('#auto').size.width >= 6 and app.selected
            await pilot.press('m')
            assert app.screen.query_one(OptionList).size.height > 2
            await pilot.press('escape', 'p')
            assert isinstance(app.screen, Picker) and app.screen.shown[-1][0] == '\x00ask'


async def test_places_resume_and_views_are_saved_per_host_and_board(tmp_path):
    path = tmp_path / 'state.json'
    app, _ = demo_app(prefs=Prefs(str(path), 'gitlab.example.com'))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)  # no saved state: the project's first board
        await pilot.press('M', 'e')
        await settle(app, pilot)
        await pilot.press('p')
        group = next(c[0] for c in app.screen.shown if c[0] == 'groups/demo')
        await pilot.press(*'groups', 'enter')
        await settle(app, pilot)
        assert app.place['kind'] == 'group' and app.board_id == 'groups/demo/-/boards/3' and group
    saved = json.loads(path.read_text())['gitlab.example.com']
    assert saved['project'] == 'groups/demo' and set(saved['visited']) == {'demo/portal/-/boards/1', 'groups/demo/-/boards/3'}
    assert saved['views']['demo/portal/-/boards/1'] == {'mine': True, 'compact': False, 'hidden': [], 'auto': True,
                                                       'lanes': True}


async def test_open_dialog_validates_urls_and_the_demo_never_runs_glab(monkeypatch):
    monkeypatch.setattr(Glab, 'run', lambda *a, **k: pytest.fail('the demo ran glab'))
    app, _ = demo_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        await pilot.press('p', *'url', 'enter')  # matches no place, so only the URL entry is left
        assert isinstance(app.screen, AskDialog)
        app.screen.query_one(Input).value = 'https://gitlab.com/geocity/geocity/-/boards/1'
        await pilot.press('enter')
        assert 'this session uses gitlab.example.com' in str(app.query_one('#status', Static).content)
        app.go('demo/portal/-/boards/2?custom-field[3]=1')
        assert 'custom-field[3]' in str(app.query_one('#status', Static).content)
        app.go('https://gitlab.example.com/demo/portal/-/boards/2?label_name[]=frontend')
        await settle(app, pilot)
        assert app.board_id == 'demo/portal/-/boards/2' and 'label frontend' in str(app.query_one('#scope', Static).content)
        app.open_issue({**app.selected, 'webUrl': 'https://evil.example.net/x'}, app.status)
        assert 'Not opening' in str(app.query_one('#status', Static).content)


def test_command_line_guides_setup_and_refuses_bad_urls(monkeypatch, capsys):
    monkeypatch.setattr('shutil.which', lambda name: None)
    monkeypatch.setattr(sys, 'argv', ['gitlab_board', SAMPLE])
    with pytest.raises(SystemExit) as error:
        main()
    assert 'brew install glab' in str(error.value) and 'glab auth login --hostname gitlab.com' in str(error.value)
    monkeypatch.setattr(sys, 'argv', ['gitlab_board', SAMPLE.replace('#/', '&custom-field[1]=4')])
    with pytest.raises(SystemExit, match='custom-field'):
        main()
    monkeypatch.setattr(glab, 'check', lambda host: 'glab 1.115.0')
    monkeypatch.setattr(sys, 'argv', ['gitlab_board', '--auth-status', '--hostname', 'git.example.org'])
    main()
    assert 'glab 1.115.0: signed in to git.example.org' in capsys.readouterr().out
    monkeypatch.setattr(sys, 'argv', ['gitlab_board'])
    with pytest.raises(SystemExit, match='Give a board URL'):
        main()  # nothing saved yet to resume


def test_demo_boards_cover_each_list_type():
    kinds = {lst['listType'] for *_, lists in BOARDS.values() for lst in lists}
    assert kinds == {'backlog', 'label', 'status', 'assignee', 'milestone', 'closed'}
