"""issueboard: which board a project folder opens, chosen once with a suggestion from local clues, and saved only
after its setup succeeds."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from textual.widgets import Input, OptionList
from test_gitlab import settle
from test_startup import HOST, checkout, git, signed_in

from gitlab_board import glab
from gitlab_board.demo import DemoClient as GitLabDemo
from issueboard import app as issueboard
from issueboard.app import directory, evidence, main
from youtrack_board import credentials
from youtrack_board.app import BoardApp
from youtrack_board.demo import DemoClient as YouTrackDemo
from issueboard.prefs import default_path, write_json

ACCOUNT = credentials.Credentials(YouTrackDemo.base_url, 'not-a-real-token', 'saved in a test keyring')


def test_directory_entry_point_shows_help():
    package = Path(issueboard.__file__).parent
    result = subprocess.run([sys.executable, str(package), '--help'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'usage: issueboard' in result.stdout


@pytest.mark.parametrize('backend', ['youtrack', 'gitlab', 'jira'])
def test_tracker_subcommand_help_works_without_setup(backend):
    result = subprocess.run([sys.executable, '-m', 'issueboard', backend, '--help'],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert f'usage: issueboard {backend}' in result.stdout
    assert '--demo' in result.stdout


def test_tracker_subcommand_keeps_credential_actions(monkeypatch):
    # Logout must remain reachable without selecting or saving a folder's board.
    logged_out = []
    monkeypatch.setattr(credentials, 'logout', lambda: logged_out.append(True) or 'Logged out')
    monkeypatch.setattr(issueboard, 'read_directories', lambda: pytest.fail('read a folder choice'))
    main(['youtrack', '--logout'])
    assert logged_out == [True]


@pytest.fixture
def run(monkeypatch):
    """Boards built by issueboard, recorded instead of run; their clients are the offline demos."""
    started = []
    monkeypatch.setattr(BoardApp, 'run', lambda self: started.append(self))
    monkeypatch.setattr(issueboard.gitlab_app.GitLabApp, 'run', lambda self: started.append(self))
    monkeypatch.setattr(issueboard, 'YouTrack', lambda url, token: YouTrackDemo())
    monkeypatch.setattr(issueboard, 'GitLab', lambda host: GitLabDemo())
    return started


def terminal(monkeypatch, *answers):
    """An interactive terminal that answers the prompts in order; an exception is raised where it stands."""
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    asked, answers = [], list(answers)

    def answer(prompt=''):
        asked.append(prompt)
        given = answers.pop(0)
        if isinstance(given, BaseException):
            raise given
        return given
    monkeypatch.setattr('builtins.input', answer)
    # Workflow tests supply selections; the real searchable UI is driven separately below.
    monkeypatch.setattr(issueboard.SetupPicker, 'run', lambda self: issueboard.pick_tracker(
        self.title, [(n, label) for n, (_, label) in enumerate(self.options)], self.default))
    return asked


def youtrack_signed_in(monkeypatch):
    monkeypatch.setattr(credentials, 'connect', lambda **_: (ACCOUNT, 'demo account'))


def saved():
    try:
        return json.loads(Path(issueboard.config_path()).read_text())['directories']
    except FileNotFoundError:
        return None


def opened(app):
    async def board():
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            return app.board_id
    return asyncio.run(board())


def test_subfolders_share_their_checkouts_folder_and_worktrees_have_their_own(tmp_path):
    repo = checkout(tmp_path / 'code', ('origin', f'git@{HOST}:demo/portal.git'))
    git('-C', repo, '-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-q', '--allow-empty', '-m', 'x')
    git('-C', repo, 'worktree', 'add', '-q', tmp_path / 'tree')
    (repo / 'src' / 'deep').mkdir(parents=True)
    (tmp_path / 'plain' / 'sub').mkdir(parents=True)
    root = str(repo.resolve())
    assert directory(repo / 'src' / 'deep') == directory(repo) == root
    assert directory(tmp_path / 'tree') == str((tmp_path / 'tree').resolve()) != root
    assert directory(tmp_path / 'plain' / 'sub') == str((tmp_path / 'plain' / 'sub').resolve())


def test_gitlab_hosting_alone_suggests_nothing_and_saved_links_do(tmp_path):
    repo = checkout(tmp_path / 'code', ('origin', f'git@{HOST}:demo/portal.git'))
    pointing, why, _ = evidence(repo)
    assert pointing == [] and 'alone decides nothing' in why[0]
    write_json(default_path('youtrack-board'), {YouTrackDemo.base_url: {'repos': {'0-1': f'https://{HOST}/Demo/Portal'}}})
    assert evidence(repo)[0] == ['youtrack']
    write_json(default_path('gitlab-board'), {HOST: {'defaults': {'demo/portal': 'demo/portal/-/boards/1'}}})
    assert evidence(repo)[0] == ['youtrack', 'gitlab']  # both: ambiguous, so nothing is suggested
    write_json(default_path('youtrack-board'), {})
    assert evidence(repo)[0] == ['gitlab']
    git('-C', repo, 'checkout', '-q', '-b', 'feature/WEB-12-login')
    pointing, why, _ = evidence(repo)
    assert pointing == ['gitlab'] and 'YouTrack and Jira both write them' in why[1]  # a key alone is no clue
    jira = {'backend': 'jira', 'url': 'https://example.atlassian.net', 'project': 'WEB', 'board': '1'}
    assert evidence(repo, {'/other/worktree': jira})[0] == ['gitlab', 'jira']
    assert evidence(repo, {'/other/worktree': {**jira, 'project': 'OPS'}})[0] == ['gitlab']


def test_first_start_asks_sets_up_youtrack_saves_and_reopens_without_asking(tmp_path, monkeypatch, run, capsys):
    repo = checkout(tmp_path / 'code', ('origin', f'git@{HOST}:demo/portal.git'))
    git('-C', repo, 'checkout', '-q', '-b', 'DEMO-3-connect')
    (repo / 'src').mkdir()
    monkeypatch.chdir(repo / 'src')
    youtrack_signed_in(monkeypatch)
    asked = terminal(monkeypatch, '1', '2', '1')  # YouTrack, then project DEMO, then its first board
    main([])
    out = capsys.readouterr().out
    assert 'DEMO-3, an issue key as YouTrack and Jira both write them' in out and '1. YouTrack\n' in out
    assert asked[0] == 'Choose 1-3: '
    folder = str(repo.resolve())
    assert saved() == {folder: {'backend': 'youtrack', 'url': YouTrackDemo.base_url, 'project': '0-1',
                                'board': 'product', 'name': 'Product team'}}
    assert opened(run[0]) == 'product' and run[0].prefs.data['project'] == '0-1'

    terminal(monkeypatch)  # no prompt allowed now
    main([])
    assert opened(run[1]) == 'product'


def test_without_clues_enter_picks_nothing_and_gitlab_setup_confirms_the_remote_and_board(tmp_path, monkeypatch, run):
    repo = checkout(tmp_path / 'code', ('origin', f'git@{HOST}:demo/portal.git'))
    monkeypatch.chdir(repo)
    asked_glab = signed_in(monkeypatch, HOST)
    asked = terminal(monkeypatch, '', '2', '', '2')  # Enter alone is no choice; GitLab; its remote; board 2
    main([])
    assert len(asked) == 4 and asked_glab == [HOST]
    assert saved() == {str(repo.resolve()): {'backend': 'gitlab', 'board': f'https://{HOST}/demo/portal/-/boards/2'}}
    assert run[0].start.board == 2 and opened(run[0]) == 'demo/portal/-/boards/2'


def test_cancelling_or_failing_setup_keeps_the_saved_choice(tmp_path, monkeypatch, run):
    monkeypatch.chdir(tmp_path)
    folder = str(tmp_path.resolve())
    before = {'backend': 'gitlab', 'board': f'https://{HOST}/demo/portal/-/boards/1'}
    write_json(issueboard.config_path(), {'directories': {folder: before}})
    terminal(monkeypatch, '1', KeyboardInterrupt())
    youtrack_signed_in(monkeypatch)
    with pytest.raises(SystemExit, match='cancelled'):
        main(['--setup'])
    signed_in(monkeypatch)  # glab knows no host: its sign-in is offered and declined
    terminal(monkeypatch, '2', f'https://{HOST}/demo/api', 'n')
    with pytest.raises(SystemExit, match='cancelled'):
        main(['--setup'])
    assert saved() == {folder: before} and not run


def test_setup_replaces_only_this_folders_choice(tmp_path, monkeypatch, run):
    monkeypatch.chdir(tmp_path)
    folder = str(tmp_path.resolve())
    other = {'backend': 'gitlab', 'board': f'https://{HOST}/demo/api/-/boards/1'}
    write_json(issueboard.config_path(), {'directories': {'/elsewhere': other, folder: other}})
    signed_in(monkeypatch, HOST)
    asked = terminal(monkeypatch, '', f'https://{HOST}/demo/portal/-/boards/1?label_name[]=Bug')
    main(['--setup'])
    assert 'Enter for 2' in asked[0]  # the current choice is preselected
    assert saved() == {'/elsewhere': other, folder: {'backend': 'gitlab', 'board': f'https://{HOST}/demo/portal/-/boards/1?label_name[]=Bug'}}
    assert run[0].start.params == {'label_name': ['Bug']}


def test_without_a_terminal_nothing_is_asked_and_the_way_out_is_named(tmp_path, monkeypatch, run):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    monkeypatch.setattr('builtins.input', lambda *_: pytest.fail('asked without a terminal'))
    for argv in ([], ['--setup']):
        with pytest.raises(SystemExit, match='interactive terminal.*issueboard youtrack, issueboard gitlab or issueboard jira'):
            main(argv)
    assert saved() is None and not run


def test_a_damaged_folder_list_is_never_overwritten(tmp_path, monkeypatch, run):
    monkeypatch.chdir(tmp_path)
    write_json(issueboard.config_path(), [])
    with pytest.raises(SystemExit, match='not an Issue Board folder list'):
        main([])
    assert json.loads(Path(issueboard.config_path()).read_text()) == []


def test_youtrack_credentials_for_another_server_open_nothing(tmp_path, monkeypatch, run):
    monkeypatch.chdir(tmp_path)
    youtrack_signed_in(monkeypatch)
    folders = {str(tmp_path.resolve()): {
        'backend': 'youtrack', 'url': 'https://other.youtrack.cloud', 'project': None, 'board': 'web'}}
    write_json(issueboard.config_path(), {'directories': folders})
    monkeypatch.setattr(issueboard.youtrack_app.Prefs, 'visit', lambda *_: pytest.fail('a board was recorded as visited'))
    with pytest.raises(SystemExit, match='other.youtrack.cloud.*issueboard youtrack --setup.*issueboard --setup'):
        main([])
    assert not run and saved() == folders and not Path(default_path('youtrack-board')).exists()


def test_both_demos_start_offline(monkeypatch, run):
    monkeypatch.setattr(glab, 'check', lambda *_: pytest.fail('the demo ran glab'))
    monkeypatch.setattr(credentials, 'connect', lambda **_: pytest.fail('the demo looked for credentials'))
    main(['--demo', 'youtrack'])
    main(['--demo', 'gitlab'])
    assert [type(app).__name__ for app in run] == ['BoardApp', 'GitLabApp'] and saved() is None


@pytest.mark.parametrize('size', [(100, 30), (60, 20)])
async def test_setup_fuzzy_search_ranks_matches_navigates_and_selects_original_value(size):
    options = [(n, f'Unrelated project {n}') for n in range(200)]
    options += [('portal', 'Customer Portal'), ('team', 'Product team'), ('support', 'Product support team')]
    app = issueboard.SetupPicker('YouTrack project:', options, default=0)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        search = app.screen.query_one(Input)
        assert search.has_focus
        await pilot.press('p', 'r', 'd', 't', 'm')  # noncontiguous, case-insensitive
        await pilot.pause()
        picker = app.screen
        assert [c[0] for c in picker.shown] == [201, 202]
        assert picker.query_one(OptionList).highlighted == 0  # the best match, not the original default
        assert picker.query_one('#dialog').region.width <= size[0]
        assert picker.query_one('#dialog').region.height <= size[1]
        await pilot.press('down', 'enter')
    assert app.return_value == 202
    assert options[app.return_value][0] == 'support'


async def test_setup_no_matches_cannot_submit_and_clearing_restores_default():
    app = issueboard.SetupPicker('YouTrack project:', [(None, 'All projects'), ('web', 'Website')], default=0)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press('z', 'z', 'z', 'enter')
        await pilot.pause()
        assert not app.screen.shown
        assert app.screen.query_one(OptionList).get_option_at_index(0).disabled
        assert app.return_value is None and app.is_running
        await pilot.press('ctrl+u', 'enter')
    assert app.return_value == 0  # All projects is a valid selection, distinct from cancellation


async def test_setup_escape_cancels_and_labels_are_literal():
    app = issueboard.SetupPicker('GitLab board:', [(7, '[red]Release[/red]'), (8, '[red]Release[/red]')])
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.screen.query_one(OptionList).get_option_at_index(0).prompt.plain == '[red]Release[/red]'
        await pilot.press('escape')
    assert app.return_value is None


def test_cancelled_search_does_not_replace_the_saved_folder_choice(tmp_path, monkeypatch, run):
    monkeypatch.chdir(tmp_path)
    folder = str(tmp_path.resolve())
    before = {'backend': 'gitlab', 'board': f'https://{HOST}/demo/portal/-/boards/1'}
    write_json(issueboard.config_path(), {'directories': {folder: before}})
    terminal(monkeypatch, '1')
    youtrack_signed_in(monkeypatch)
    monkeypatch.setattr(issueboard.SetupPicker, 'run', lambda self: None)
    with pytest.raises(SystemExit, match='cancelled'):
        main(['--setup'])
    assert saved() == {folder: before} and not run
