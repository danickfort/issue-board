"""Where the Jira Cloud site, account email and API token come from, and how they are kept.

In order: JIRA_URL, JIRA_EMAIL and JIRA_API_TOKEN from the environment; a site and its email in this app's config
file with its token in the OS credential store (several sites can be saved, the last one set up opens by default); the ALS workspace's macOS keychain item, which holds a token for
https://richemont.atlassian.net only and is never sent to another site; otherwise setup asks.
A site, its email and its token always come from the same place, and the token is never written to a file.
"""
import asyncio
import contextlib
import getpass
import json
import os
import re
import subprocess
import sys
import warnings
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

from issueboard.prefs import write_json
from .client import Client, JiraError, check_url

SERVICE = 'jira-board'
# The ALS workspace tooling keeps account = email, password = API token in this item, for Richemont's site alone.
LEGACY_SERVICE, LEGACY_SITE = 'als-atlassian-api', 'https://richemont.atlassian.net'
SECURITY = '/usr/bin/security'
ENV = ('JIRA_URL', 'JIRA_EMAIL', 'JIRA_API_TOKEN')
TOKEN_PAGE = 'https://id.atlassian.com/manage-profile/security/api-tokens'
# keyring backends that keep secrets in the OS store. Others, such as keyrings.alt's plaintext file, are refused.
SECURE_BACKENDS = ('keyring.backends.macOS', 'keyring.backends.Windows', 'keyring.backends.SecretService',
                   'keyring.backends.libsecret', 'keyring.backends.kwallet')
REJECTED = (401, 403)


class SetupError(Exception):
    pass


@dataclass(frozen=True)
class Credentials:
    url: str
    email: str
    token: str = field(repr=False)
    source: str  # completes "API token …", e.g. "saved in macOS Keyring"


def normalize(url):
    url = url.strip().rstrip('/')
    url = 'https://' + url if url and '://' not in url else url
    parts = urlsplit(url)
    return parts._replace(netloc=parts.netloc.lower()).geturl() if parts.netloc else url


def config_path():
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return os.path.join(base, 'jira-board', 'config.json')


def read_config():
    """The saved sites as {'url': default site, 'sites': {site: email}}, or {} and a warning when the file is unusable."""
    try:
        with open(config_path(), encoding='utf-8') as file:
            data = json.load(file)
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as exc:
        return {}, f'Ignoring the unreadable config file {config_path()} ({type(exc).__name__}).'
    sites = data.get('sites') if isinstance(data, dict) else None
    if not isinstance(sites, dict) or not all(isinstance(e, str) and e for e in sites.values()) or \
            data.get('url') not in sites:
        return {}, f'Ignoring {config_path()}: it has no valid "url" and "sites".'
    return data, None


def store():
    """The OS credential store. Raises SetupError instead of falling back to anything less secure."""
    import keyring  # here, so --demo and --help never load a credential backend

    backend = keyring.get_keyring()
    if not all(type(b).__module__ in SECURE_BACKENDS for b in getattr(backend, 'backends', None) or [backend]):
        raise SetupError(f'{backend.name} is not an OS credential store')
    return backend


def from_env():
    values = {name: os.environ.get(name, '').strip() for name in ENV}
    if all(values.values()):
        return Credentials(normalize(values['JIRA_URL']), values['JIRA_EMAIL'], values['JIRA_API_TOKEN'],
                           'from the environment (JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN)')
    if any(values.values()):
        missing = ', '.join(n for n, v in values.items() if not v)
        raise SetupError(f'{missing} {"is" if "," not in missing else "are"} not set, but other JIRA_ variables are. '
                         'Set all of JIRA_URL, JIRA_EMAIL and JIRA_API_TOKEN, or unset them to use saved credentials.')
    return None


def saved(config, site):
    """The token saved for exactly this site, so a token never goes to a host it was not saved for."""
    backend = store()
    token = backend.get_password(SERVICE, site)
    return Credentials(site, config['sites'][site], token, f'saved in {backend.name}') if token else None


def keychain_item(service, password=True):
    """(account, password) of a macOS keychain generic password, read with Apple's own tool, or None. Without
    `password`, only whether it exists: (account, None).

    The password is captured in memory only; it is never an argument, a file or a log line."""
    if sys.platform != 'darwin' or not os.path.exists(SECURITY):
        return None
    try:
        found = subprocess.run([SECURITY, 'find-generic-password', '-s', service], capture_output=True, text=True,
                               timeout=20)
        account = re.search(r'"acct"<blob>="([^"]*)"', found.stdout) if found.returncode == 0 else None
        if not account or not password:
            return account and (account.group(1), None)
        secret = subprocess.run([SECURITY, 'find-generic-password', '-s', service, '-a', account.group(1), '-w'],
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return (account.group(1), secret.stdout.strip()) if secret.returncode == 0 and secret.stdout.strip() else None


def legacy():
    """The ALS keychain item, bound to the one site it was made for."""
    item = keychain_item(LEGACY_SERVICE)
    if item and item[0]:
        return Credentials(LEGACY_SITE, item[0], item[1], f'from the ALS keychain item "{LEGACY_SERVICE}"')
    return None


def check(credentials):
    """Sign in with GET /rest/api/3/myself, which changes nothing. Raises JiraError, with .status if Jira answered."""
    async def me():
        client = Client(credentials.url, credentials.email, credentials.token)
        try:
            return await client.me()
        finally:
            await client.close()

    user = asyncio.run(me())
    if not isinstance(user, dict) or not user.get('accountId'):
        raise JiraError('This URL did not answer like Jira Cloud; check the site address.')
    return user


def describe(credentials, user, notes=()):
    host = urlsplit(credentials.url).netloc
    who = f'{host} as {user.get("displayName") or credentials.email}' if user else host
    if not user:
        notes = (*notes, 'Connection not verified; press r to retry.')
    return ' '.join([f'{who} · API token for {credentials.email} {credentials.source}.', *notes])


def signed_in(credentials):
    """The user, or None when Jira could not be checked now: the board then shows that error and can retry.
    Only rejected credentials raise."""
    try:
        return check(credentials)
    except JiraError as exc:
        if exc.status in REJECTED:
            raise
        return None


def connect(site=None, force_setup=False, *, interactive=True):
    """The credentials for `site` (any site if None) and a line saying where they came from. Setup runs only when
    none work. Credentials for another site are never used for it."""
    site = normalize(site) if site else None
    if not force_setup and (env := from_env()):
        if site and env.url != site:
            raise SetupError(f'JIRA_URL is {env.url}, but this board is on {site}. Nothing was opened. Unset JIRA_URL, '
                             'JIRA_EMAIL and JIRA_API_TOKEN, or set them for that site.')
        try:
            return env, describe(env, signed_in(env))
        except JiraError as exc:
            raise SetupError(f'Jira at {env.url} rejected JIRA_EMAIL and JIRA_API_TOKEN: {exc} Fix or unset them.') from None
    config, warning = read_config()
    notes = [warning] if warning else []
    if not force_setup:
        candidates = []
        if (site or config.get('url')) in config.get('sites', {}):
            try:
                candidates.append(saved(config, site or config['url']))
            except Exception as exc:  # a locked keychain, a missing backend: say so and try the next source
                notes.append(f'Could not read the saved token: {exc}.')
        if not site or site == LEGACY_SITE:
            candidates.append(legacy())
        for candidate in filter(None, candidates):
            try:
                return candidate, describe(candidate, signed_in(candidate), notes)
            except JiraError as exc:
                notes.append(f'Jira at {candidate.url} rejected the API token {candidate.source}: {exc}')
    if not interactive:
        raise SetupError(' '.join([*notes, f'No usable Jira credentials{f" for {site}" if site else ""}. '
                                   'Run jira-board --setup, or set JIRA_URL, JIRA_EMAIL and JIRA_API_TOKEN.']))
    found = legacy() if not site or site == LEGACY_SITE else None
    url = site or config.get('url', '')
    return setup(url, config.get('sites', {}).get(url, ''), notes, found, fixed=bool(site))


def setup(url='', email='', notes=(), found=None, *, fixed=False):
    """Ask for a site, email and API token (or take the ALS keychain item), check them read-only, and save them.
    fixed: the site is the one a folder's board is on, so it is not asked for. Three tries at most."""
    if not sys.stdin.isatty():
        raise SetupError(' '.join([*notes, 'No usable Jira credentials. Run jira-board --setup in a terminal, or set '
                                   'JIRA_URL, JIRA_EMAIL and JIRA_API_TOKEN.']))
    for note in notes:
        print(note)
    print('Connect to Jira Cloud · 1. Site address  2. Atlassian account email  3. API token  4. Verify and save\n'
          'Your token stays in the OS credential store, never a config file. Ctrl+C cancels.')
    try:
        for _ in range(3):
            credentials = None
            if found and input(f'Use the API token for {found.email} {found.source}, for {found.url}? [Y/n] ').strip() \
                    .lower() in ('', 'y', 'yes'):
                credentials = found
            if not credentials:
                if not fixed:
                    url = normalize(input(f'Jira site{f" [{url}]" if url else ""}: ') or url)
                try:
                    check_url(url)
                except JiraError as exc:
                    print(exc)
                    continue
                email = input(f'Atlassian account email{f" [{email}]" if email else ""}: ').strip() or email
                if not email:
                    print('An email is required. Nothing was saved.')
                    continue
                print(f'Create an API token at {TOKEN_PAGE}: choose Create API token (not "with scopes").')
                token = hidden('Paste API token (hidden): ')
                if not token:
                    print('A token is required. Nothing was saved.')
                    continue
                credentials = Credentials(url, email, token, 'entered in setup')
            print(f'Checking {credentials.url}…')
            try:
                user = check(credentials)
            except JiraError as exc:
                print(f'Not saved: {exc}')
                found = None if credentials is found else found
                continue
            print(f'Signed in as {user.get("displayName") or credentials.email}.')
            if credentials is not found:
                credentials = remember(credentials)
            return credentials, describe(credentials, user)
    except (KeyboardInterrupt, EOFError):
        print()
        raise SetupError('Setup cancelled. Nothing was saved.') from None
    raise SetupError('Setup did not succeed after three tries. Nothing was saved.')


def hidden(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)  # getpass would otherwise echo the token as typed
        try:
            return getpass.getpass(prompt).strip()
        except getpass.GetPassWarning:
            raise SetupError('This terminal cannot hide typing, so no token was asked for. '
                             'Set JIRA_URL, JIRA_EMAIL and JIRA_API_TOKEN instead.') from None


def remember(credentials):
    """Save the token in the OS credential store, then the site and email in the config file, as the default site.
    If the store cannot be used, keep the credentials for this session only and say so; nothing is written then."""
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
        write_json(config_path(), {'url': credentials.url,
                                   'sites': {**config.get('sites', {}), credentials.url: credentials.email}})
    except OSError as exc:
        with contextlib.suppress(Exception):
            if previous is not None:
                backend.set_password(SERVICE, credentials.url, previous)
            else:
                backend.delete_password(SERVICE, credentials.url)
        print(f'Could not save {config_path()} ({exc}). Using the token for this session only; nothing was saved.')
        return replace(credentials, source='entered in setup, for this session only')
    print(f'Saved the token in {backend.name} (service "{SERVICE}") and the site and email in {config_path()}.')
    if any(os.environ.get(name) for name in ENV):
        print('JIRA_URL, JIRA_EMAIL or JIRA_API_TOKEN is set in this environment; it takes precedence on the next start.')
    return replace(credentials, source=f'saved in {backend.name}')


def logout():
    """Remove this app's saved tokens and config, and say which other sources would still be used. The ALS keychain
    item belongs to the ALS tools and is never changed."""
    import keyring.errors

    config, warning = read_config()
    lines = [warning] if warning else []
    if config.get('sites'):
        backend = store()
        for site in config['sites']:
            with contextlib.suppress(keyring.errors.PasswordDeleteError):  # already gone
                backend.delete_password(SERVICE, site)
            if backend.get_password(SERVICE, site):
                raise SetupError(f'Could not remove the token for {site} from {backend.name}. The config file was kept.')
        os.remove(config_path())
        lines.append(f'Removed the tokens for {", ".join(config["sites"])} from {backend.name} and deleted '
                     f'{config_path()}.')
    else:
        lines.append('No saved Jira site or token.')
    others = []
    if keychain_item(LEGACY_SERVICE, password=False):
        others.append(f'the API token in the ALS keychain item "{LEGACY_SERVICE}", for {LEGACY_SITE}')
    if any(os.environ.get(name) for name in ENV):
        others.append('the environment (JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN)')
    if others:
        lines.append('These are left as they are and would still be used: ' + '; '.join(others) + '.')
    return '\n'.join(lines)
