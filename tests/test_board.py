import asyncio
import io
import json

import httpx
import pytest
from rich.console import Console
from rich.text import Text
from textual.widgets import Input, OptionList, Select, Static

from youtrack_board.app import (BacklogScreen, BoardApp, Card, Column, CommentDialog, CommentsScreen, More, Picker, Row,
                                ViewDialog, main, names)
from youtrack_board.client import ISSUE_FIELDS, Client, YouTrackError, group_issues
from youtrack_board.demo import DemoClient
from youtrack_board.prefs import Prefs


async def settle(app, pilot):
    # Loading chains workers (boards -> board -> issues), so wait more than once.
    for _ in range(3):
        await pilot.pause()
        await app.workers.wait_for_complete()
    await pilot.pause()


def state(client, issue_id):
    return next(i for i in client.data if i['id'] == issue_id)['customFields'][0]['value']['name']


async def test_keyboard_move_comment_and_filter():
    client = DemoClient()
    client.data[2]['customFields'][1]['value'] = {'login': 'jdoe'}
    app = BoardApp(client)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        assert len(app.query(Column)) == 3
        assert app.selected['idReadable'] == 'DEMO-1'
        await pilot.press('right', 'l')
        assert app.selected['idReadable'] == 'DEMO-5'
        await pilot.press('h', 'left', 'j')
        assert app.selected['idReadable'] == 'DEMO-2'
        await pilot.press('m')
        assert isinstance(app.screen, Picker)
        await pilot.press('enter')  # the value after the current one is preselected
        await settle(app, pilot)
        assert state(client, '2') == 'In Progress'
        assert app.selected['idReadable'] == 'DEMO-2'
        assert app.focused is app.query(Column)[1]
        await pilot.press('enter')
        assert app.screen.maximized is app.query_one('#details-pane')
        await pilot.press('escape')
        assert app.screen.maximized is None and app.focused is app.query(Column)[1]
        await pilot.press('c')
        app.screen.query_one(Input).value = 'Verified in the terminal'
        await pilot.press('enter')
        await settle(app, pilot)
        assert client.threads['2'][-1]['text'] == 'Verified in the terminal'
        await pilot.press('slash', 'j', 'd', 'o', 'e')
        assert [c.issue['idReadable'] for c in app.query(Card)] == ['DEMO-3']
        await pilot.press('enter')
        assert app.selected['idReadable'] == 'DEMO-3'
        app.query_one('#filter', Input).value = 'no match'
        await pilot.pause()
        assert len(app.query(Card)) == 0
        assert app.selected is None


async def test_api_pagination_context_path_and_writes():
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == 'POST':
            return httpx.Response(200)
        skip = int(request.url.params.get('$skip', 0))
        return httpx.Response(200, json=[{'id': '1'}] if skip == 0 else [])

    client = Client('https://example.test/youtrack', 'secret', transport=httpx.MockTransport(respond))
    try:
        assert await client.boards() == [{'id': '1'}]
        assert requests[0].url.path == '/youtrack/api/agiles'
        assert requests[1].url.params['$skip'] == '1'
        assert requests[0].headers['Authorization'] == 'Bearer secret'
        await client.issues({'name': 'Team board'}, {'name': 'Sprint 1'})
        assert requests[2].url.params['query'] == '{Board Team board}: {Sprint 1}'
        issue = DemoClient().data[0]
        await client.move(issue, 'State', 'In Progress')
        body = json.loads(requests[-1].content)
        assert body == {'customFields': [{'id': 'state', '$type': 'StateIssueCustomField', 'value': {'name': 'In Progress'}}]}
        await client.comment(issue, '  hello  ')
        assert json.loads(requests[-1].content) == {'text': 'hello'}
        assert requests[-1].url.path.endswith('/issues/1/comments')
    finally:
        await client.close()


async def test_errors_and_unsafe_requests():
    with pytest.raises(YouTrackError):
        Client('http://example.test', 'secret')
    client = Client('https://example.test', 'secret', transport=httpx.MockTransport(
        lambda request: httpx.Response(403, json={'error_description': 'Permission denied secret'})))
    try:
        with pytest.raises(YouTrackError, match='HTTP 403') as error:
            await client.boards()
        assert 'secret' not in str(error.value)
        with pytest.raises(YouTrackError, match='unsupported'):
            await client.issues({'name': 'bad} or #Unresolved'}, {'name': 'sprint'})
        with pytest.raises(YouTrackError, match='empty'):
            await client.comment({'id': '1'}, ' ')
        with pytest.raises(YouTrackError, match='single-value'):
            await client.move({'id': '1', 'customFields': []}, 'State', 'Done')
    finally:
        await client.close()


async def test_board_columns_group_values_and_keep_unmatched():
    demo = DemoClient()
    board = await demo.board('ops')
    board['columnSettings']['columns'][0]['fieldValues'].append({'name': 'In Progress'})
    issues = await demo.issues(board, None)
    issue = issues[0]
    issue['customFields'][0]['value'] = None
    groups = group_issues(board, issues)
    assert [i['idReadable'] for i in groups[0][1]] == ['OPS-2']
    assert groups[-1] == ('Other / no column', [issue])


async def test_write_failure_keeps_error_visible():
    class RejectedClient(DemoClient):
        async def move(self, issue, field_name, value):
            raise YouTrackError('Workflow rejected the move')

    app = BoardApp(RejectedClient())
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        await pilot.press('m', 'enter')
        await settle(app, pilot)
        status = app.query_one('#status', Static)
        assert 'Workflow rejected' in str(status.content) and status.has_class('error')
        assert all(not select.disabled for select in app.query(Select))


async def test_refresh_keeps_selection_and_renders_never_duplicate_columns():
    app = BoardApp(DemoClient())
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        assert app.query_one('#board').size.height >= 12
        await pilot.press('right', 'down', 'r')
        await settle(app, pilot)
        assert app.selected['idReadable'] == 'DEMO-4'
        assert app.focused is app.query(Column)[1]
        # A filter keystroke can land while a load is rendering.
        await asyncio.gather(app.render_board(), app.render_board())
        assert len(app.query(Column)) == 3


async def test_startup_error_can_be_retried():
    class FlakyClient(DemoClient):
        down = True

        async def boards(self):
            if self.down:
                raise YouTrackError('Cannot reach YouTrack.')
            return await super().boards()

    client = FlakyClient()
    app = BoardApp(client)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        assert app.query_one('#status', Static).has_class('error')
        client.down = False
        await pilot.press('r')
        await settle(app, pilot)
        assert len(app.query(Card)) == 8
        assert not app.query_one('#status', Static).has_class('error')


def assignee(client, issue_id):
    value = next(i for i in client.data if i['id'] == issue_id)['customFields'][1]['value']
    return value and value['login']


def picker_values(app):
    return [choice[0] for choice in app.screen.shown]


async def test_assign_offers_project_people_me_first_and_saves():
    client = DemoClient()
    app = BoardApp(client)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press('j', 'a')  # DEMO-2, assigned to jordan
        await settle(app, pilot)
        picker = app.screen
        assert isinstance(picker, Picker)
        logins = [value.get('login') for value in picker_values(app)]
        assert logins == ['alex', 'jordan', 'sam', None]  # me, then people on the board; banned kim is left out
        assert [choice[3] for choice in picker.shown] == [False, True, False, False]  # the current assignee is disabled
        assert picker.shown[picker.query_one(OptionList).highlighted][0]['login'] == 'alex'
        await pilot.press(*'riv', 'enter')
        await settle(app, pilot)
        assert assignee(client, '2') == 'sam'
        assert app.selected['idReadable'] == 'DEMO-2' and names(app.selected, 'Assignee') == 'Sam Rivera'
        await pilot.press('a', *'unass', 'enter')
        await settle(app, pilot)
        assert assignee(client, '2') is None
        await pilot.press('b', *'support', 'enter')  # OPS requires an assignee
        await settle(app, pilot)
        await pilot.press('a')
        await settle(app, pilot)
        assert [value.get('login') for value in picker_values(app)] == ['jordan', 'priya']


async def test_assign_refuses_multi_user_fields_and_shows_load_errors():
    class Forbidden(DemoClient):
        async def assignees(self, issue):
            raise YouTrackError('YouTrack returned HTTP 403.')

    client = Forbidden()
    client.data[0]['customFields'][1]['$type'] = 'MultiUserIssueCustomField'
    app = BoardApp(client)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press('a')
        assert not isinstance(app.screen, Picker)
        assert 'multi-user' in str(app.query_one('#status', Static).content)
        await pilot.press('j', 'a')
        await settle(app, pilot)
        hint = app.screen.query_one('#hint', Static)
        assert 'HTTP 403' in str(hint.content) and hint.has_class('error')
        await pilot.press('enter', 'escape')
        await settle(app, pilot)
        assert not isinstance(app.screen, Picker) and assignee(client, '2') == 'jordan'


async def test_assignee_api_reads_the_field_bundle_and_writes_documented_payloads():
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == 'POST':
            return httpx.Response(200)
        if request.url.path.endswith('/issues/2'):
            return httpx.Response(200, json={'customFields': [{'name': 'Assignee', 'projectCustomField': None}]})
        if request.url.path.endswith('/issues/1'):
            return httpx.Response(200, json={'customFields': [
                {'name': 'State'}, {'name': 'Assignee', 'projectCustomField': {'canBeEmpty': True, 'bundle': {'id': '8-20'}}}]})
        skip = int(request.url.params['$skip'])
        return httpx.Response(200, json=[{'id': '1-1', 'login': 'jane'}] if skip == 0 else [])

    client = Client('https://example.test', 'secret', transport=httpx.MockTransport(respond))
    issue = DemoClient().data[0]
    try:
        settings, users = await client.assignees(issue)
        assert settings['canBeEmpty'] and users == [{'id': '1-1', 'login': 'jane'}]
        assert 'projectCustomField(canBeEmpty,emptyFieldText,bundle(id))' in requests[0].url.params['fields']
        assert requests[1].url.path == '/api/admin/customFieldSettings/bundles/user/8-20/aggregatedUsers'
        assert requests[1].url.params['fields'] == 'id,login,fullName,banned' and requests[2].url.params['$skip'] == '1'
        await client.assign(issue, 'jane')
        assert json.loads(requests[-1].content) == {'customFields': [
            {'id': 'assignee', '$type': 'SingleUserIssueCustomField', 'value': {'login': 'jane'}}]}
        await client.assign(issue, None)
        assert json.loads(requests[-1].content)['customFields'][0]['value'] is None
        sent = len(requests)
        issue['customFields'][1]['$type'] = 'MultiUserIssueCustomField'
        with pytest.raises(YouTrackError, match='single-user'):
            await client.assign(issue, 'jane')
        assert len(requests) == sent
        with pytest.raises(YouTrackError, match='Assignee user list'):
            await client.assignees({**issue, 'id': '2'})
    finally:
        await client.close()


def test_prefs_keep_other_servers_write_atomically_and_survive_bad_files(tmp_path):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps({'https://other': {'project': 'keep'}}))
    prefs = Prefs(str(path), 'https://a')
    prefs.visit('b1')
    prefs.set_view('b1', compact=True)
    prefs.data['project'] = '0-1'
    prefs.save()
    saved = json.loads(path.read_text())
    assert saved['https://other'] == {'project': 'keep'}
    assert saved['https://a']['views']['b1'] == {'mine': False, 'compact': True, 'hidden': [], 'auto': True}
    assert Prefs(str(path), 'https://a').data == prefs.data
    assert [p.name for p in tmp_path.iterdir()] == ['state.json']  # no temp files left behind

    path.write_text(json.dumps({'https://a': {'visited': {'x': 'yesterday', 'y': 5}, 'project': 3, 'views': {
        'b': {'mine': 'yes', 'hidden': ['Done', 7], 'auto': 'no'}, 'c': [], 'd': {'auto': False}}}}))
    assert Prefs(str(path), 'https://a').data == {'visited': {'y': 5}, 'project': None, 'views': {
        'b': {'mine': False, 'compact': False, 'hidden': ['Done'], 'auto': True},  # only an explicit false turns it off
        'c': {'mine': False, 'compact': False, 'hidden': [], 'auto': True},
        'd': {'mine': False, 'compact': False, 'hidden': [], 'auto': False}}}
    path.write_text('{not json')
    broken = Prefs(str(path), 'https://a')
    assert broken.warning and broken.data['visited'] == {}
    assert (tmp_path / 'state.json.corrupt').read_text() == '{not json'
    broken.save()
    assert json.loads(path.read_text()) == {'https://a': broken.data}


def test_demo_never_touches_saved_state(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    monkeypatch.setattr('sys.argv', ['youtrack-board', '--demo'])
    started = []
    monkeypatch.setattr(BoardApp, 'run', lambda self: started.append(self))
    main()
    assert started[0].prefs.path is None and started[0].prefs.data['visited']
    started[0].prefs.save()
    assert not list(tmp_path.iterdir())


async def test_boards_resume_recent_first_and_project_scope_narrows_without_reloading(tmp_path):
    class Counting(DemoClient):
        loads = 0

        async def board(self, board_id):
            self.loads += 1
            return await super().board(board_id)

    path = str(tmp_path / 'state.json')
    client = Counting()
    seed = Prefs(path, client.base_url)
    seed.data['visited'] = {'product': 100, 'ops': 200}
    seed.save()
    app = BoardApp(client, Prefs(path, client.base_url))
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        assert app.board_id == 'ops'  # resumes the most recently opened board
        await pilot.press('b')
        assert picker_values(app) == ['ops', 'product', 'services', 'web'] and app.screen.shown[0][3]
        await pilot.press('enter')  # the previous board is preselected, like Alt+Tab
        await settle(app, pilot)
        assert app.board_id == 'product'
        visited = Prefs(path, client.base_url).data['visited']
        assert visited['product'] > visited['ops']

        loads = client.loads
        await pilot.press('right', 'j', 'j', 'p', *'web', 'enter')  # select WEB-2; Product team spans DEMO and WEB
        await settle(app, pilot)
        assert client.loads == loads and app.board_id == 'product'
        assert {c.issue['project']['shortName'] for c in app.query(Card)} == {'WEB'}
        assert app.selected['idReadable'] == 'WEB-2'
        assert 'WEB only' in str(app.query_one('#status', Static).content)

        await pilot.press('p', *'ops', 'enter')  # not on this board: open its most recent board
        await settle(app, pilot)
        assert app.board_id == 'ops' and str(app.query_one('#project').label) == 'OPS ▾'
        await pilot.press('b', *'zzz')
        assert picker_values(app) == ['*']  # the way out stays visible
        await pilot.press('enter')
        await pilot.pause()
        assert app.prefs.data['project'] is None and picker_values(app) == ['ops', 'product', 'services', 'web']
        await pilot.press('escape', 'p', *'web', 'enter')
        await settle(app, pilot)
        assert app.board_id == 'product'  # WEB's most recently opened board, not the never-opened Website launch

    app = BoardApp(client, Prefs(path, client.base_url))
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        assert app.board_id == 'product' and app.prefs.data['project'] == '0-2'


async def test_view_options_apply_live_per_board_and_persist(tmp_path):
    path = str(tmp_path / 'state.json')
    client = DemoClient()
    app = BoardApp(client, Prefs(path, client.base_url))
    async with app.run_test(size=(60, 20)) as pilot:
        await settle(app, pilot)
        await pilot.press('v')
        assert isinstance(app.screen, ViewDialog)
        await pilot.press('down', 'space', 'down', 'down', 'down', 'space')  # compact cards, hide Done
        await pilot.pause()
        assert [c.border_title.split(' · ')[0] for c in app.query(Column)] == ['Open', 'In Progress']
        assert all(card.has_class('-compact') for card in app.query(Card))
        await pilot.press('escape')
        await pilot.pause()
        assert isinstance(app.focused, Column)
        await pilot.press('M')
        await settle(app, pilot)
        assert {names(c.issue, 'Assignee') for c in app.query(Card)} == {'Alex Morgan'}
        assert 'assigned to me' in str(app.query_one('#status', Static).content)
        saved = json.loads(open(path).read())[client.base_url]['views']
        assert saved == {'product': {'mine': True, 'compact': True, 'hidden': ['Done'], 'auto': True}}
        await pilot.press('b', *'support', 'enter')  # other boards keep their own view
        await settle(app, pilot)
        assert len(app.query(Column)) == 3 and not app.query('Card.-compact')
        await pilot.press('v', 'down', 'down', 'space', 'down', 'space', 'down', 'space', 'escape')
        await pilot.pause()
        assert 'All columns are hidden' in str(app.query_one('#board .empty', Static).content)


async def test_switching_boards_quickly_discards_the_slower_response():
    class Slow(DemoClient):
        release = asyncio.Event()

        async def board(self, board_id):
            if board_id == 'product':
                await self.release.wait()
            return await super().board(board_id)

    client = Slow()
    app = BoardApp(client)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.open_board('ops')
        for _ in range(10):
            await pilot.pause()
        assert app.board_data['id'] == 'ops'
        client.release.set()  # the first board answers last
        for _ in range(10):
            await pilot.pause()
        assert app.board_id == 'ops' and app.board_data['id'] == 'ops'
        assert {c.issue['project']['shortName'] for c in app.query(Card)} == {'OPS'}
        assert 'product' not in app.prefs.data['visited']


async def test_pickers_fit_small_terminals_with_long_lists():
    class Crowded(DemoClient):
        async def assignees(self, issue):
            return {'canBeEmpty': True}, [{'login': f'user{n}', 'fullName': f'Person {n:02}'} for n in range(60)]

    app = BoardApp(Crowded())
    async with app.run_test(size=(60, 20)) as pilot:
        await settle(app, pilot)
        await pilot.press('a')
        await settle(app, pilot)
        dialog = app.screen.query_one('#dialog')
        assert dialog.region.y >= 0 and dialog.region.bottom <= 20
        assert app.screen.query_one('#hint').region.bottom <= dialog.region.bottom
        assert len(app.screen.shown) == 62  # the current assignee, 60 people, Unassigned
        await pilot.press(*'person 42')
        assert [c[0]['login'] for c in app.screen.shown] == ['user42']


async def test_a_key_right_after_moving_the_cursor_acts_on_the_new_card():
    app = BoardApp(DemoClient())
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        app.focused.action_cursor_down()
        app.action_assign()  # same tick, before ListView.Highlighted is handled: what fast typing does
        await pilot.pause()
        assert app.screen.prompt.startswith('Assign DEMO-2')


async def test_board_attribute_with_spaces_and_hyphen_is_quoted_as_a_whole():
    queries = []

    def respond(request):
        query = request.url.params['query']
        queries.append(query)
        correct = query == '{Board Parler Parent - Sprints}: {Sprint 5}'
        first = request.url.params['$skip'] == '0'
        return httpx.Response(200, json=[{'id': 'issue-1'}] if correct and first else [])

    client = Client('https://example.test', 'secret', transport=httpx.MockTransport(respond))
    try:
        issues = await client.issues({'name': 'Parler Parent - Sprints'}, {'name': 'Sprint 5'})
        assert issues == [{'id': 'issue-1'}]
        assert all(q == '{Board Parler Parent - Sprints}: {Sprint 5}' for q in queries)
    finally:
        await client.close()


def picker_labels(app):
    return [str(choice[1]) for choice in app.screen.shown]


def highlighted(app):
    return app.screen.shown[app.screen.query_one(OptionList).highlighted][0]


async def test_workflow_state_offers_youtrack_transitions_and_applies_the_chosen_one():
    client = DemoClient()  # WEB's State is a state-machine field; DEMO's is a plain State field
    app = BoardApp(client)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        await pilot.press('slash', *'pricing', 'enter', 'm')  # WEB-2, in Review
        await settle(app, pilot)
        assert isinstance(app.screen, Picker)
        assert [c[0] and c[0][1]['id'] for c in app.screen.shown] == ['request changes', 'approve', None]
        assert picker_labels(app)[:2] == ['→ In Progress  request changes', '→ Done  approve']
        assert 'condition' in picker_labels(app)[2]  # YouTrack can still refuse a listed transition
        assert highlighted(app)[1]['id'] == 'approve'  # the transition to the next column, not just the first one
        await pilot.press('enter')
        await settle(app, pilot)
        assert state(client, '7') == 'Done'
        assert app.selected['idReadable'] == 'WEB-2' and names(app.selected, 'State') == 'Done'
        app.query_one('#filter', Input).value = 'landing'  # WEB-1: Open and unassigned
        await settle(app, pilot)
        await pilot.press('m', 'enter')  # the demo workflow only starts assigned issues, like a YouTrack guard
        await settle(app, pilot)
        status = app.query_one('#status', Static)
        assert 'kept WEB-1 in Open' in str(status.content) and status.has_class('error')
        assert state(client, '6') == 'Open'


async def test_workflow_transitions_reflect_youtrack_now_and_explain_when_there_are_none():
    class Flaky(DemoClient):
        down = False

        async def transitions(self, issue, field_name):
            if self.down:
                raise YouTrackError('YouTrack returned HTTP 403.')
            return await super().transitions(issue, field_name)

    client = Flaky()
    app = BoardApp(client)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        await pilot.press('slash', *'accessib', 'enter')  # WEB-3, In Progress on the board
        client.data[7]['customFields'][0]['value'] = {'name': 'Done'}  # someone else finished it meanwhile
        await pilot.press('m')
        await settle(app, pilot)
        assert [c[0] and c[0][1]['id'] for c in app.screen.shown] == ['reopen', None]
        assert 'Now Done in YouTrack (board showed In Progress)' in app.screen.query_one(Input).placeholder
        await pilot.press('escape')
        client.data[7]['customFields'][0]['value'] = {'name': 'Archived'}
        await pilot.press('m')
        await settle(app, pilot)
        hint = app.screen.query_one('#hint', Static)
        assert 'no transitions from Archived' in str(hint.content) and hint.has_class('error')
        await pilot.press('enter', 'escape')
        client.down = True
        await pilot.press('m')
        await settle(app, pilot)
        assert 'HTTP 403' in str(app.screen.query_one('#hint', Static).content)
        await pilot.press('escape')
        assert state(client, '8') == 'Archived'


async def test_move_explains_fields_it_cannot_change():
    client = DemoClient()
    client.data[0]['customFields'][0]['$type'] = 'MultiEnumIssueCustomField'
    del client.data[1]['customFields'][0]
    app = BoardApp(client)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        await pilot.press('m')
        status = str(app.query_one('#status', Static).content)
        assert not isinstance(app.screen, Picker) and 'State is a MultiEnum field' in status
        app.query_one('#filter', Input).value = 'onboarding'  # DEMO-2 has no State field at all
        await settle(app, pilot)
        await pilot.press('m')
        assert 'has no State field' in str(app.query_one('#status', Static).content)


async def test_transition_api_uses_the_documented_event_payload():
    requests = []
    field = {'id': '134-10829', 'name': 'State', '$type': 'StateMachineIssueCustomField', 'value': {'name': 'Ready'},
             'possibleEvents': [{'id': 'start', 'presentation': 'In Progress', '$type': 'Event'}]}

    def respond(request):
        requests.append(request)
        if request.method == 'GET':
            return httpx.Response(200, json=field)
        return httpx.Response(200, json={'value': {'name': 'In Progress'}, '$type': 'StateMachineIssueCustomField'})

    client = Client('https://example.test', 'secret', transport=httpx.MockTransport(respond))
    issue = {'id': '2-78', 'idReadable': 'PP-78', 'customFields': [{'id': '134-10829', 'name': 'State'}]}
    try:
        fresh = await client.transitions(issue, 'State')
        assert fresh == field
        assert requests[0].url.path == '/api/issues/2-78/fields/134-10829'
        assert 'possibleEvents(id,presentation)' in requests[0].url.params['fields']
        assert await client.transition(issue, fresh, fresh['possibleEvents'][0]) == 'In Progress'
        assert requests[1].method == 'POST' and requests[1].url.path == '/api/issues/2-78/fields/134-10829'
        assert json.loads(requests[1].content) == {'id': '134-10829', '$type': 'StateMachineIssueCustomField',
                                                   'event': {'id': 'start', 'presentation': 'In Progress', '$type': 'Event'}}
    finally:
        await client.close()


def rendered(renderable, width=400):
    """What a widget's content looks like on screen, as plain text."""
    console = Console(width=width, file=io.StringIO(), color_system=None)
    console.print(Text(renderable) if isinstance(renderable, str) else renderable)
    return '\n'.join(line.rstrip() for line in console.file.getvalue().rstrip().splitlines())


def thread(app):
    return [rendered(widget.content) for widget in app.screen.query('#thread > Static')]


async def test_comments_read_newest_first_safely_and_return_to_the_card():
    client = DemoClient()
    client.threads['3'][0]['text'] += '\x1b[2J\x9b31m'  # terminal escapes from YouTrack must not reach the terminal
    client.threads['3'][1]['author']['fullName'] = 'Sam *Rivera* [b]'  # names are not Markdown
    app = BoardApp(client)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press('right', 'C')  # DEMO-3
        await settle(app, pilot)
        assert isinstance(app.screen, CommentsScreen)
        newest, private, oldest = thread(app)
        assert newest.startswith('Alex Morgan') and 'edited' in newest.splitlines()[0]
        assert 'Markdown is formatted' in newest and '**' not in newest and '[b]tags[/b] stays literal' in newest
        assert '☑ Read comments' in newest and 'the guide (https://example.com/guide)' in newest
        assert private.splitlines()[0].startswith('Sam *Rivera* [b]') and 'visible to Developers' in private.splitlines()[0]
        await pilot.press('s')  # the source, as typed, for anything the formatting gets wrong
        assert '**Markdown** is formatted' in thread(app)[0] and '* [x] Read comments' in thread(app)[0]
        await pilot.press('s')
        assert '**' not in thread(app)[0]
        # Multiline text keeps its lines; the escape characters are dropped, so what is left is inert text.
        assert oldest.splitlines()[1:] == ['Token auth works against the staging instance.',
                                           'Next step: let people pick a sprint.[2J31m']
        assert '3 comments, newest first' in str(app.screen.query_one('#hint', Static).content)
        assert app.focused is app.screen.query_one('#thread')  # arrows and PgUp/PgDn scroll the thread
        await pilot.press('escape')
        assert app.focused is app.query(Column)[1] and app.selected['idReadable'] == 'DEMO-3'
        await pilot.press('C', 'c')  # c in the reader hands over to the comment dialog for the same card
        assert isinstance(app.screen, CommentDialog) and app.screen.prompt == 'Comment on DEMO-3'
        await pilot.press(*'Seen', 'enter')
        await settle(app, pilot)
        await pilot.press('C')
        await settle(app, pilot)
        assert len(thread(app)) == 4 and thread(app)[0].endswith('\nSeen')


async def test_comments_show_errors_with_retry_empty_threads_and_drop_late_answers():
    class Flaky(DemoClient):
        fail, hold = 1, asyncio.Event()

        async def comments(self, issue):
            if self.fail:
                self.fail -= 1
                raise YouTrackError('YouTrack returned HTTP 500.')
            if issue['id'] == '3':
                await self.hold.wait()
            return await super().comments(issue)

    client = Flaky()
    app = BoardApp(client)
    async with app.run_test(size=(60, 20)) as pilot:
        await settle(app, pilot)
        await pilot.press('right', 'C')
        await settle(app, pilot)
        assert 'HTTP 500' in thread(app)[0] and app.screen.query_one('#hint').has_class('error')
        await pilot.press('r')
        await pilot.pause()
        assert thread(app) == ['Loading comments…']  # DEMO-3's answer is held back
        await pilot.press('escape', 'j', 'C')  # close before it arrives and read DEMO-4 instead
        await settle(app, pilot)
        assert app.screen.issue['idReadable'] == 'DEMO-4' and 'No comments yet' in thread(app)[0]
        client.hold.set()
        await settle(app, pilot)
        assert app.screen.issue['idReadable'] == 'DEMO-4' and len(thread(app)) == 1


async def test_comments_api_reads_every_page_and_skips_deleted_comments():
    requests = []
    pages = [[{'id': 'a', 'created': 1, 'text': 'first'}, {'id': 'b', 'created': 3, 'deleted': True}],
             [{'id': 'c', 'created': 2, 'text': 'second'}], []]

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=pages[len(requests) - 1])

    client = Client('https://example.test', 'secret', transport=httpx.MockTransport(respond))
    try:
        comments = await client.comments({'id': '2-78'})
        assert [c['id'] for c in comments] == ['c', 'a']  # newest first, the deleted one left out
        assert requests[0].url.path == '/api/issues/2-78/comments'
        assert [r.url.params['$skip'] for r in requests] == ['0', '2', '3']
        fields = requests[0].url.params['fields']
        assert all(f in fields for f in ('text', 'created', 'updated', 'author(login,fullName)', 'visibility($type'))
        assert all(r.method == 'GET' for r in requests)
    finally:
        await client.close()


# Board settings as YouTrack returned them on 2026-10-16 (SWING - Support and Parler Parent - Sprints), trimmed to
# what the app asks for. Issue data is synthetic, so no live counts are pinned here.
SWING_LANES = ['MDB', 'DEFIVELO', 'SAG', 'CIAO', 'TERVIN', 'CCHE', 'HN', 'ALSWC', 'MIDAT', 'GEO', 'BCUL', 'PP']
SWING_COLUMNS = [(3, ['In Progress', 'On Hold']), (4, ['Dev Review']), (5, ['Customer Review']), (1, ['For Budget Approval']),
                 (7, ['Closed']), (2, ['Ready']), (0, ['To Estimate']), (6, ['To Deploy'])]


def board_config(board_id, name, projects, columns, sprints_disabled, swimlanes):
    return {'id': board_id, 'name': name, 'currentSprint': {'id': '146-97', 'name': 'First sprint'},
            'sprintsSettings': {'disableSprints': sprints_disabled}, 'orphansAtTheTop': False, 'hideOrphansSwimlane': True,
            'projects': [{'id': f'0-{n}', 'shortName': key, 'name': f'{key} client'} for n, key in enumerate(projects)],
            'swimlaneSettings': swimlanes,
            'columnSettings': {'field': {'name': 'State'}, 'columns': [
                {'presentation': ', '.join(values), 'ordinal': ordinal, 'fieldValues': [{'name': v} for v in values]}
                for ordinal, values in columns]}}


SWING = board_config('145-65', 'SWING - Support', SWING_LANES, SWING_COLUMNS, True, {
    '$type': 'AttributeBasedSwimlaneSettings', 'enabled': True,
    'field': {'id': 'project', 'name': 'project', 'presentation': 'project'},
    'values': [{'id': f'151-{4385 + n}', 'name': key, 'presentation': f'{key} client'} for n, key in enumerate(SWING_LANES)]})
PP = board_config('145-973', 'Parler Parent - Sprints', ['PP'], [(0, ['Ready']), (1, ['In Progress', 'On Hold']), (2, ['Closed'])],
                  False, {'$type': 'IssueBasedSwimlaneSettings', 'enabled': True,
                          'field': {'id': '111-1', 'name': 'Type', 'presentation': 'Type'}, 'values': [{'name': 'Epic'}]})


def synthetic_issue(n, project, state):
    return {'id': f'2-{n}', 'idReadable': f'{project}-{n}', 'summary': f'Issue {n}', 'description': None,
            'project': {'id': f'0-{SWING_LANES.index(project) if project in SWING_LANES else 99}', 'shortName': project},
            'customFields': [{'id': 's', 'name': 'State', '$type': 'StateIssueCustomField', 'value': {'name': state}},
                             {'id': 'a', 'name': 'Assignee', '$type': 'SingleUserIssueCustomField', 'value': None}]}


def fake_youtrack(board, searches, sprints=(), backlogs=None):
    """A read-only YouTrack with one board: `searches` maps each issue query to its result, and `backlogs` each saved
    search ID to its issues or an HTTP error status. Writes fail the test."""
    seen = []

    def respond(request):
        assert request.method == 'GET', f'unexpected {request.method}'
        path, params = request.url.path, request.url.params
        seen.append((path, params.get('query')))
        skip, top = int(params.get('$skip', 0)), int(params.get('$top', 100))
        if path == '/api/users/me':
            return httpx.Response(200, json={'id': '1-1', 'login': 'me'})
        if path == '/api/agiles':
            found = [{'id': board['id'], 'name': board['name'], 'projects': board['projects']}]
        elif path == f'/api/agiles/{board["id"]}':
            return httpx.Response(200, json=board)
        elif path == f'/api/agiles/{board["id"]}/sprints':
            found = list(sprints)
        elif path.startswith('/api/savedQueries/') and path.endswith('/issues'):
            found = backlogs[path.split('/')[3]]
            if isinstance(found, int):
                return httpx.Response(found, json={'error_description': 'You have no access to this saved search.'})
        else:
            found = searches[params['query']]
        return httpx.Response(200, json=found[skip:skip + top])

    return Client('https://example.test', 'secret', transport=httpx.MockTransport(respond)), seen


async def test_board_with_sprints_disabled_loads_its_members_without_a_sprint():
    states = ['In Progress', 'On Hold', 'Dev Review', 'Ready', 'To Estimate', 'Closed', 'To Deploy']
    members = [synthetic_issue(n, SWING_LANES[n % 8], states[n % len(states)]) for n in range(130)]  # more than a page
    client, seen = fake_youtrack(SWING, {'has: {Board SWING - Support}': members,
                                         '{Board SWING - Support}: {First sprint}': []},
                                 sprints=[{'id': '146-97', 'name': 'First sprint', 'archived': False}])
    app = BoardApp(client)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        assert [i['id'] for i in app.issues_data] == [i['id'] for i in members]
        assert {query for _, query in seen if query} == {'has: {Board SWING - Support}'}
        assert not app.query_one('#sprints', Select).display  # no sprint to choose on this board
        assert str(app.query_one('#status', Static).content).startswith('130 issues')
        await pilot.press('r')
        await settle(app, pilot)
        assert len(app.issues_data) == 130


async def test_sprint_board_loads_only_the_sprint_and_keeps_an_empty_sprint_empty():
    sprint = [synthetic_issue(n, 'PP', 'Ready') for n in range(3)]
    sprints = [{'id': '146-2727', 'name': 'Sprint 5', 'archived': False}, {'id': '146-2728', 'name': 'Sprint 6', 'archived': False}]
    client, seen = fake_youtrack({**PP, 'currentSprint': {'id': '146-2727'}}, {
        '{Board Parler Parent - Sprints}: {Sprint 5}': sprint, '{Board Parler Parent - Sprints}: {Sprint 6}': [],
        'has: {Board Parler Parent - Sprints}': sprint + [synthetic_issue(9, 'PP', 'Ready')]}, sprints)
    app = BoardApp(client)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        assert [i['id'] for i in app.issues_data] == [i['id'] for i in sprint]
        selector = app.query_one('#sprints', Select)
        assert selector.display and selector.value == '146-2727'
        assert 'Epic swimlanes not shown' in str(app.query_one('#status', Static).content)  # said, not faked
        assert len(app.query(Column)) == 3 and not app.query('.lane')
        selector.value = '146-2728'
        await settle(app, pilot)
        assert app.issues_data == [] and not app.query(Card)  # no fallback to the whole board
        assert 'No issues in Sprint 6' in str(app.query_one('#status', Static).content)
        assert all(query != 'has: {Board Parler Parent - Sprints}' for _, query in seen)


def lanes(app):
    return [rendered(label.content).split()[0] for label in app.query('#board > .lane')]


def cell_of(app, issue_id):
    """(lane name, column index) of the cell holding a card."""
    card = next(c for c in app.query(Card) if c.issue['idReadable'] == issue_id)
    row = card.parent.parent
    return lanes(app)[list(app.query('#board > .lane-row')).index(row)], list(row.children).index(card.parent)


async def test_project_swimlanes_follow_the_board_order_share_columns_and_lose_no_card():
    client = DemoClient()
    app = BoardApp(client)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        await pilot.press('b', *'services', 'enter')
        await settle(app, pilot)
        assert lanes(app) == ['MOB', 'OPS', 'DEMO', 'Uncategorized']  # the board's order; WEB has no lane there
        assert not app.query_one('#sprints', Select).display
        board_ids = sorted(i['idReadable'] for i in await client.issues(await client.board('services'), None))
        assert sorted(c.issue['idReadable'] for c in app.query(Card)) == board_ids  # each card exactly once
        assert cell_of(app, 'WEB-2') == ('Uncategorized', 1)  # Review is grouped into In Progress
        heads = [(h.region.x, h.region.width) for h in app.query('#heads > Static')]
        assert [str(h.content) for h in app.query('#heads > Static')] == ['Open · 5', 'In Progress · 6', 'Done · 2']
        for row in app.query('#board > .lane-row'):
            assert [(cell.region.x, cell.region.width) for cell in row.children] == heads
        assert 'MOB  Mobile app for the members' in rendered(app.query('#board > .lane').first().content)
        app.prefs.set_view('services', hidden=['Done'])  # personal views apply to every lane alike
        await app.render_board()
        assert [str(h.content) for h in app.query('#heads > Static')] == ['Open · 5', 'In Progress · 6']
        assert all(len(row.children) == 2 for row in app.query('#board > .lane-row')) and len(app.query(Card)) == 11
        assert '1 hidden column' in str(app.query_one('#status', Static).content)


async def test_swimlane_keys_cross_lanes_and_moves_keep_the_card_in_its_lane():
    client = DemoClient()
    app = BoardApp(client)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press('b', *'services', 'enter')
        await settle(app, pilot)
        assert app.selected['idReadable'] == 'MOB-2'
        await pilot.press('down')  # MOB has one open card: down goes to the same column in the next lane
        assert app.selected['idReadable'] == 'OPS-1'
        await pilot.press('k')
        assert app.selected['idReadable'] == 'MOB-2'
        await pilot.press('right', 'right')  # MOB has nothing done: an empty cell can still hold focus
        assert app.selected is None and app.focused.has_class('-lane') and len(app.focused) == 0
        await pilot.press('j', 'left')
        assert app.selected['idReadable'] == 'OPS-2' and cell_of(app, 'OPS-2') == ('OPS', 1)
        await pilot.press('m', *'done', 'enter')  # to the next column; Review is grouped into In Progress here
        await settle(app, pilot)
        assert state(client, '10') == 'Done'
        assert app.selected['idReadable'] == 'OPS-2' and cell_of(app, 'OPS-2') == ('OPS', 2)
        assert app.focused.highlighted_child.issue['idReadable'] == 'OPS-2'
        await pilot.press('down', 'down', 'down')  # DEMO-5 (Done), then the uncategorized lane's empty Done cell
        assert app.focused.parent is list(app.query('#board > .lane-row'))[-1]
        board = app.query_one('#board')
        assert board.scroll_y > 0 and board.region.contains_region(app.focused.region)


async def test_swimlane_headers_stay_aligned_while_scrolling_sideways():
    app = BoardApp(DemoClient())
    async with app.run_test(size=(60, 20)) as pilot:
        await settle(app, pilot)
        await pilot.press('b', *'services', 'enter')
        await settle(app, pilot)
        await pilot.press('right', 'right', 'down')  # Done is off screen at 60 columns
        await settle(app, pilot)
        board = app.query_one('#board')
        assert board.scroll_x > 0 and app.selected['idReadable'] == 'OPS-3'
        assert board.region.contains_region(app.focused.highlighted_child.region)
        heads = [h.region.x for h in app.query('#heads > Static')]
        for row in app.query('#board > .lane-row'):
            assert [cell.region.x for cell in row.children] == heads
        label = app.query('#board > .lane')[1]
        assert label.region.x == board.region.x + 1 and rendered(label.content).startswith('OPS')  # still in view


async def test_project_scope_on_swimlanes_says_so_and_x_shows_everything_again():
    app = BoardApp(DemoClient())
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press('b', *'services', 'enter')
        await settle(app, pilot)
        await pilot.press('p', *'mob', 'enter')  # MOB is on this board: a filter, no reload
        await settle(app, pilot)
        status = str(app.query_one('#status', Static).content)
        assert lanes(app) == ['MOB'] and 'MOB only' in status and '1 of 3 lanes have cards' in status
        await pilot.press('M')  # nobody assigned MOB cards to alex
        await settle(app, pilot)
        assert not app.query(Card)
        assert 'No cards match MOB only, assigned to me. Press x' in rendered(app.query_one('#board > .empty').content)
        assert 'x shows all' in str(app.query_one('#status', Static).content)
        await pilot.press('x')
        await settle(app, pilot)
        assert lanes(app) == ['MOB', 'OPS', 'DEMO', 'Uncategorized'] and len(app.query(Card)) == 13
        assert app.prefs.data['project'] is None and not app.view()['mine'] and isinstance(app.focused, Column)


def test_markdown_renders_youtrack_text_readably_and_never_as_live_content():
    from youtrack_board.markup import markdown
    text = rendered(markdown(
        '## Steps\nfirst line\nsecond<br/>third\n\n* [x] **done**\n* [ ] open\n\n> quoted\n\n'
        '| A | B |\n| - | - |\n| `x` | y |\n\n```mermaid\ngraph TD; A-->B\n```\n\n'
        '<details><summary>Log</summary>\n<script>alert(1)</script>\n</details>\n\n'
        'See [spec](https://example.com/spec), <https://example.com/x>, [bad](javascript:alert(1)) by @jane.doe.\n'
        '![](shot.png){width=70%} and [b]markup[/b] ~~old~~'), width=60)
    lines = text.splitlines()
    assert lines[0] == 'Steps' and lines[2:5] == ['first line', 'second', 'third']  # YouTrack breaks single newlines
    assert ' • ☑ done' in lines and ' • ☐ open' in lines and '▌ quoted' in text
    assert 'x' in text and 'y' in text and 'graph TD; A-->B' in text  # tables and diagram source stay readable
    assert '<script>alert(1)</script>' in text  # HTML is shown as source, never interpreted
    assert 'spec (https://example.com/spec)' in text and 'https://example.com/x,' in text
    assert 'https://example.com/x (' not in text and '[bad](javascript:alert(1))' in text
    assert '[image: shot.png] and [b]markup[/b] old' in text and '{width' not in text


async def test_details_render_markdown_and_s_shows_the_source():
    app = BoardApp(DemoClient())
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        details = app.query_one('#details', Static)
        assert app.selected['idReadable'] == 'DEMO-1'
        assert '☑ Swimlanes per project' in rendered(details.content) and '##' not in rendered(details.content)
        await pilot.press('s')
        assert '## Acceptance' in rendered(details.content) and '* [x] **Swimlanes**' in rendered(details.content)
        assert isinstance(app.focused, Column) and app.selected['idReadable'] == 'DEMO-1'
        await pilot.press('j', 'k', 's')
        assert '##' not in rendered(details.content)


async def test_keys_pressed_in_one_tick_each_move_a_cell():
    app = BoardApp(DemoClient())
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        await pilot.press('b', *'services', 'enter')
        await settle(app, pilot)
        app.action_column(1)
        app.action_column(1)  # same tick, as when typing fast: focus must already be on the middle column
        app.focused.action_cursor_down()
        app.focused.action_cursor_down()
        await pilot.pause()
        assert cell_of(app, app.selected['idReadable']) == ('DEMO', 2)  # MOB, then OPS, then DEMO in Done


def rows(app):
    return [row.issue['idReadable'] for row in app.screen.query(Row)]


def screen_status(app):
    return str(app.screen.query_one('#status', Static).content)


async def test_backlog_pages_in_the_saved_search_order_and_the_board_comes_back_as_it_was():
    client = DemoClient()
    ranked = [i['idReadable'] for i in client.backlogs['7-1']]  # Product team's backlog, in the order it was ranked
    app = BoardApp(client)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press('M', 'right', 'j')  # only my cards on the board, WEB-3 selected
        await settle(app, pilot)
        board, sprint = app.query_one('#board'), app.query_one('#sprints', Select).value
        assert app.selected['idReadable'] == 'WEB-3'
        await pilot.press('B')
        await settle(app, pilot)
        assert isinstance(app.screen, BacklogScreen)
        assert rows(app) == ranked[:50] and len(app.screen.query(More)) == 1  # a page, not the whole backlog
        assert {names(r.issue, 'Assignee') for r in app.screen.query(Row)} > {'Alex Morgan'}  # the board's "mine" stays there
        assert screen_status(app).startswith('50 issues loaded, in backlog order · n loads 50 more')
        await pilot.press('j', 'j')
        assert 'Remember the last sprint per board' in rendered(app.screen.query_one('#details', Static).content)
        for key in 'ambpvMx':  # board keys must not act on the board's hidden card
            await pilot.press(key)
        await settle(app, pilot)
        assert isinstance(app.screen, BacklogScreen) and app.view()['mine'] and client.data[7]['customFields'][0]['value'] \
            == {'name': 'In Progress'}
        await pilot.press('end', 'enter')  # the "more" row
        await settle(app, pilot)
        assert rows(app) == ranked[:100] and app.screen.current()['idReadable'] == ranked[50]  # continues at the new ones
        await pilot.press('n')
        await settle(app, pilot)
        assert rows(app) == ranked and not app.screen.query(More)
        assert screen_status(app).startswith('123 issues, in backlog order')
        await pilot.press('escape')
        assert not isinstance(app.screen, BacklogScreen)
        assert app.selected['idReadable'] == 'WEB-3' and app.focused is app.query(Column)[1]
        assert app.query_one('#sprints', Select).value == sprint and app.query_one('#board') is board
        assert app.view()['mine'] and 'assigned to me' in str(app.query_one('#status', Static).content)


async def test_backlog_search_covers_loaded_issues_and_says_so():
    app = BoardApp(DemoClient())
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press('B')
        await settle(app, pilot)
        await pilot.press('slash', *'login')
        await settle(app, pilot)
        found = rows(app)
        assert found and all('login' in r.issue['summary'] for r in app.screen.query(Row))
        assert screen_status(app).startswith(f'{len(found)} of 50 loaded match · n searches 50 more')
        await pilot.press('enter', 'n')  # back to the list, then search the next page as well
        await settle(app, pilot)
        assert rows(app)[:len(found)] == found and len(rows(app)) > len(found)
        assert app.screen.current()['idReadable'] == rows(app)[len(found)]  # the first new match
        assert 'of 100 loaded match' in screen_status(app)
        app.screen.query_one('#backlog-search', Input).value = 'zzz'
        await settle(app, pilot)
        assert rows(app) == [] and screen_status(app).startswith('No loaded issue matches · n searches 50 more')
        app.screen.query_one('#backlog-search', Input).value = ''
        await settle(app, pilot)
        assert len(rows(app)) == 100


async def test_backlog_details_markdown_source_and_comments_only_read():
    app = BoardApp(DemoClient())
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press('B')
        await settle(app, pilot)
        details = app.screen.query_one('#details', Static)
        assert 'Show the backlog of a board' in rendered(details.content) and '**' not in rendered(details.content)
        await pilot.press('s')
        assert '**backlog**' in rendered(details.content)
        await pilot.press('enter')
        assert app.screen.maximized is app.screen.query_one('#details-pane')
        await pilot.press('escape')
        assert app.screen.maximized is None and isinstance(app.screen, BacklogScreen)
        await pilot.press('C')
        await settle(app, pilot)
        assert isinstance(app.screen, CommentsScreen) and app.screen.issue['idReadable'] == 'DEMO-6'
        assert 'keep that order' in thread(app)[0] and 'c write' not in str(app.screen.query_one('#hint', Static).content)
        await pilot.press('c')  # writing stays on the board: here it would comment on the board's card
        assert isinstance(app.screen, CommentsScreen)
        await pilot.press('escape')
        assert isinstance(app.screen, BacklogScreen) and app.screen.current()['idReadable'] == 'DEMO-6'
        await pilot.press('escape')
        assert '## Acceptance' in rendered(app.query_one('#details', Static).content)  # s applies to the board as well
        assert app.selected['idReadable'] == 'DEMO-1' and isinstance(app.focused, Column)


async def test_boards_without_a_backlog_or_with_an_empty_one_say_so():
    app = BoardApp(DemoClient())
    async with app.run_test(size=(60, 20)) as pilot:
        await settle(app, pilot)
        await pilot.press('b', *'launch', 'enter', 'B')  # Website launch has no backlog
        await settle(app, pilot)
        assert rows(app) == [] and screen_status(app) == 'No backlog'
        assert 'none is set, or its saved search may not be shared' in rendered(app.screen.query_one('#details', Static).content)
        await pilot.press('escape', 'b', *'services', 'enter')
        await settle(app, pilot)
        await pilot.press('right', 'down', 'down', 'down')  # a lane further down: the board scrolls
        await settle(app, pilot)
        board, selected = app.query_one('#board'), app.selected
        scroll = board.scroll_offset
        assert scroll.y > 0
        await pilot.press('B')
        await settle(app, pilot)
        assert rows(app) == [] and screen_status(app) == 'The backlog is empty'
        assert 'Services escalations' in rendered(app.screen.query_one('#details', Static).content)
        await pilot.press('B')
        await settle(app, pilot)
        assert board.scroll_offset == scroll and app.selected is selected and app.focused.highlighted_child.issue is selected


async def test_backlog_reads_the_board_setting_again_and_never_lists_everything():
    saved = {'id': '7-94', 'name': 'SWING - Support', 'query': '#{Unresolved} tag:swing-support has: -{Board SWING - Support}'}
    ranked = [synthetic_issue(n, project, 'To Estimate') for n, project in
              [(365, 'TERVIN'), (203, 'SAG'), (185, 'SAG'), (317, 'CCHE'), (1415, 'CIAO')]]  # across projects, as ranked
    board, backlogs = {**SWING, 'backlog': saved}, {'7-94': ranked}
    client, seen = fake_youtrack(board, {'has: {Board SWING - Support}': []}, backlogs=backlogs)
    app = BoardApp(client)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press('B')
        await settle(app, pilot)
        assert rows(app) == [i['idReadable'] for i in ranked] and screen_status(app).startswith('5 issues, in backlog order')
        assert [query for path, query in seen if path == '/api/issues'] == ['has: {Board SWING - Support}']  # the board's
        backlogs['7-94'] = 403
        await pilot.press('r')
        await settle(app, pilot)
        status = app.screen.query_one('#status', Static)
        assert status.has_class('error') and 'Could not load the backlog. Press r to retry. YouTrack returned HTTP 403' in str(status.content)
        assert 'may not be shared with you' in rendered(app.screen.query_one('#details', Static).content)
        backlogs['7-94'] = ranked
        await pilot.press('r')
        await settle(app, pilot)
        assert len(rows(app)) == 5 and not status.has_class('error')
        asked = len([path for path, _ in seen if path.startswith('/api/savedQueries/')])
        for setting in ({**saved, 'query': ' '}, {**saved, 'query': None}, None):  # r reads the board's setting again
            board['backlog'] = setting
            await pilot.press('r')
            await settle(app, pilot)
            assert rows(app) == []
            if setting:
                assert 'would list every issue you can see' in rendered(app.screen.query_one('#details', Static).content)
        assert screen_status(app) == 'No backlog' and 'saved search' not in rendered(app.screen.query_one('#backlog-title').content)
        assert len([path for path, _ in seen if path.startswith('/api/savedQueries/')]) == asked  # none of them was asked
        assert all(path in ('/api/issues', '/api/agiles', '/api/agiles/145-65', '/api/users/me', '/api/savedQueries/7-94/issues')
                   for path, _ in seen)


async def test_backlog_api_asks_the_saved_search_for_one_page_at_a_time():
    requests = []
    ranked = [synthetic_issue(n, 'PP', 'Draft') for n in (114, 93, 23, 83, 96, 70)]  # the board's manual order

    def respond(request):
        requests.append(request)
        skip, top = int(request.url.params['$skip']), int(request.url.params['$top'])
        return httpx.Response(200, json=ranked[skip:skip + top])

    client = Client('https://example.test', 'secret', transport=httpx.MockTransport(respond))
    saved = {'id': '7-1414', 'name': 'Parler Parent - Sprints Backlog', 'query': 'project: PP #Unresolved'}
    try:
        page, more = await client.backlog(saved, 0, 4)
        assert [i['idReadable'] for i in page] == ['PP-114', 'PP-93', 'PP-23', 'PP-83'] and more
        page, more = await client.backlog(saved, 4, 4)
        assert [i['idReadable'] for i in page] == ['PP-96', 'PP-70'] and not more
        assert [(r.method, r.url.path, r.url.params['$skip'], r.url.params['$top']) for r in requests] == [
            ('GET', '/api/savedQueries/7-1414/issues', '0', '5'), ('GET', '/api/savedQueries/7-1414/issues', '4', '5')]
        assert requests[0].url.params['fields'] == ISSUE_FIELDS and 'query' not in requests[0].url.params
        for query in ('', '  ', None):
            with pytest.raises(YouTrackError, match='would list every issue'):
                await client.backlog({**saved, 'query': query}, 0, 4)
        assert len(requests) == 2
    finally:
        await client.close()


async def test_closing_the_backlog_drops_late_pages_and_reload_never_duplicates():
    class Slow(DemoClient):
        hold = None

        async def backlog(self, saved, skip, top):
            if self.hold and (saved['id'], skip) == self.hold[0]:
                await self.hold[1].wait()
            return await super().backlog(saved, skip, top)

    client = Slow()
    client.hold = (('7-1', 0), asyncio.Event())
    app = BoardApp(client)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press('B')
        await pilot.pause()
        assert rows(app) == [] and screen_status(app) == 'Loading the backlog…'
        await pilot.press('escape', 'b', *'support', 'enter')  # close before Product team's backlog answers
        await settle(app, pilot)
        await pilot.press('B')
        await settle(app, pilot)
        client.hold[1].set()
        await settle(app, pilot)
        assert rows(app) == ['OPS-4', 'OPS-5', 'OPS-6', 'OPS-7']
        await pilot.press('escape', 'b', *'product', 'enter', 'B')
        await settle(app, pilot)
        client.hold = (('7-1', 50), asyncio.Event())
        await pilot.press('n')
        await pilot.pause()
        await pilot.press('r')  # reload while the next page is on its way
        await settle(app, pilot)
        client.hold[1].set()
        await settle(app, pilot)
        assert rows(app) == [i['idReadable'] for i in client.backlogs['7-1'][:50]]


async def test_backlog_rows_fit_small_terminals():
    app = BoardApp(DemoClient())
    async with app.run_test(size=(60, 20)) as pilot:
        await settle(app, pilot)
        await pilot.press('B')
        await settle(app, pilot)
        listing = app.screen.query_one('#backlog')
        visible = [row for row in app.screen.query(Row) if listing.region.contains_region(row.region)]
        assert len(visible) >= 6 and all(row.region.height == 1 for row in visible)
        for row in visible:
            meta, summary = row.query_one('.meta'), row.query_one('.summary')
            assert meta.region.width >= len('Open · Unassigned') and summary.region.width >= 12
            assert listing.region.contains_region(meta.region)
        assert app.screen.query_one('#details-pane').region.height >= 4
        assert app.screen.query_one('Footer').region.bottom == 20


async def refreshed(app, pilot):
    """One tick of the auto-refresh timer, and its answer."""
    app.tick()
    await settle(app, pilot)


def summaries(app):
    return {card.issue['idReadable']: card.issue['summary'] for card in app.query(Card)}


async def test_auto_refresh_shows_changes_keeps_the_view_and_leaves_an_unchanged_board_alone():
    client = DemoClient()
    app = BoardApp(client)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press('p', *'demo', 'enter', 's', 'right', 'j')  # DEMO only, Markdown source, DEMO-4
        await settle(app, pilot)
        assert app.selected['idReadable'] == 'DEMO-4'
        columns = list(app.query(Column))
        await refreshed(app, pilot)
        assert list(app.query(Column)) == columns  # nothing changed in YouTrack, so nothing was rebuilt
        client.data[2]['customFields'][0]['value'] = {'name': 'Done'}  # someone else finishes DEMO-3
        client.data[3]['description'] = 'Needs **retries**'  # and edits DEMO-4
        await refreshed(app, pilot)
        assert [c.issue['idReadable'] for c in app.query(Column)[2].children] == ['DEMO-3', 'DEMO-5']
        assert app.selected['idReadable'] == 'DEMO-4' and app.focused is app.query(Column)[1]
        assert app.focused.highlighted_child.issue is app.selected
        assert 'Needs **retries**' in rendered(app.query_one('#details', Static).content)  # still the source
        assert {c.issue['project']['shortName'] for c in app.query(Card)} == {'DEMO'}
        assert 'DEMO only' in str(app.query_one('#status', Static).content)


async def test_auto_refresh_keeps_the_swimlane_scroll_and_the_focused_cell():
    client = DemoClient()
    app = BoardApp(client)
    async with app.run_test(size=(60, 20)) as pilot:
        await settle(app, pilot)
        await pilot.press('b', *'services', 'enter')
        await settle(app, pilot)
        await pilot.press('right', 'right', 'down', 'down', 'down')  # the empty Done cell of the last lane
        await settle(app, pilot)
        board, cell = app.query_one('#board'), app.focused
        scroll, place = board.scroll_offset, (lanes(app)[-1], list(cell.parent.children).index(cell))
        assert scroll.x > 0 and scroll.y > 0 and app.selected is None and place == ('Uncategorized', 2)
        client.data[11]['customFields'][0]['value'] = {'name': 'Done'}  # MOB-1, in the first lane, is done
        await refreshed(app, pilot)
        assert cell_of(app, 'MOB-1') == ('MOB', 2)
        cell = app.focused
        assert (lanes(app)[-1], list(cell.parent.children).index(cell)) == place and app.selected is None
        assert board.scroll_offset == scroll
        await pilot.press('up')  # DEMO-5
        board.scroll_to(y=board.scroll_y - 2, animate=False, immediate=True)  # as the mouse wheel would
        await settle(app, pilot)
        scroll = board.scroll_offset
        assert board.region.contains_region(app.focused.highlighted_child.region)
        client.data[11]['summary'] = 'Offline tickets'
        await refreshed(app, pilot)
        assert board.scroll_offset == scroll and summaries(app)['MOB-1'] == 'Offline tickets'
        client.data[4]['customFields'][0]['value'] = {'name': 'Open'}  # reopened elsewhere: the selection follows it
        await refreshed(app, pilot)
        assert app.selected['idReadable'] == 'DEMO-5' and cell_of(app, 'DEMO-5') == ('DEMO', 0)
        assert app.focused.highlighted_child.issue is app.selected
        assert board.region.contains_region(app.focused.highlighted_child.region)


async def test_auto_refresh_toggles_per_board_and_is_remembered(tmp_path):
    class Counting(DemoClient):
        calls = 0

        async def issues(self, board, sprint):
            self.calls += 1
            return await super().issues(board, sprint)

    path = str(tmp_path / 'state.json')
    client = Counting()
    app = BoardApp(client, Prefs(path, client.base_url))
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        auto = app.query_one('#auto')
        assert str(auto.label) == f'↻ {app.REFRESH}s' and app.view()['auto']  # on by default
        await pilot.press('R')
        assert str(auto.label) == '↻ off'
        assert json.loads(open(path).read())[client.base_url]['views']['product']['auto'] is False
        client.data[0]['summary'] = 'Renamed in YouTrack'
        calls = client.calls
        await refreshed(app, pilot)
        assert client.calls == calls and summaries(app)['DEMO-1'] == 'Keyboard navigation for the board'
        await pilot.press('r')  # reloading by hand still works
        await settle(app, pilot)
        assert summaries(app)['DEMO-1'] == 'Renamed in YouTrack'
        await pilot.press('b', *'support', 'enter')  # each board has its own setting
        await settle(app, pilot)
        assert str(auto.label) == f'↻ {app.REFRESH}s'
        await pilot.press('b', 'enter')
        await settle(app, pilot)
        assert app.board_id == 'product' and str(auto.label) == '↻ off'

    app = BoardApp(client, Prefs(path, client.base_url))
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        auto = app.query_one('#auto')
        assert app.board_id == 'product' and str(auto.label) == '↻ off'
        client.data[0]['summary'] = 'Renamed again'
        calls = client.calls
        await pilot.press('R')  # turning it on brings the board up to date at once
        await settle(app, pilot)
        assert str(auto.label) == f'↻ {app.REFRESH}s' and client.calls == calls + 1
        assert summaries(app)['DEMO-1'] == 'Renamed again'


async def test_auto_refresh_waits_for_dialogs_the_backlog_typing_details_and_saves():
    class Watched(DemoClient):
        calls, saving = 0, asyncio.Event()

        async def issues(self, board, sprint):
            self.calls += 1
            return await super().issues(board, sprint)

        async def move(self, issue, field_name, value):
            await self.saving.wait()
            await super().move(issue, field_name, value)

    client = Watched()
    app = BoardApp(client)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        # The move dialog, comments, the backlog, the view dialog, typing a filter, then expanded details.
        for key in ('m', 'C', 'B', 'v', 'slash', 'enter'):
            await pilot.press(key)
            await settle(app, pilot)
            calls = client.calls
            await refreshed(app, pilot)
            assert client.calls == calls, key
            await pilot.press('escape')
            await settle(app, pilot)
        assert isinstance(app.focused, Column) and app.screen.maximized is None
        calls = client.calls
        await refreshed(app, pilot)
        assert client.calls == calls + 1  # back on the board, it refreshes again
        await pilot.press('m', 'enter')  # a save that YouTrack has not answered yet
        await pilot.pause()
        assert app.busy
        app.tick()
        await pilot.pause()
        assert client.calls == calls + 1
        client.saving.set()
        await settle(app, pilot)
        assert state(client, '1') == 'In Progress' and client.calls == calls + 2  # the save's own reload


async def test_auto_refresh_runs_one_at_a_time_and_drops_answers_meant_for_another_view():
    class Held(DemoClient):
        calls, hold = 0, None

        async def sprints(self, board_id):
            return [*await super().sprints(board_id), {'id': f'{board_id}-2', 'name': 'Sprint 02', 'archived': False}]

        async def issues(self, board, sprint):
            self.calls += 1
            if self.hold:
                await self.hold.wait()
            return [] if sprint and sprint['name'] == 'Sprint 02' else await super().issues(board, sprint)

    client = Held()
    app = BoardApp(client)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        client.data[0]['summary'] = 'Renamed in YouTrack'
        held = client.hold = asyncio.Event()
        calls = client.calls
        app.tick()
        await pilot.pause()
        app.tick()  # the first answer is still on its way: no second request
        await pilot.pause()
        assert client.calls == calls + 1
        await pilot.press('v')  # a dialog opens before the answer arrives
        client.hold = None
        held.set()
        await settle(app, pilot)
        assert summaries(app)['DEMO-1'] == 'Keyboard navigation for the board'  # not rebuilt under the dialog
        await pilot.press('escape')
        await refreshed(app, pilot)
        assert summaries(app)['DEMO-1'] == 'Renamed in YouTrack'

        held = client.hold = asyncio.Event()
        app.tick()
        await pilot.pause()
        client.hold = None
        app.query_one('#sprints', Select).value = 'product-2'  # another sprint while Sprint 01's answer is on its way
        await settle(app, pilot)
        held.set()
        await settle(app, pilot)
        assert app.sprint_data['name'] == 'Sprint 02' and not app.query(Card)

        held = client.hold = asyncio.Event()
        app.tick()
        await pilot.pause()
        client.hold = None
        await pilot.press('b', *'support', 'enter')  # another board while Sprint 02's answer is on its way
        await settle(app, pilot)
        held.set()
        await settle(app, pilot)
        assert app.board_id == 'ops' and {c.issue['project']['shortName'] for c in app.query(Card)} == {'OPS'}


async def test_auto_refresh_failures_keep_the_board_slow_down_and_warn_once(monkeypatch):
    class Flaky(DemoClient):
        calls, down = 0, False

        async def issues(self, board, sprint):
            self.calls += 1
            if self.down:
                raise YouTrackError('Cannot reach YouTrack. Check the URL and connection.')
            return await super().issues(board, sprint)

    client = Flaky()
    app = BoardApp(client)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press('right', 'j')  # DEMO-4
        notes = []
        monkeypatch.setattr(app, 'notify', lambda message, **options: notes.append((options.get('title'), message)))
        cards, auto, status = summaries(app), app.query_one('#auto'), app.query_one('#status', Static)
        client.down = True
        client.data[0]['summary'] = 'Renamed while unreachable'
        tries = []
        for _ in range(12):
            calls = client.calls
            await refreshed(app, pilot)
            tries.append(client.calls - calls)
        assert tries == [1, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0]  # after REFRESH, then 2×, 4×, 8× (at most 12×) apart
        assert summaries(app) == cards and app.selected['idReadable'] == 'DEMO-4'  # the board stays as it was
        assert auto.has_class('error') and str(auto.label) == f'↻ {app.REFRESH * 8}s'
        assert not status.has_class('error')
        assert [title for title, _ in notes] == ['Auto-refresh failed'] and 'Cannot reach YouTrack' in notes[0][1]
        client.down = False
        await pilot.press('r')  # a reload by hand that works ends the slowdown
        await settle(app, pilot)
        assert not auto.has_class('error') and str(auto.label) == f'↻ {app.REFRESH}s'
        assert summaries(app)['DEMO-1'] == 'Renamed while unreachable' and app.selected['idReadable'] == 'DEMO-4'
        calls = client.calls
        await refreshed(app, pilot)
        assert client.calls == calls + 1 and len(notes) == 1


async def test_auto_refresh_runs_on_its_own_timer(monkeypatch):
    monkeypatch.setattr(BoardApp, 'REFRESH', 0.05)
    client = DemoClient()
    app = BoardApp(client)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        client.data[0]['summary'] = 'Renamed in YouTrack'
        await pilot.pause(0.3)
        await settle(app, pilot)
        assert summaries(app)['DEMO-1'] == 'Renamed in YouTrack' and app.selected['idReadable'] == 'DEMO-1'


async def test_auto_refresh_indicator_fits_the_toolbar(monkeypatch):
    monkeypatch.setattr(BoardApp, 'REFRESH', 5)  # the labels people see; this test ends long before a tick
    app = BoardApp(DemoClient())
    async with app.run_test(size=(60, 20)) as pilot:
        await settle(app, pilot)
        auto, shown = app.query_one('#auto'), []
        for step in ('on', 'slowest', 'off'):
            if step == 'slowest':
                app.slowdown = 12
                app.show_auto()
            if step == 'off':
                await pilot.press('R')
            await pilot.pause()
            shown.append(str(auto.label))
            assert auto.region.height == 1 and app.query_one('#toolbar').region.contains_region(auto.region), step
        assert shown == ['↻ 5s', '↻ 60s', '↻ off']
        await pilot.click('#auto')  # the indicator is also a button
        assert str(auto.label) == '↻ 5s' and app.view()['auto']
