import getpass
import io
import json
import os
import sys
import warnings
from functools import partial

import httpx
import keyring.errors
import pytest

from youtrack_board import credentials
from youtrack_board.app import BoardApp, main
from youtrack_board.client import Client
from youtrack_board.credentials import SetupError, config_path
from youtrack_board.demo import DemoClient

USERS = {'good-token': 'jane', 'other-token': 'sam'}
CODEX = '''
[mcp_servers.Liip_YouTrack]
command = "npx"
args = ["youtrack-mcp"]
[mcp_servers.Liip_YouTrack.env]
YOUTRACK_URL = "https://yt.example/"
YOUTRACK_TOKEN = "good-token"
'''


class Keychain:
    """Stands in for keyring's macOS backend, which is what the allowlist checks for."""
    name = 'macOS Keyring'

    def __init__(self, saved=None):
        self.saved = dict(saved or {})

    def get_password(self, service, url):
        assert service == 'youtrack-board'  # never anyone else's keychain items
        return self.saved.get(url)

    def set_password(self, service, url, token):
        assert service == 'youtrack-board'
        self.saved[url] = token

    def delete_password(self, service, url):
        assert service == 'youtrack-board'
        if url not in self.saved:
            raise keyring.errors.PasswordDeleteError('not found')
        del self.saved[url]


Keychain.__module__ = 'keyring.backends.macOS'


@pytest.fixture
def youtrack(monkeypatch):
    """A YouTrack that knows two tokens. Every request is recorded and must be the read-only sign-in check."""
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == 'GET' and request.url.path in ('/api/users/me', '/youtrack/api/users/me')
        if request.url.host == 'down.example':
            raise httpx.ConnectError('down')
        login = USERS.get(request.headers['Authorization'].removeprefix('Bearer '))
        if login:
            return httpx.Response(200, json={'login': login, 'fullName': login.title()})
        return httpx.Response(401, json={'error': 'Unauthorized', 'error_description': 'Invalid token'})

    monkeypatch.setattr(credentials, 'Client', partial(Client, transport=httpx.MockTransport(respond)))
    return requests


@pytest.fixture
def terminal(monkeypatch):
    """An interactive terminal with queued answers: lines for input(), tokens for the hidden prompt."""
    answers = {'line': [], 'secret': []}

    def take(kind, prompt=''):
        if not answers[kind]:
            raise EOFError
        return answers[kind].pop(0)

    monkeypatch.setattr(sys, 'stdin', type('Tty', (), {'isatty': lambda self: True})())
    monkeypatch.setattr('builtins.input', partial(take, 'line'))
    monkeypatch.setattr(getpass, 'getpass', partial(take, 'secret'))
    return answers


def start(monkeypatch, *args):
    """Run the command line; return the board app it would have started, if any."""
    monkeypatch.setattr('sys.argv', ['youtrack-board', *args])
    started = []
    monkeypatch.setattr(BoardApp, 'run', lambda self: started.append(self))
    main()
    return started[0] if started else None


def use(monkeypatch, backend):
    monkeypatch.setattr('keyring.get_keyring', lambda: backend)
    return backend


def save_config(data):
    os.makedirs(os.path.dirname(config_path()), exist_ok=True)
    with open(config_path(), 'w') as file:
        file.write(data if isinstance(data, str) else json.dumps(data))


def mcp(home, codex=None, claude=None):
    if codex is not None:
        (home / '.codex').mkdir()
        (home / '.codex' / 'config.toml').write_text(codex)
    if claude is not None:
        (home / '.claude.json').write_text(claude if isinstance(claude, str) else json.dumps(claude))


def test_environment_pair_still_starts_the_board_and_partial_pairs_are_refused(monkeypatch, youtrack):
    monkeypatch.setenv('YOUTRACK_URL', 'https://yt.example/')
    monkeypatch.setenv('YOUTRACK_TOKEN', 'good-token')
    app = start(monkeypatch)
    assert app.client.base_url == 'https://yt.example'
    assert app.connection == 'yt.example as jane · token from the environment (YOUTRACK_URL, YOUTRACK_TOKEN).'
    assert not os.path.exists(config_path())  # nothing saved, and the keychain was never opened (see conftest)

    monkeypatch.setenv('YOUTRACK_URL', 'https://down.example')
    assert start(monkeypatch).connection.startswith('down.example · token')  # offline: the board reports it and retries

    monkeypatch.setenv('YOUTRACK_URL', 'https://yt.example')
    monkeypatch.setenv('YOUTRACK_TOKEN', 'revoked-token')
    with pytest.raises(SystemExit) as stop:
        start(monkeypatch)
    assert 'rejected YOUTRACK_TOKEN' in stop.value.code and 'revoked-token' not in stop.value.code

    monkeypatch.delenv('YOUTRACK_TOKEN')
    with pytest.raises(SystemExit) as stop:
        start(monkeypatch)
    assert 'YOUTRACK_URL is set but YOUTRACK_TOKEN is not' in stop.value.code


def test_a_saved_token_is_only_used_for_the_url_it_was_saved_for(monkeypatch, youtrack):
    use(monkeypatch, Keychain({'https://a.example': 'good-token'}))
    monkeypatch.setattr(sys, 'stdin', io.StringIO())  # not a terminal: setup cannot ask
    save_config({'url': 'https://b.example'})
    with pytest.raises(SystemExit) as stop:
        start(monkeypatch)
    assert 'No usable YouTrack credentials' in stop.value.code and not youtrack

    os.remove(config_path())
    save_config({'url': 'https://a.example'})
    app = start(monkeypatch)
    assert app.client.base_url == 'https://a.example'
    assert app.connection == 'a.example as jane · token saved in macOS Keyring.'


def test_mcp_discovery_reads_only_youtrack_url_and_token_pairs(isolated):
    mcp(isolated, codex=CODEX + '''
[mcp_servers.half.env]
YOUTRACK_URL = "https://half.example"
[mcp_servers.indirect.env]
YOUTRACK_URL = "https://indirect.example"
YOUTRACK_TOKEN = "${YT_TOKEN}"
[mcp_servers.plain.env]
YOUTRACK_URL = "http://insecure.example"
YOUTRACK_TOKEN = "other-token"
[mcp_servers.remote]
url = "https://remote.example/mcp"
bearer_token_env_var = "YT"
''', claude={
        'mcpServers': {'yt': {'command': 'npx', 'env': {'YOUTRACK_URL': 'yt.example', 'YOUTRACK_TOKEN': 'good-token'}},
                       'Other': {'env': {'YOUTRACK_URL': 'https://other.example', 'YOUTRACK_TOKEN': 'other-token'}},
                       'oauth': {'type': 'http', 'url': 'https://yt.example/mcp',
                                 'headers': {'Authorization': 'Bearer oauth-token'}}},
        'projects': {'/work': {'mcpServers': {'p': {'env': {'YOUTRACK_URL': 'https://p.example', 'YOUTRACK_TOKEN': 'p'}}}}},
        'oauthAccount': {'accessToken': 'oauth-token'}})
    found = credentials.discover()
    assert [(c.url, c.token) for c in found] == [('https://yt.example', 'good-token'), ('https://other.example', 'other-token')]
    assert found[0].source == ('from Codex MCP server "Liip_YouTrack" (~/.codex/config.toml) '
                               'and Claude Code MCP server "yt" (~/.claude.json)')
    assert 'good-token' not in repr(found)
    (isolated / '.claude.json').write_text('{broken')
    assert [c.url for c in credentials.discover()] == ['https://yt.example']


def test_a_single_mcp_entry_is_used_in_place_and_nothing_is_saved(monkeypatch, youtrack, isolated):
    mcp(isolated, codex=CODEX)
    save_config('{not json')
    before = (isolated / '.codex' / 'config.toml').read_bytes()
    app = start(monkeypatch)
    assert app.client.base_url == 'https://yt.example'
    assert app.connection.startswith('yt.example as jane · token from Codex MCP server "Liip_YouTrack"')
    assert 'Ignoring the unreadable config file' in app.connection
    assert (isolated / '.codex' / 'config.toml').read_bytes() == before
    assert open(config_path()).read() == '{not json'  # the keychain was never opened either (see conftest)


def test_first_start_setup_checks_read_only_and_saves_token_in_keychain_and_url_in_config(
        monkeypatch, youtrack, terminal, capsys):
    keychain = use(monkeypatch, Keychain())
    terminal['line'] += ['http://yt.example', 'yt.example', '']  # plain HTTP is refused; Enter keeps the URL
    terminal['secret'] += ['wrong-token', 'good-token']
    app = start(monkeypatch)
    assert keychain.saved == {'https://yt.example': 'good-token'}
    assert json.loads(open(config_path()).read()) == {'url': 'https://yt.example'}
    assert os.stat(config_path()).st_mode & 0o777 == 0o600
    assert app.connection == 'yt.example as jane · token saved in macOS Keyring.'
    out = capsys.readouterr()
    assert 'Use an HTTPS YouTrack URL' in out.out and 'Not saved: YouTrack returned HTTP 401. Invalid token' in out.out
    assert 'token' in out.out and not any(t in out.out + out.err for t in ('good-token', 'wrong-token'))
    assert len(youtrack) == 2


def test_setup_stops_after_three_tries_and_can_be_cancelled(monkeypatch, youtrack, terminal):
    keychain = use(monkeypatch, Keychain())
    terminal['line'] += ['yt.example', '', '']
    terminal['secret'] += ['bad-1', 'bad-2', 'bad-3']
    with pytest.raises(SystemExit) as stop:
        start(monkeypatch)
    assert 'after three tries. Nothing was saved' in stop.value.code
    assert keychain.saved == {} and not os.path.exists(config_path()) and len(youtrack) == 3
    with pytest.raises(SystemExit) as stop:
        start(monkeypatch, '--setup')  # no answers: like Ctrl+D
    assert 'Setup cancelled' in stop.value.code


def test_setup_refuses_to_ask_for_the_token_where_typing_would_show(monkeypatch, youtrack, terminal):
    def echoing(prompt):  # what getpass does when it cannot turn off echo: warn, then read in plain sight
        warnings.warn('Can not control echo on the terminal.', getpass.GetPassWarning)
        return 'good-token'

    use(monkeypatch, Keychain())
    monkeypatch.setattr(getpass, 'getpass', echoing)
    terminal['line'].append('yt.example')
    with pytest.raises(SystemExit) as stop:
        start(monkeypatch)
    assert 'cannot hide typing' in stop.value.code and not youtrack


def test_insecure_or_locked_stores_keep_credentials_for_this_session_only(monkeypatch, youtrack, terminal, capsys):
    class Plaintext(Keychain):
        name = 'PlaintextKeyring'

    class Locked(Keychain):
        def set_password(self, service, url, token):
            raise keyring.errors.KeyringLocked("Can't store password on keychain: locked")

    Plaintext.__module__, Locked.__module__ = 'keyrings.alt.file', 'keyring.backends.macOS'
    for backend, reason in ((Plaintext(), 'PlaintextKeyring is not an OS credential store'), (Locked(), 'locked')):
        use(monkeypatch, backend)
        terminal['line'].append('yt.example')
        terminal['secret'].append('good-token')
        app = start(monkeypatch)
        assert app.client.base_url == 'https://yt.example' and 'for this session only' in app.connection
        assert backend.saved == {} and not os.path.exists(config_path())
        assert reason in capsys.readouterr().out

    class Chain:  # keyring's chainer may fall back to its later members, so each must be secure
        name, backends = 'ChainerBackend', [Keychain(), Plaintext()]

    use(monkeypatch, Chain())
    with pytest.raises(SetupError):
        credentials.store()


def test_a_rejected_saved_token_asks_once_for_a_new_one(monkeypatch, youtrack, terminal, isolated, capsys):
    keychain = use(monkeypatch, Keychain({'https://yt.example': 'revoked-token'}))
    save_config({'url': 'https://yt.example'})
    mcp(isolated, claude={'mcpServers': {'yt': {'env': {'YOUTRACK_URL': 'https://yt.example',
                                                        'YOUTRACK_TOKEN': 'revoked-token'}}}})
    terminal['line'].append('')  # keep the saved URL
    terminal['secret'].append('good-token')
    app = start(monkeypatch)
    out = capsys.readouterr().out
    assert 'rejected the token saved in macOS Keyring' in out
    assert 'found on this computer' not in out  # the same rejected pair in the MCP config is not offered again
    assert keychain.saved == {'https://yt.example': 'good-token'} and 'jane' in app.connection


def test_several_mcp_entries_ask_which_one_to_save(monkeypatch, youtrack, terminal, isolated, capsys):
    mcp(isolated, codex=CODEX, claude={'mcpServers': {'Other': {'env': {'YOUTRACK_URL': 'https://other.example',
                                                                        'YOUTRACK_TOKEN': 'other-token'}}}})
    keychain = use(monkeypatch, Keychain({'https://old.example': 'old-token'}))
    save_config({'url': 'https://old.example'})
    terminal['line'] += ['9', '2']  # out of range: asked again
    app = start(monkeypatch, '--setup')
    out = capsys.readouterr().out
    assert '1. https://yt.example, token from Codex MCP server "Liip_YouTrack"' in out
    assert '2. https://other.example, token from Claude Code MCP server "Other"' in out
    assert app.client.base_url == 'https://other.example' and 'as sam' in app.connection
    assert keychain.saved == {'https://other.example': 'other-token'}  # the replaced URL's token is removed
    assert [r.url.host for r in youtrack] == ['other.example']  # each token only ever goes to its own host

    monkeypatch.setattr(sys, 'stdin', io.StringIO())
    os.remove(config_path())
    with pytest.raises(SystemExit) as stop:
        start(monkeypatch)
    assert '2 YouTrack MCP entries were found; choose one with --setup' in stop.value.code


def test_logout_removes_the_saved_token_and_url_and_names_what_remains(monkeypatch, isolated, capsys):
    keychain = use(monkeypatch, Keychain({'https://yt.example': 'good-token'}))
    save_config({'url': 'https://yt.example'})
    mcp(isolated, codex=CODEX)
    assert start(monkeypatch, '--logout') is None
    out = capsys.readouterr().out
    assert keychain.saved == {} and not os.path.exists(config_path())
    assert 'Removed the token for https://yt.example from macOS Keyring' in out
    assert 'would still be used: token from Codex MCP server "Liip_YouTrack"' in out
    start(monkeypatch, '--logout')
    assert 'No saved YouTrack URL or token.' in capsys.readouterr().out


def test_demo_and_help_never_look_for_credentials(monkeypatch, isolated):
    def forbidden(*args):
        raise AssertionError('looked for credentials')

    monkeypatch.setenv('YOUTRACK_URL', 'https://yt.example')  # even a half-set environment is ignored
    mcp(isolated, codex=CODEX)
    for name in ('discover', 'read_config', 'from_env', 'connect'):
        monkeypatch.setattr(credentials, name, forbidden)
    app = start(monkeypatch, '--demo')
    assert isinstance(app.client, DemoClient) and app.connection is None
    with pytest.raises(SystemExit) as stop:
        start(monkeypatch, '--help')
    assert stop.value.code == 0
    assert [p.name for p in isolated.iterdir()] == ['.codex']  # no config or state directory was created


def test_setup_guides_token_creation_and_opens_browser_once(monkeypatch, youtrack, terminal, capsys):
    use(monkeypatch, Keychain())
    opened = []
    monkeypatch.setattr(credentials.webbrowser, 'open', lambda url: opened.append(url) or True)
    terminal['line'] += ['https://yt.example/youtrack', '']
    terminal['secret'] += ['wrong-token', 'good-token']
    account, _ = credentials.connect(force_setup=True, open_browser=True)
    assert account.url == 'https://yt.example/youtrack'
    assert opened == ['https://yt.example/youtrack/users/me?tab=account-security']
    output = capsys.readouterr().out
    assert '1. Instance URL' in output and 'YouTrack scope' in output
    assert all(token not in output for token in ('wrong-token', 'good-token'))


def test_setup_empty_token_does_not_contact_server_and_browser_failure_is_recoverable(
        monkeypatch, youtrack, terminal, capsys):
    use(monkeypatch, Keychain())
    monkeypatch.setattr(credentials.webbrowser, 'open', lambda url: False)
    terminal['line'] += ['yt.example', '']
    terminal['secret'] += ['', 'good-token']
    account, _ = credentials.connect(force_setup=True, open_browser=True)
    assert account.url == 'https://yt.example' and len(youtrack) == 1
    output = capsys.readouterr().out
    assert 'Could not open a browser' in output and 'A token is required' in output


def test_status_resolution_never_prompts(monkeypatch, youtrack, terminal):
    with pytest.raises(SetupError, match='Run youtrack-board --setup'):
        credentials.connect(interactive=False)
    monkeypatch.setenv('YOUTRACK_URL', 'https://yt.example')
    monkeypatch.setenv('YOUTRACK_TOKEN', 'good-token')
    account, text = credentials.connect(interactive=False)
    assert account.url == 'https://yt.example' and 'as jane' in text
    assert len(youtrack) == 1


def test_auth_status_checks_account_without_starting_board(monkeypatch, youtrack, capsys):
    monkeypatch.setenv('YOUTRACK_URL', 'https://yt.example')
    monkeypatch.setenv('YOUTRACK_TOKEN', 'good-token')
    assert start(monkeypatch, '--auth-status') is None
    output = capsys.readouterr().out
    assert 'as jane' in output and 'from the environment' in output and 'good-token' not in output
    assert len(youtrack) == 1
    monkeypatch.delenv('YOUTRACK_TOKEN')
    monkeypatch.delenv('YOUTRACK_URL')
    with pytest.raises(SystemExit, match='No usable YouTrack credentials'):
        start(monkeypatch, '--auth-status')


def test_open_flag_requires_explicit_setup(monkeypatch):
    with pytest.raises(SystemExit) as stop:
        start(monkeypatch, '--open')
    assert stop.value.code == 2


def test_failed_config_write_preserves_previously_saved_token(monkeypatch, capsys):
    keychain = use(monkeypatch, Keychain({'https://yt.example': 'existing-token'}))
    save_config({'url': 'https://yt.example'})

    def full_disk(*args):
        raise OSError('No space left on device')

    monkeypatch.setattr(credentials, 'write_json', full_disk)
    result = credentials.remember(credentials.Credentials('https://yt.example', 'replacement-token', 'entered in setup'))
    assert keychain.saved == {'https://yt.example': 'existing-token'}
    assert json.loads(open(config_path()).read()) == {'url': 'https://yt.example'}
    assert 'session only' in result.source
    output = capsys.readouterr().out
    assert 'existing-token' not in output and 'replacement-token' not in output
