"""Starting inside a code checkout: its GitLab project, the project's default board, and the YouTrack repository link."""
import json
import subprocess
import sys

import pytest
from textual.widgets import Input, Static

from gitlab_board import glab, remotes
from gitlab_board.app import GitLabApp, Prefs, main
from gitlab_board.demo import DemoClient
from gitlab_board.glab import GlabError, UnknownHost
from gitlab_board.merge_requests import MergeRequestsScreen
from gitlab_board.remotes import NoRemote, Remote
from gitlab_board.target import Target
from test_gitlab import FAKE, settle
from test_merge_requests import pick
from youtrack_board.app import BoardApp, CommentDialog, Picker
from youtrack_board.demo import DemoClient as YouTrackDemo
from youtrack_board.demo import demo_gitlab
from youtrack_board.prefs import Prefs as YouTrackPrefs

HOST = 'gitlab.example.com'
TOKEN = 'glpat-abcdefghijklmnopqrstuvwx'


def git(*args, cwd=None):
    return subprocess.run(['git', *map(str, args)], cwd=cwd, check=True, capture_output=True, text=True).stdout


def checkout(folder, *remotes_):
    """A real Git repository with these (name, fetch URL) remotes."""
    git('init', '-q', '-b', 'main', folder)
    for name, url in remotes_:
        git('-C', folder, 'remote', 'add', name, url)
    return folder


def signed_in(monkeypatch, *hosts):
    """glab is signed in to these hosts only; any other is unknown to it, as `glab auth status --hostname` says."""
    asked = []

    def check(host, binary='glab'):
        asked.append(host)
        if host not in hosts:
            raise UnknownHost(glab.LOGIN.format(host=host))
        return 'glab 1.115.0'
    monkeypatch.setattr(glab, 'check', check)
    return asked


def refuse(host, binary='glab'):
    raise GlabError(glab.LOGIN.format(host=host))


# Remote discovery, from real Git repositories

def test_remotes_name_projects_by_host_and_path_from_any_url_form(tmp_path):
    repo = checkout(tmp_path / 'code', ('origin', f'git@{HOST}:team/sub/app.git'),
                    ('mirror', f'https://oauth2:{TOKEN}@{HOST.upper()}/team/sub/app'),
                    ('upstream', 'ssh://git@gitlab.example.org:2222/core/app.git'),
                    ('web', 'https://git.example.net:8443/a/b/c.git/'), ('local', str(tmp_path / 'elsewhere')))
    git('-C', repo, 'remote', 'set-url', '--push', 'upstream', 'git@push-only.example.com:x/y.git')
    found = remotes.remotes(repo)
    assert found == [Remote('mirror, origin', HOST, 'team/sub/app'), Remote('upstream', 'gitlab.example.org', 'core/app'),
                     Remote('web', 'git.example.net:8443', 'a/b/c')]  # SSH port dropped, web port kept, push URL ignored
    assert TOKEN not in ' '.join(map(str, found)) and str(found[0]) == f'mirror, origin → {HOST}/team/sub/app'


def test_subfolders_and_worktrees_find_the_same_remotes_without_changing_git_config(tmp_path):
    repo = checkout(tmp_path / 'code', ('origin', f'https://{HOST}/team/app.git'))
    git('-C', repo, '-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-q', '--allow-empty', '-m', 'x')
    git('-C', repo, 'worktree', 'add', '-q', tmp_path / 'tree')
    (repo / 'src' / 'deep').mkdir(parents=True)
    config = (repo / '.git' / 'config').read_text()
    for folder in (repo, repo / 'src' / 'deep', tmp_path / 'tree'):
        assert remotes.remotes(folder) == [Remote('origin', HOST, 'team/app')]
    assert (repo / '.git' / 'config').read_text() == config


def test_ssh_aliases_resolve_through_ssh_config(tmp_path, monkeypatch):
    config = tmp_path / 'ssh_config'
    config.write_text(f'Host work\n  HostName {HOST}\n  User git\n')
    monkeypatch.setattr(remotes, 'SSH_CONFIG', str(config))
    repo = checkout(tmp_path / 'code', ('origin', 'work:team/app.git'), ('other', 'ssh://work/team/lib'))
    assert remotes.remotes(repo) == [Remote('origin', HOST, 'team/app'), Remote('other', HOST, 'team/lib')]


def test_a_base_remote_chosen_in_glab_wins(tmp_path):
    repo = checkout(tmp_path / 'code', ('origin', f'git@{HOST}:me/app.git'), ('upstream', f'git@{HOST}:team/app.git'))
    assert len(remotes.remotes(repo)) == 2
    git('-C', repo, 'config', 'remote.upstream.glab-resolved-base', 'base')
    assert remotes.remotes(repo) == [Remote('upstream', HOST, 'team/app')]
    git('-C', repo, 'config', 'remote.upstream.glab-resolved-base', 'base:other/app')
    assert remotes.remotes(repo) == [Remote('upstream', HOST, 'other/app')]


def test_no_checkout_no_remote_and_local_remotes_say_why(tmp_path):
    (tmp_path / 'plain').mkdir()
    with pytest.raises(NoRemote, match='Not inside a Git checkout') as error:
        remotes.remotes(tmp_path / 'plain')
    assert not error.value.checkout
    with pytest.raises(NoRemote, match='has no remote.') as error:
        remotes.remotes(checkout(tmp_path / 'bare'))
    assert error.value.checkout
    with pytest.raises(NoRemote, match='no remote on a network host'):
        remotes.remotes(checkout(tmp_path / 'local', ('origin', str(tmp_path / 'bare')), ('file', 'file:///srv/x.git')))


def test_only_hosts_glab_is_signed_in_to_count(tmp_path, monkeypatch):
    asked = signed_in(monkeypatch, HOST)
    repo = checkout(tmp_path / 'code', ('origin', f'https://me:{TOKEN}@github.com/me/app.git'),
                    ('gitlab', f'git@{HOST}:team/app.git'))
    assert remotes.find(repo) == ([Remote('gitlab', HOST, 'team/app')], {HOST: 'glab 1.115.0'})
    assert asked == [HOST, 'github.com']  # git lists remotes by name
    with pytest.raises(NoRemote, match=f'No remote of this checkout is on {HOST}.org'):
        remotes.find(repo, HOST + '.org')
    signed_in(monkeypatch)
    with pytest.raises(NoRemote, match='glab is signed in to') as error:
        remotes.find(repo)
    assert 'origin → github.com/me/app' in str(error.value) and TOKEN not in str(error.value)
    monkeypatch.setattr(glab, 'check', refuse)
    with pytest.raises(GlabError, match='glab auth login'):
        remotes.find(repo)  # a host glab knows refused the sign-in: said, not skipped


def test_glab_tells_unknown_hosts_from_unreachable_ones(tmp_path, monkeypatch):
    script = tmp_path / 'glab'
    script.write_text(FAKE.format(python=sys.executable))
    script.chmod(0o755)
    monkeypatch.setenv('FAKE_LOG', str(tmp_path / 'log.jsonl'))
    monkeypatch.setenv('FAKE_EXIT', '1')
    monkeypatch.setenv('FAKE_ERR', 'X github.com has not been authenticated with glab; run `glab auth login`')
    with pytest.raises(UnknownHost):
        glab.check('github.com', str(script))
    monkeypatch.setenv('FAKE_ERR', f'Get "https://{HOST}/api/v4/user": dial tcp: lookup {HOST}: no such host')
    with pytest.raises(GlabError, match=f'Could not reach {HOST}') as error:
        glab.check(HOST, str(script))
    assert not isinstance(error.value, UnknownHost)
    monkeypatch.setenv('FAKE_ERR', f'401 Unauthorized {TOKEN}')
    with pytest.raises(GlabError, match='glab auth login --hostname') as error:
        glab.check(HOST, str(script))
    assert TOKEN not in str(error.value)


# GitLab board: the command line

def started(monkeypatch, *args):
    monkeypatch.setattr(sys, 'argv', ['gitlab-board', *args])
    apps = []
    monkeypatch.setattr(GitLabApp, 'run', lambda self: apps.append(self))
    main()
    return apps[0]


def test_command_line_opens_the_checkouts_project_unless_a_url_is_given(tmp_path, monkeypatch):
    asked = signed_in(monkeypatch, HOST, 'gitlab.example.org')
    repo = checkout(tmp_path / 'code', ('origin', f'git@{HOST}:team/app.git'))
    (repo / 'src').mkdir()
    monkeypatch.chdir(repo / 'src')
    app = started(monkeypatch)
    assert app.start == Target(HOST, 'team/app', 'project') and app.remotes == [Remote('origin', HOST, 'team/app')]
    assert app.client.host == HOST and app.prefs.server == HOST and asked == [HOST]  # one sign-in check
    app = started(monkeypatch, 'https://gitlab.example.org/groups/ops/-/boards/4')
    assert app.start.path == 'ops' and app.start.board == 4 and app.remotes == []
    git('-C', repo, 'remote', 'add', 'fork', 'git@gitlab.example.org:me/app.git')
    with pytest.raises(SystemExit, match='several hosts') as error:
        started(monkeypatch)
    assert 'fork → gitlab.example.org/me/app' in str(error.value)
    app = started(monkeypatch, '--hostname', 'gitlab.example.org')
    assert app.start.path == 'me/app' and app.client.host == 'gitlab.example.org'
    git('-C', repo, 'remote', 'add', 'upstream', f'https://{HOST}/core/app')
    app = started(monkeypatch, '--hostname', HOST)
    assert app.start is None and [r.path for r in app.remotes] == ['team/app', 'core/app']  # the board asks


def test_command_line_without_a_gitlab_remote_resumes_and_says_why(tmp_path, monkeypatch, capsys):
    signed_in(monkeypatch, 'gitlab.com')
    monkeypatch.chdir(checkout(tmp_path / 'code', ('origin', 'git@github.com:me/app.git')))
    with pytest.raises(SystemExit, match='github.com/me/app.*Give a board URL'):
        started(monkeypatch)
    state = tmp_path / 'state' / 'gitlab-board' / 'state.json'
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({'gitlab.com': {'project': 'team/app'}}))
    app = started(monkeypatch)
    assert app.start.path == 'team/app' and 'github.com/me/app' in app.note
    monkeypatch.chdir(tmp_path)  # not a checkout: resuming is all, nothing to explain
    app = started(monkeypatch)
    assert app.start.path == 'team/app' and app.note is None


def test_the_demo_never_looks_at_the_checkout(monkeypatch):
    monkeypatch.setattr(remotes, 'git', lambda *a: pytest.fail('the demo read git remotes'))
    app = started(monkeypatch, '--demo')
    assert app.remotes == () and app.start.path == 'demo/portal'


# GitLab board: the default board of the checkout's project

class OneBoard(DemoClient):
    async def place(self, path, kind=''):
        found = await super().place(path, kind)
        return {**found, 'boards': found['boards'][:1]}


def from_checkout(prefs, client=None, *paths):
    client = client or DemoClient()
    found = [Remote('origin', client.host, p) for p in paths or ('demo/portal',)]
    start = Target(client.host, found[0].path, 'project') if len(found) == 1 else None
    return GitLabApp(client, prefs, start=start, remotes=found)


def status(app):
    return str(app.query_one('#status', Static).content)


async def test_first_start_asks_for_the_default_board_and_saves_only_a_choice(tmp_path):
    path = tmp_path / 'state.json'
    app = from_checkout(Prefs(str(path), HOST))
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        assert isinstance(app.screen, Picker) and app.screen.prompt == 'Found project demo/portal — choose your default board'
        assert app.screen.value == 'demo/portal/-/boards/2'  # your last visit on GitLab, preselected only
        assert 'your last visit on GitLab' in str(app.screen.choices[1][1]) and not app.board_data
        await pilot.press('escape')
        await settle(app, pilot)
        assert not app.board_data and 'nothing was saved' in status(app) and not path.exists()
    app = from_checkout(Prefs(str(path), HOST))
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press('enter')
        await settle(app, pilot)
        assert app.board_id == 'demo/portal/-/boards/2' and app.board_data
    assert json.loads(path.read_text())[HOST]['defaults'] == {'demo/portal': 'demo/portal/-/boards/2'}
    app = from_checkout(Prefs(str(path), HOST))
    async with app.run_test(size=(60, 20)) as pilot:
        await settle(app, pilot)  # the default opens; no question
        assert app.board_id == 'demo/portal/-/boards/2' and not isinstance(app.screen, Picker)
        await pilot.press('b')
        assert '· default' in str(next(c[1] for c in app.screen.choices if c[0] == 'demo/portal/-/boards/2'))
        await pilot.press('escape')


async def test_board_switches_keep_the_default_and_d_changes_it_per_place(tmp_path):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps({HOST: {'defaults': {'demo/portal': 'demo/portal/-/boards/2', 'bad': 3}},
                                'gitlab.example.org': {'defaults': {'demo/portal': 'demo/portal/-/boards/1'}}}))
    app = from_checkout(Prefs(str(path), HOST))
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        assert app.board_id == 'demo/portal/-/boards/2'
        await pilot.press('b', 'enter')  # another board, picked from the list
        await settle(app, pilot)
        assert app.board_id == 'demo/portal/-/boards/1' and app.prefs.data['defaults']['demo/portal'].endswith('/2')
        await pilot.press('D')
        app.go('https://gitlab.example.com/groups/demo')  # a group has its own default
        await settle(app, pilot)
        await pilot.press('D')
    saved = json.loads(path.read_text())
    assert saved[HOST]['defaults'] == {'demo/portal': 'demo/portal/-/boards/1', 'groups/demo': 'groups/demo/-/boards/3'}
    assert saved['gitlab.example.org'] == {'defaults': {'demo/portal': 'demo/portal/-/boards/1'}}  # another host: untouched
    app = GitLabApp(DemoClient(), Prefs(str(path), HOST), start=Target(HOST, 'demo/portal'))  # a project URL, no checkout
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        assert app.board_id == 'demo/portal/-/boards/1'


async def test_a_gone_default_is_explained_and_asked_again():
    app = from_checkout(Prefs(None, HOST, {'defaults': {'demo/portal': 'demo/portal/-/boards/9'}}))
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        assert app.screen.prompt == 'demo/portal: choose a new default board'
        assert 'default board of demo/portal is gone' in status(app)
        await pilot.press('down', 'enter')
        await settle(app, pilot)
        assert app.prefs.data['defaults'] == {'demo/portal': app.board_id}


async def test_one_board_opens_and_becomes_the_default_and_none_is_explained():
    app = from_checkout(Prefs(None, HOST), OneBoard())
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        assert app.board_id == 'demo/portal/-/boards/1' and app.prefs.data['defaults'] == {'demo/portal': app.board_id}
    app = from_checkout(Prefs(None, HOST), None, 'demo/api')
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        assert 'demo/api has no issue boards' in status(app) and app.prefs.data['defaults'] == {}


async def test_several_projects_in_the_remotes_are_asked_about():
    app = from_checkout(Prefs(None, HOST), None, 'demo/api', 'demo/portal')
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        assert app.screen.prompt.startswith('Found 2 GitLab projects') and not app.place
        await pilot.press('escape')
        assert 'No project chosen' in status(app)
        app.load_boards()
        await settle(app, pilot)
        await pilot.press(*'portal', 'enter')
        await settle(app, pilot)
        assert app.screen.prompt == 'Found project demo/portal — choose your default board'


# YouTrack board: the repository of a project's merge requests

def youtrack(prefs=None, cwd=None):
    client = YouTrackDemo()
    return BoardApp(client, prefs or YouTrackPrefs(None, client.base_url), gitlab=demo_gitlab, cwd=cwd), client


def hint(screen):
    return str(screen.query_one('#mr-hint').content)


async def test_a_saved_repository_wins_over_the_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(remotes, 'find', lambda *a: pytest.fail('looked at the checkout'))
    client = YouTrackDemo()
    app, _ = youtrack(YouTrackPrefs(None, client.base_url, {'repos': {'0-1': f'https://{HOST}/demo/portal'}}), tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, 'DEMO-3')
        await pilot.press('g')
        await settle(app, pilot)
        assert isinstance(app.screen, MergeRequestsScreen) and app.screen.home == 'demo/portal'


async def test_the_checkouts_repository_links_the_selected_project_only_once_gitlab_shows_it(tmp_path, monkeypatch):
    signed_in(monkeypatch, HOST)
    repo = checkout(tmp_path / 'code', ('origin', 'https://github.com/me/site'), ('gitlab', f'git@{HOST}:demo/api.git'))
    path = tmp_path / 'state.json'
    app, client = youtrack(YouTrackPrefs(str(path), YouTrackDemo.base_url), repo)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, 'WEB-2')
        await pilot.press('g')
        await settle(app, pilot)
        assert isinstance(app.screen, MergeRequestsScreen) and [m['title'] for m in app.screen.mrs] == [
            'Prices endpoint for the pricing page']
        assert app.prefs.data['repos'] == {'0-2': f'https://{HOST}/demo/api'}  # WEB only, not DEMO on the same board
        await pilot.press('escape')
        await pick(app, pilot, 'DEMO-3')
        await pilot.press('g')  # the repository is WEB's already: DEMO is asked, with it filled in
        await settle(app, pilot)
        assert isinstance(app.screen, CommentDialog) and app.screen.query_one(Input).value == f'https://{HOST}/demo/api'
        await pilot.press('escape')
    assert json.loads(path.read_text())[client.base_url]['repos'] == {'0-2': f'https://{HOST}/demo/api'}


async def test_a_remote_gitlab_does_not_show_is_not_saved(tmp_path, monkeypatch):
    signed_in(monkeypatch, HOST)
    app, _ = youtrack(cwd=checkout(tmp_path / 'code', ('origin', f'https://{HOST}/demo/gone.git')))
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, 'WEB-2')
        await pilot.press('g')
        await settle(app, pilot)
        assert 'no project or group demo/gone' in hint(app.screen) and app.prefs.data['repos'] == {}


async def test_several_remotes_are_a_choice_and_cancelling_saves_nothing(tmp_path, monkeypatch):
    signed_in(monkeypatch, HOST)
    repo = checkout(tmp_path / 'code', ('origin', f'git@{HOST}:demo/portal.git'), ('api', f'git@{HOST}:demo/api.git'))
    app, _ = youtrack(cwd=repo)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, 'WEB-2')
        await pilot.press('g')
        await settle(app, pilot)
        assert isinstance(app.screen, Picker) and 'this checkout has several' in app.screen.prompt
        await pilot.press('escape')
        assert app.prefs.data['repos'] == {}
        await pilot.press('g')
        await settle(app, pilot)
        await pilot.press(*'api', 'enter')
        await settle(app, pilot)
        assert app.prefs.data['repos'] == {'0-2': f'https://{HOST}/demo/api'}


async def test_no_gitlab_remote_or_no_checkout_asks_for_the_url(tmp_path, monkeypatch):
    signed_in(monkeypatch, HOST)
    for cwd in (checkout(tmp_path / 'code', ('origin', 'git@github.com:me/site.git')), tmp_path):
        app, _ = youtrack(cwd=cwd)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app, pilot)
            await pick(app, pilot, 'WEB-2')
            await pilot.press('g')
            await settle(app, pilot)
            assert isinstance(app.screen, CommentDialog) and app.screen.query_one(Input).value == ''


async def test_a_refused_sign_in_is_explained_and_nothing_is_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(glab, 'check', refuse)
    app, _ = youtrack(cwd=checkout(tmp_path / 'code', ('origin', f'git@{HOST}:demo/api.git')))
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pick(app, pilot, 'WEB-2')
        await pilot.press('g')
        await settle(app, pilot)
        assert not app.screen.is_modal and f'glab auth login --hostname {HOST}' in status(app)
        assert app.prefs.data['repos'] == {}


def test_the_youtrack_demo_never_looks_at_the_checkout(monkeypatch):
    from youtrack_board.app import main as youtrack_main
    monkeypatch.setattr(remotes, 'git', lambda *a: pytest.fail('read git remotes at start'))
    monkeypatch.setattr('sys.argv', ['youtrack-board', '--demo'])
    apps = []
    monkeypatch.setattr(BoardApp, 'run', lambda self: apps.append(self))
    youtrack_main()
    assert apps[0].cwd is None
