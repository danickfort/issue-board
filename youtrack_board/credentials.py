"""Where the YouTrack URL and token come from, and how they are kept.

In order: YOUTRACK_URL and YOUTRACK_TOKEN from the environment; the URL in this app's config file with its token in the
OS credential store; a single YouTrack entry in the Codex or Claude Code MCP configuration; otherwise setup asks.
A URL and its token always come from the same place, and the token is never written to a file.
"""
import asyncio
import contextlib
import getpass
import json
import os
import sys
import tomllib
import warnings
import webbrowser
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

from .client import Client, YouTrackError, check_url
from issueboard.prefs import write_json

SERVICE = 'youtrack-board'
# keyring backends that keep secrets in the OS store. Others, such as keyrings.alt's plaintext file, are refused.
SECURE_BACKENDS = ('keyring.backends.macOS', 'keyring.backends.Windows', 'keyring.backends.SecretService',
                   'keyring.backends.libsecret', 'keyring.backends.kwallet')
# Global MCP configuration only: the file, the key holding its servers, and the tool's name.
MCP_FILES = (('~/.codex/config.toml', 'mcp_servers', 'Codex'), ('~/.claude.json', 'mcpServers', 'Claude Code'))
REJECTED = (401, 403)


class SetupError(Exception):
    pass


@dataclass(frozen=True)
class Credentials:
    url: str
    token: str = field(repr=False)
    source: str  # completes "token …", e.g. "saved in macOS Keyring"


def normalize(url):
    url = url.strip().rstrip('/')
    return 'https://' + url if url and '://' not in url else url


def config_path():
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return os.path.join(base, 'youtrack-board', 'config.json')


def read_config():
    """The saved URL as {'url': …}, or {} and a warning when the file is unusable (setup then replaces it)."""
    try:
        with open(config_path(), encoding='utf-8') as file:
            data = json.load(file)
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as exc:
        return {}, f'Ignoring the unreadable config file {config_path()} ({type(exc).__name__}).'
    if not isinstance(data, dict) or not isinstance(data.get('url'), str):
        return {}, f'Ignoring {config_path()}: it has no valid "url".'
    return data, None


def store():
    """The OS credential store. Raises SetupError instead of falling back to anything less secure."""
    import keyring  # here, so --demo and --help never load a credential backend

    backend = keyring.get_keyring()
    if not all(type(b).__module__ in SECURE_BACKENDS for b in getattr(backend, 'backends', None) or [backend]):
        raise SetupError(f'{backend.name} is not an OS credential store')
    return backend


def from_env():
    url, token = (os.environ.get(name, '').strip() for name in ('YOUTRACK_URL', 'YOUTRACK_TOKEN'))
    if url and token:
        return Credentials(normalize(url), token, 'from the environment (YOUTRACK_URL, YOUTRACK_TOKEN)')
    if url or token:
        have, missing = ('YOUTRACK_URL', 'YOUTRACK_TOKEN') if url else ('YOUTRACK_TOKEN', 'YOUTRACK_URL')
        raise SetupError(f'{have} is set but {missing} is not. Set both, or unset {have} to use saved credentials.')
    return None


def saved(url):
    """The token saved for exactly this URL, so a token never goes to a host it was not saved for."""
    backend = store()
    token = backend.get_password(SERVICE, url)
    return Credentials(url, token, f'saved in {backend.name}') if token else None


def discover():
    """Global Codex and Claude Code MCP servers whose env sets both YOUTRACK_URL and YOUTRACK_TOKEN.

    Nothing else is read: no project files, no commands are run, and an entry using $VAR references is skipped rather
    than expanded. OAuth logins of remote MCP servers are not YouTrack tokens and are never looked at.
    Entries with the same URL and token are merged.
    """
    found = {}
    for path, key, tool in MCP_FILES:
        try:
            with open(os.path.expanduser(path), 'rb') as file:
                data = tomllib.load(file) if path.endswith('.toml') else json.load(file)
        except (OSError, ValueError):
            continue
        servers = data.get(key) if isinstance(data, dict) else None
        for name, server in servers.items() if isinstance(servers, dict) else ():
            env = server.get('env') if isinstance(server, dict) else None
            pair = [env.get(k) if isinstance(env, dict) else None for k in ('YOUTRACK_URL', 'YOUTRACK_TOKEN')]
            if not all(isinstance(v, str) and v.strip() and '$' not in v for v in pair):
                continue
            url, token = normalize(pair[0]), pair[1].strip()
            try:
                check_url(url)
            except YouTrackError:
                continue
            found.setdefault((url, token), []).append(f'{tool} MCP server "{name}" ({path})')
    return [Credentials(url, token, 'from ' + ' and '.join(labels)) for (url, token), labels in found.items()]


def check(credentials):
    """Sign in with GET users/me, which changes nothing. Raises YouTrackError, with .status if YouTrack answered."""
    async def me():
        client = Client(credentials.url, credentials.token)
        try:
            return await client.me()
        finally:
            await client.close()

    user = asyncio.run(me())
    if not isinstance(user, dict) or not user.get('login'):
        raise YouTrackError('This URL did not answer like YouTrack; check the base URL.')
    return user


def describe(credentials, user, notes=()):
    host = urlsplit(credentials.url).netloc
    who = f'{host} as {user["login"]}' if user else host
    if not user:
        notes = (*notes, 'Connection not verified; press r to retry.')
    return ' '.join([f'{who} · token {credentials.source}.', *notes])


def connect(force_setup=False, *, open_browser=False, interactive=True):
    """The credentials to start with and a line saying where they came from. Setup runs only when none work."""
    if not force_setup and (env := from_env()):
        try:
            return env, describe(env, signed_in(env))
        except YouTrackError as exc:
            raise SetupError(f'YouTrack at {env.url} rejected YOUTRACK_TOKEN: {exc} '
                             'Fix or unset YOUTRACK_URL and YOUTRACK_TOKEN.') from None
    config, warning = read_config()
    notes = [warning] if warning else []
    found = discover()
    if not force_setup:
        candidate = None
        if config.get('url'):
            try:
                candidate = saved(config['url'])
            except Exception as exc:  # a locked keychain, a missing backend: say so and try the next source
                notes.append(f'Could not read the saved token: {exc}.')
        candidate = candidate or (found[0] if len(found) == 1 else None)
        if candidate:
            try:
                return candidate, describe(candidate, signed_in(candidate), notes)
            except YouTrackError as exc:
                notes.append(f'YouTrack at {candidate.url} rejected the token {candidate.source}: {exc}')
                found = [c for c in found if not same(c, candidate)]
    if not interactive:
        raise SetupError(' '.join([*notes, 'No usable YouTrack credentials. Run youtrack-board --setup.']))
    return setup(found, config.get('url', ''), notes, open_browser=open_browser)


def same(a, b):
    return (a.url, a.token) == (b.url, b.token)


def signed_in(credentials):
    """The user, or None when YouTrack could not be checked now: the board then shows that error and can retry.
    Only a rejected token raises."""
    try:
        return check(credentials)
    except YouTrackError as exc:
        if exc.status in REJECTED:
            raise
        return None


def setup(found=(), url='', notes=(), *, open_browser=False):
    """Ask for a URL and token (or one of the found ones), check them read-only, and save them. Three tries at most."""
    if not sys.stdin.isatty():
        hint = f' {len(found)} YouTrack MCP entries were found; choose one with --setup.' if len(found) > 1 else ''
        raise SetupError(' '.join([*notes, 'No usable YouTrack credentials.' + hint,
                                   'Run youtrack-board --setup in a terminal, or set YOUTRACK_URL and YOUTRACK_TOKEN.']))
    for note in notes:
        print(note)
    print('Connect to YouTrack · 1. Instance URL  2. Permanent token  3. Verify and save\n'
          'Your token stays in the OS credential store, never a config file. Ctrl+C cancels.')
    try:
        for _ in range(3):
            credentials = choose(found) if found else None
            if not credentials:
                url = normalize(input(f'YouTrack URL{f" [{url}]" if url else ""}: ') or url)
                try:
                    check_url(url)
                except YouTrackError as exc:
                    print(f'{exc} Example: https://example.youtrack.cloud')
                    continue
                token_page = url + '/users/me?tab=account-security'
                print(f'Create a token: {token_page}\n'
                      'Choose Tokens → New token, name it YouTrack Board, and select the YouTrack scope.')
                if open_browser:
                    try:
                        opened = webbrowser.open(token_page)
                    except webbrowser.Error:
                        opened = False
                    if not opened:
                        print('Could not open a browser. Open the link above to create your token.')
                    open_browser = False  # do not reopen the browser on a failed verification
                token = hidden('Paste permanent token (hidden): ')
                if not token:
                    print('A token is required. Nothing was saved.')
                    continue
                credentials = Credentials(url, token, 'entered in setup')
            print(f'Checking {credentials.url}…')
            try:
                user = check(credentials)
            except YouTrackError as exc:
                print(f'Not saved: {exc}')
                found = [c for c in found if not same(c, credentials)]
                continue
            print(f'Signed in as {user.get("fullName") or user["login"]} ({user["login"]}).')
            credentials = remember(credentials)
            return credentials, describe(credentials, user)
    except (KeyboardInterrupt, EOFError):
        print()
        raise SetupError('Setup cancelled. Nothing was saved.') from None
    raise SetupError('Setup did not succeed after three tries. Nothing was saved.')


def choose(found):
    print('YouTrack credentials found on this computer:')
    for n, credentials in enumerate(found, 1):
        print(f'  {n}. {credentials.url}, token {credentials.source}')
    numbers = '1' if len(found) == 1 else f'1-{len(found)}'
    while True:
        answer = input(f'Use {numbers}, or press Enter to type a URL and token: ').strip()
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(found):
            return found[int(answer) - 1]


def hidden(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)  # getpass would otherwise echo the token as typed
        try:
            return getpass.getpass(prompt).strip()
        except getpass.GetPassWarning:
            raise SetupError('This terminal cannot hide typing, so no token was asked for. '
                             'Set YOUTRACK_URL and YOUTRACK_TOKEN instead.') from None


def remember(credentials):
    """Save the token in the OS credential store, then the URL in the config file. If the store cannot be used,
    keep the credentials for this session only and say so; nothing is written then."""
    config, _ = read_config()
    try:
        backend = store()
        previous = backend.get_password(SERVICE, credentials.url)
        backend.set_password(SERVICE, credentials.url, credentials.token)
    except Exception as exc:
        reason = str(exc).replace(credentials.token, '[redacted]')
        print(f'Could not save the token securely ({reason}). Using it for this session only; nothing was saved.')
        return replace(credentials, source='entered in setup, for this session only')
    try:
        write_json(config_path(), {**config, 'url': credentials.url})
    except OSError as exc:
        with contextlib.suppress(Exception):
            if previous is not None:
                backend.set_password(SERVICE, credentials.url, previous)
            else:
                backend.delete_password(SERVICE, credentials.url)
        print(f'Could not save {config_path()} ({exc}). Using the token for this session only; nothing was saved.')
        return replace(credentials, source='entered in setup, for this session only')
    if config.get('url') not in (None, credentials.url):  # the token for the URL this one replaces
        with contextlib.suppress(Exception):
            backend.delete_password(SERVICE, config['url'])
    print(f'Saved the token in {backend.name} (service "{SERVICE}") and the URL in {config_path()}.')
    if os.environ.get('YOUTRACK_URL') or os.environ.get('YOUTRACK_TOKEN'):
        print('YOUTRACK_URL or YOUTRACK_TOKEN is set in this environment; it takes precedence on the next start.')
    return replace(credentials, source=f'saved in {backend.name}')


def logout():
    """Remove the saved token and URL, and say which other sources would still be used."""
    import keyring.errors

    config, warning = read_config()
    lines = [warning] if warning else []
    if config.get('url'):
        backend = store()
        with contextlib.suppress(keyring.errors.PasswordDeleteError):  # already gone
            backend.delete_password(SERVICE, config['url'])
        if backend.get_password(SERVICE, config['url']):
            raise SetupError(f'Could not remove the token for {config["url"]} from {backend.name}. Nothing was changed.')
        os.remove(config_path())
        lines.append(f'Removed the token for {config["url"]} from {backend.name} and deleted {config_path()}.')
    else:
        lines.append('No saved YouTrack URL or token.')
    others = [c.source for c in discover()]
    if os.environ.get('YOUTRACK_TOKEN'):
        others.append('from the environment (YOUTRACK_URL, YOUTRACK_TOKEN)')
    if others:
        lines.append('These are left as they are and would still be used: token ' + '; token '.join(others) + '.')
    return '\n'.join(lines)
