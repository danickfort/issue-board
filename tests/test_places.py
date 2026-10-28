"""Jira merge requests, the connection strip, and switching places (P) among the folders issueboard has saved."""
import json
import os
from pathlib import Path

import pytest
from textual.widgets import Button, Input, Static
from test_gitlab import demo_app, settle
from test_startup import HOST, checkout, signed_in

from gitlab_board import glab
from gitlab_board.glab import Glab
from gitlab_board.merge_requests import MergeRequestsScreen
from issueboard import app as issueboard
from issueboard.prefs import Prefs, write_json
from issueboard.story_merge_requests import demo_gitlab
from issueboard.widgets import Column, CommentDialog, Picker
from jira_board import credentials as jira_credentials
from jira_board.app import JiraApp
from jira_board.demo import DemoClient as JiraDemo
from jira_board.demo import demo_state as jira_state
from youtrack_board import credentials
from youtrack_board.app import BoardApp
from youtrack_board.demo import DemoClient as YouTrackDemo
from youtrack_board.demo import demo_state as youtrack_state

SITE = JiraDemo.base_url


def jira(prefs=None, cwd=None, client=None):
    client = client or JiraDemo()
    return JiraApp(client, prefs or Prefs(None, client.base_url, jira_state()), gitlab=demo_gitlab, cwd=cwd)


async def pick(app, pilot, key):
    column = next(c for c in app.query(Column) if any(app.ref(card.issue) == key for card in c.children))
    column.focus()
    column.index = [app.ref(card.issue) for card in column.children].index(key)
    await pilot.pause()
    assert app.ref(app.selected) == key


def who(app):
    return str(app.query_one('#who', Static).render())


# Jira: GitLab merge requests naming a Jira issue

@pytest.mark.parametrize('size', [(80, 24), (60, 20)])
async def test_jira_g_lists_mrs_naming_the_issue_and_G_every_mr_of_the_project_repository(size, monkeypatch):
    monkeypatch.setattr(Glab, 'run', lambda *a, **k: pytest.fail('the demo ran glab'))
    app = jira()
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, 'WEB-2')
        column, card = app.focused, app.selected
        await pilot.press('g')
        await settle(app, pilot)
        screen = app.screen
        assert isinstance(screen, MergeRequestsScreen) and screen.home == 'demo/api'
        assert [(m['title'], m['why']) for m in screen.mrs] == [('Prices endpoint for the pricing page',
                                                                 'Names WEB-2 in its branch, description')]
        assert screen.query_one('#mr-hint').region.bottom <= screen.query_one('#mr-dialog').region.bottom
        await pilot.press('escape')
        assert app.focused is column and app.selected is card
        app.set_focus(None)
        app.selected = None  # G without an issue: the board's project
        await pilot.press('G')
        await settle(app, pilot)
        titles = [m['title'] for m in app.screen.mrs]
        assert len(titles) == 3 and 'Prices endpoint for the pricing page' in titles
        app.screen.query_one('OptionList').highlighted = titles.index('Prices endpoint for the pricing page')
        await pilot.press('i')  # back to the Jira issue the MR names, its details expanded
        await settle(app, pilot)
        assert app.selected['key'] == 'WEB-2' and app.screen.maximized is app.query_one('#details-pane')
    # project keys are whole tokens, and only those of projects linked to this repository count
    assert app.named_stories({'title': 'WEB-10 and WEB-1x, DES-1, xWEB-3'}, HOST, 'demo/api') == {'WEB-10': 'title'}


async def test_jira_first_use_validates_then_saves_the_repository_per_site_and_project(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps({'https://other.atlassian.net': {'repos': {'DES': 'https://gitlab.com/keep/this'}},
                                SITE: {'visited': {'1': 1}}}))
    app = jira(Prefs(str(path), SITE))
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, 'WEB-2')
        await pilot.press('g')
        assert isinstance(app.screen, CommentDialog) and 'GitLab repository of WEB' in str(app.screen.query_one('Label').content)
        await pilot.press('escape')  # cancelled: nothing saved
        assert app.prefs.data['repos'] == {}
        await pilot.press('g', *'gitlab.example.com/demo/gone', 'enter')
        await settle(app, pilot)
        assert 'no project or group demo/gone' in str(app.screen.query_one('#mr-hint').content)
        assert app.prefs.data['repos'] == {}  # GitLab did not show it: not saved
        await pilot.press('u')
        app.screen.query_one(Input).value = f'https://{HOST}/demo/api'
        await pilot.press('enter')
        await settle(app, pilot)
        assert len(app.screen.mrs) == 1 and app.prefs.data['repos'] == {'WEB': f'https://{HOST}/demo/api'}
    saved = json.loads(path.read_text())
    assert saved['https://other.atlassian.net'] == {'repos': {'DES': 'https://gitlab.com/keep/this'}}
    assert saved[SITE]['repos'] == {'WEB': f'https://{HOST}/demo/api'}


async def test_jira_finds_the_repository_in_its_own_folders_checkout(tmp_path, monkeypatch):
    signed_in(monkeypatch, HOST)
    repo = checkout(tmp_path / 'code', ('origin', f'git@{HOST}:demo/api.git'))
    app = jira(Prefs(None, SITE, {'visited': jira_state()['visited']}), cwd=repo)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, 'WEB-2')
        await pilot.press('g')
        await settle(app, pilot)
        assert isinstance(app.screen, MergeRequestsScreen) and app.prefs.data['repos'] == {'WEB': f'https://{HOST}/demo/api'}


async def test_jira_G_on_a_board_without_one_project_asks_for_one():
    client = JiraDemo()
    app = jira(client=client)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        app.board_data['location'] = {}  # a filter board spanning projects
        app.set_focus(None)
        app.selected = None
        await pilot.press('G')
        assert not app.screen.is_modal and 'choose a project with p' in str(app.query_one('#status', Static).content)


# The connection strip

@pytest.mark.parametrize('size', [(100, 30), (60, 20)])
async def test_each_board_shows_its_tracker_account_and_server_and_no_place_when_run_alone(size):
    for make, expected in ((jira, 'Jira Scrum · Alex Morgan · example.atlassian.net'),
                           (lambda: BoardApp(YouTrackDemo(), Prefs(None, YouTrackDemo.base_url, youtrack_state())),
                            'YouTrack · Alex Morgan (alex) · example.youtrack.cloud'),
                           (lambda: demo_app()[0], 'GitLab · Alex Morgan (@alex) · gitlab.example.com')):
        app = make()
        async with app.run_test(size=size) as pilot:
            await settle(app, pilot)
            assert who(app) == expected
            assert app.query_one('#who').region.width >= 40 and not app.query('#places')
            assert app.query_one('#toolbar').region.y == 1 and not app.check_action('places', ())


async def test_the_account_is_never_guessed():
    class Refusing(JiraDemo):
        async def me(self):
            from jira_board.client import JiraError
            raise JiraError('Jira returned HTTP 401.')
    app = jira(client=Refusing())
    async with app.run_test(size=(80, 24)) as pilot:
        assert 'signing in…' in who(app) or 'account unknown' in who(app)
        await settle(app, pilot)
        assert who(app) == 'Jira · account unknown · example.atlassian.net'


async def test_kanban_and_board_changes_update_the_strip():
    app = jira()
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        app.load_board('2')
        await settle(app, pilot)
        assert who(app).startswith('Jira Kanban · Alex Morgan')


# Places: the picker on the board

def places(tmp_path, **extra):
    """Three saved folders, one per tracker, plus one that is gone and one that is not a valid entry."""
    tmp_path = tmp_path.resolve()
    folders = {name: tmp_path / name for name in ('web', 'site', 'support', *extra)}
    for folder in folders.values():
        folder.mkdir()
    saved = {str(folders['web']): {'backend': 'jira', 'url': SITE, 'project': 'WEB', 'board': '1', 'name': 'Web team'},
             str(folders['site']): {'backend': 'gitlab', 'board': f'https://{HOST}/demo/portal/-/boards/1'},
             str(folders['support']): {'backend': 'youtrack', 'url': YouTrackDemo.base_url, 'project': None,
                                       'board': 'ops', 'name': 'Support kanban'},
             str(tmp_path / 'gone'): {'backend': 'gitlab', 'board': f'https://{HOST}/demo/api/-/boards/1'},
             str(tmp_path / 'broken'): {'backend': 'jira'},
             **{str(folders[name]): choice for name, choice in extra.items()}}
    write_json(issueboard.config_path(), {'directories': saved})
    return {name: str(folder) for name, folder in folders.items()}


@pytest.mark.parametrize('size', [(100, 30), (60, 20)])
async def test_P_lists_saved_places_and_picks_another_or_nothing(tmp_path, size):
    folders = places(tmp_path)
    app = jira()
    app.folder, app.places = folders['web'], issueboard.place_choices
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        assert str(app.query_one('#places', Button).label) == '⌂ Folder (Shift+P): web ▾'
        assert app.query_one('#who').region.width >= 20  # the account stays readable beside the place
        await pilot.press('P')
        picker = app.screen
        assert isinstance(picker, Picker)
        rows = {value: (label.plain, disabled) for value, label, _, disabled in picker.choices}
        gone = str(tmp_path.resolve() / 'gone')
        assert set(rows) == {*folders.values(), gone}  # the broken entry is skipped
        assert rows[folders['web']] == (f'web  Jira · Web team  example.atlassian.net · {folders["web"]}  (current)', True)
        assert rows[folders['site']][0].startswith('site  GitLab · demo/portal board 1  gitlab.example.com')
        assert rows[gone][1] and 'folder not found' in rows[gone][0]
        await pilot.press(*'youtrack')
        assert [c[0] for c in picker.shown] == [folders['support']]
        await pilot.press('escape')  # nothing changes
        assert not app.screen.is_modal and app.return_value is None and app.is_running
        await pilot.press('P', *'portal', 'enter')
        await pilot.pause()
    assert app.return_value == folders['site']


async def test_P_waits_for_saves_and_dialogs_and_warns_about_an_unsent_comment(tmp_path):
    folders = places(tmp_path)
    app = jira()
    app.folder, app.places = folders['web'], issueboard.place_choices
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        app.busy = True
        await pilot.press('P')
        assert not app.screen.is_modal
        app.busy = False
        await pilot.press('c')
        assert isinstance(app.screen, CommentDialog)
        await pilot.press('P')  # typed into the comment, not a switch
        assert isinstance(app.screen, CommentDialog) and app.screen.query_one(Input).value == 'P'
        await pilot.press('escape')
        app.drafts['WEB-1'] = 'not sent yet'
        await pilot.press('P')
        assert not app.screen.is_modal  # warned first
        await pilot.press('P')
        assert isinstance(app.screen, Picker)


async def test_a_damaged_folder_list_keeps_the_board_and_is_not_overwritten(tmp_path):
    write_json(issueboard.config_path(), [])
    app = jira()
    app.folder, app.places = str(tmp_path), issueboard.place_choices
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press('P')
        assert not app.screen.is_modal and app.is_running
    assert json.loads(Path(issueboard.config_path()).read_text()) == []


async def test_closing_a_board_closes_its_clients():
    closed = []

    class Client(JiraDemo):
        async def close(self):
            closed.append('jira')

    class GitLab(issueboard.gitlab_app.DemoClient):
        async def close(self):
            closed.append('gitlab')
    app = JiraApp(Client(), Prefs(None, SITE, jira_state()), gitlab=lambda host: GitLab())
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, 'WEB-2')
        await pilot.press('g')
        await settle(app, pilot)
    assert closed == ['jira', 'gitlab']


# Places: the launcher

@pytest.fixture
def launcher(monkeypatch, tmp_path):
    """Boards run one after another, each answering as scripted (a folder to switch to, or None to quit). Offline
    clients; who signed in where is recorded."""
    script, ran, signed = [], [], []

    def run(self):
        ran.append(self)
        return script.pop(0)
    for cls in (BoardApp, JiraApp, issueboard.gitlab_app.GitLabApp):
        monkeypatch.setattr(cls, 'run', run)
    monkeypatch.setattr(issueboard, 'YouTrack', lambda url, token: YouTrackDemo())
    monkeypatch.setattr(issueboard, 'Jira', lambda url, email, token: JiraDemo())
    monkeypatch.setattr(issueboard, 'GitLab', lambda host: issueboard.gitlab_app.DemoClient())
    jira_account = jira_credentials.Credentials(SITE, 'jane@example.com', 'jira-token', 'test')
    youtrack_account = credentials.Credentials(YouTrackDemo.base_url, 'youtrack-token', 'test')

    def jira_connect(site=None, interactive=True, **_):
        signed.append(('jira', site, interactive))
        return jira_account, 'jira account'

    def youtrack_connect(interactive=True, **_):
        signed.append(('youtrack', None, interactive))
        return youtrack_account, 'youtrack account'
    monkeypatch.setattr(jira_credentials, 'connect', jira_connect)
    monkeypatch.setattr(credentials, 'connect', youtrack_connect)
    signed_in(monkeypatch, HOST)
    return script, ran, signed


def test_switching_opens_each_tracker_in_its_own_folder_and_saves_nothing(tmp_path, monkeypatch, launcher):
    script, ran, signed = launcher
    folders = places(tmp_path)
    monkeypatch.chdir(folders['web'])
    monkeypatch.setattr('sys.stdin.isatty', lambda: True)
    before = Path(issueboard.config_path()).read_text()
    script += [folders['support'], folders['site'], folders['web'], None]
    issueboard.main(['--verbose'])
    assert [type(app).__name__ for app in ran] == ['JiraApp', 'BoardApp', 'GitLabApp', 'JiraApp']
    assert [app.folder for app in ran] == [folders['web'], folders['support'], folders['site'], folders['web']]
    assert ran[1].cwd == folders['support'] and ran[3].cwd == folders['web']  # remotes come from the place's folder
    assert ran[2].start.path == 'demo/portal' and all(app.places is issueboard.place_choices for app in ran)
    assert all(app.log_buffer is not None for app in ran)  # --verbose lasts across switches
    # Each tracker signs in for its own server; only the start may ask, a switch never does.
    assert signed == [('jira', SITE, True), ('youtrack', None, False), ('jira', SITE, False)]
    assert os.getcwd() == folders['web'] and Path(issueboard.config_path()).read_text() == before
    assert len({id(app.client) for app in ran}) == 4  # a fresh client each time


def test_a_place_that_cannot_open_returns_to_the_previous_one_with_the_reason(tmp_path, monkeypatch, launcher):
    script, ran, signed = launcher
    folders = places(tmp_path, other={'backend': 'youtrack', 'url': 'https://other.youtrack.cloud', 'project': None,
                                      'board': 'ops'})
    monkeypatch.chdir(folders['support'])

    def refuse(site=None, **_):
        raise jira_credentials.SetupError(f'No usable Jira credentials for {site}.')
    monkeypatch.setattr(jira_credentials, 'connect', refuse)
    script += [str(Path(folders['web']).parent / 'gone'), folders['web'], folders['other'], None]
    issueboard.main([])
    assert [(type(app).__name__, app.folder) for app in ran] == [('BoardApp', folders['support'])] * 4
    assert 'gone is not there any more. Its saved board is kept' in ran[1].notice
    assert ran[2].notice == f'Could not open {folders["web"]}: No usable Jira credentials for {SITE}.'
    assert 'the YouTrack token test is for https://example.youtrack.cloud' in ran[3].notice  # never sent elsewhere
    assert issueboard.saved_choice(issueboard.read_directories(), str(Path(folders['web']).parent / 'gone'))


def test_themes_follow_each_folder_across_switches_and_restarts(tmp_path, monkeypatch, launcher):
    folders = places(tmp_path, another={'backend': 'jira', 'url': SITE, 'project': 'WEB', 'board': '1'})
    monkeypatch.chdir(folders['web'])
    visits = iter([(folders['web'], 'textual-dark', 'nord', folders['another']),
                   (folders['another'], 'textual-dark', 'dracula', folders['support']),
                   (folders['support'], 'textual-dark', 'gruvbox', folders['web']),
                   (folders['web'], 'nord', None, folders['another']),
                   (folders['another'], 'dracula', None, None),
                   (folders['web'], 'nord', None, None)])

    def run(app):
        folder, expected, selected, next_folder = next(visits)
        assert app.folder == folder and app.theme == expected
        if selected:
            app.theme = selected
        return next_folder

    for cls in (JiraApp, BoardApp):
        monkeypatch.setattr(cls, 'run', run)
    before = issueboard.read_directories()
    issueboard.main([])
    issueboard.main([])  # a fresh command restores the starting folder's theme
    after = issueboard.read_directories()
    for name, theme in [('web', 'nord'), ('another', 'dracula'), ('support', 'gruvbox')]:
        assert after[folders[name]] == {**before[folders[name]], 'theme': theme}
    assert after[folders['site']] == before[folders['site']]


def test_a_failed_switch_keeps_the_newly_selected_theme(tmp_path, monkeypatch, launcher):
    folders = places(tmp_path)
    monkeypatch.chdir(folders['web'])
    themes = []

    def run(app):
        themes.append(app.theme)
        if len(themes) == 1:
            app.theme = 'nord'
            return str(tmp_path / 'missing')
        assert app.notice

    monkeypatch.setattr(JiraApp, 'run', run)
    issueboard.main([])
    assert themes == ['textual-dark', 'nord']


@pytest.mark.parametrize('theme', ['no-longer-installed', None, 42])
def test_invalid_saved_themes_use_the_default(tmp_path, launcher, theme):
    folders = places(tmp_path)
    choice = {**issueboard.read_directories()[folders['web']], 'theme': theme}
    app = issueboard.open_place(folders['web'], choice, False)
    assert app.theme == 'textual-dark'


def test_saving_a_theme_keeps_corrupt_config_and_reports_the_error(tmp_path, capsys):
    path = Path(issueboard.config_path())
    path.parent.mkdir(parents=True)
    path.write_text('{damaged')
    issueboard.save_theme(str(tmp_path), 'nord')
    assert path.read_text() == '{damaged'
    assert 'Could not save this place’s theme' in capsys.readouterr().err
