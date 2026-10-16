"""Every GitLab request goes through the official GitLab CLI, `glab api`, with glab's own sign-in.

The app never sees or stores a token: glab reads it from its own configuration or keyring. Requests use an explicit
--hostname, run outside any Git checkout (so glab cannot pick a host or fill placeholders from a repository), and pass
no user data in argv: GraphQL documents go to stdin, REST reads are GETs whose paths contain only encoded values.
"""
import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
from urllib.parse import urlencode

TIMEOUT = 30  # seconds; GitLab's own GraphQL timeout is 30 s too
INSTALL = ('Install the GitLab CLI (glab): brew install glab, or see '
           'https://gitlab.com/gitlab-org/cli#installation. Then sign in with: glab auth login --hostname {host}')
LOGIN = ('glab is not signed in to {host}. Sign in with: glab auth login --hostname {host}  '
         '(check with: glab auth status --hostname {host})')
# Quiet, non-interactive glab. GLAB_DEBUG_HTTP and DEBUG would print request headers, so they are removed.
ENV = {'GLAB_CHECK_UPDATE': 'false', 'GLAB_NO_PROMPT': 'true', 'GLAB_SEND_TELEMETRY': 'false',
       'GLAB_SHOW_WHATS_NEW': 'false', 'GLAB_NOTIFY_SKILL_UPDATES': 'false', 'NO_COLOR': '1'}
DROP = ('GLAB_DEBUG_HTTP', 'DEBUG')
SECRET = re.compile(r'gl[a-z]{2,12}-[A-Za-z0-9_.\-]{16,}|(?i:bearer|private-token:?)\s+\S+')
SAFE_PATH = re.compile(r'[A-Za-z0-9_.~%/?&=\-]+')  # an encoded REST path and query: no ':' placeholders, no spaces
HOST = re.compile(r'[a-z0-9.\-]+(:\d{1,5})?')


class GlabError(Exception):
    status = None  # the HTTP status, when GitLab answered with one


class UnknownHost(GlabError):
    """glab has no sign-in for this host, so it may not be a GitLab at all."""


def env():
    return {**{k: v for k, v in os.environ.items() if k not in DROP}, **ENV}


def redact(text):
    return SECRET.sub('[redacted]', text)


def error(stdout, stderr, host):
    """A readable GlabError from a failed glab run: GraphQL or REST messages first, then glab's own last word."""
    messages, status = [], None
    with contextlib.suppress(ValueError):
        data = json.loads(stdout) if stdout.strip() else None
        if isinstance(data, dict):
            messages += [str(e.get('message')) for e in data.get('errors') or [] if isinstance(e, dict)]
            if not messages and (data.get('message') or data.get('error')):
                messages.append(str(data.get('message') or data.get('error')))
    lines = [line.strip().removeprefix('glab: ') for line in stderr.splitlines() if line.strip()]
    lines = [line for line in lines if not line.startswith(('DEPRECATION WARNING', 'ERROR'))]
    found = re.search(r'\(HTTP (\d{3})\)', stderr)
    status = int(found.group(1)) if found else None
    text = '; '.join(dict.fromkeys(messages)) or (lines[-1] if lines else 'glab failed without a message.')
    if status == 401 or re.search(r'no token|not authenticated|401 Unauthorized|invalid_token', stderr + text, re.I):
        status, text = 401, LOGIN.format(host=host)
    elif status == 403:
        text = f'GitLab refused this (HTTP 403): your role does not allow it. {text}'
    elif status == 429:
        text = f'GitLab is rate limiting requests (HTTP 429); wait a minute. {text}'
    exc = GlabError(redact(text)[:400])
    exc.status = status
    return exc


class Glab:
    def __init__(self, host, binary='glab'):
        if not HOST.fullmatch(host):
            raise GlabError(f'Not a GitLab host name: {host!r}')
        self.host, self.binary = host, binary

    async def run(self, args, stdin=None):
        """glab's stdout for one call. A cancelled or timed-out call kills glab and waits for it, so none is left."""
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary, *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=os.path.abspath(os.sep), env=env())
        except FileNotFoundError:
            raise GlabError(INSTALL.format(host=self.host)) from None
        try:
            out, err = await asyncio.wait_for(process.communicate(stdin.encode() if stdin else b''), TIMEOUT)
        except BaseException as exc:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await asyncio.shield(process.wait())
            if isinstance(exc, asyncio.TimeoutError):
                raise GlabError(f'{self.host} did not answer within {TIMEOUT} s.') from None
            raise
        out, err = out.decode(errors='replace'), err.decode(errors='replace')
        if process.returncode:
            raise error(out, err, self.host)
        return out

    def parse(self, out):
        try:
            return json.loads(out) if out.strip() else None
        except ValueError:
            raise GlabError('glab returned something other than JSON. Is it up to date? (glab --version)') from None

    async def graphql(self, document):
        """POST one GraphQL document, read from stdin (-F query=@-), and return its data."""
        data = self.parse(await self.run(['api', 'graphql', '--hostname', self.host, '-F', 'query=@-'], document))
        if not isinstance(data, dict) or data.get('errors') or not isinstance(data.get('data'), dict):
            raise error(json.dumps(data), '', self.host)
        return data['data']

    async def get(self, path, **params):
        """GET a REST path (already encoded) with query parameters, as JSON."""
        endpoint = path + ('?' + urlencode(params) if params else '')
        if not SAFE_PATH.fullmatch(endpoint):
            raise GlabError(f'Refusing an unsafe API path: {endpoint!r}')
        return self.parse(await self.run(['api', '--hostname', self.host, '--method', 'GET', endpoint]))

    async def post(self, path, payload):
        """POST a JSON body, read from stdin, to a REST path. glab 1.115 sends no Content-Type with --input, so it is set."""
        if not SAFE_PATH.fullmatch(path):
            raise GlabError(f'Refusing an unsafe API path: {path!r}')
        return self.parse(await self.run(['api', '--hostname', self.host, '--method', 'POST', path, '--input', '-',
                                          '--header', 'Content-Type: application/json'], json.dumps(payload)))

    async def pages(self, path, cap, per_page=100, **params):
        """Up to `cap` pages of a REST list, and whether more remain. Stops at the first short page."""
        items = []
        for page in range(1, cap + 1):
            batch = await self.get(path, **params, per_page=per_page, page=page)
            if not isinstance(batch, list):
                raise GlabError('Expected a list from GitLab.')
            items += batch
            if len(batch) < per_page:
                return items, False
        return items, True


def check(host, binary='glab'):
    """Before the board opens: glab is installed and signed in to the host. Returns glab's version line.
    Only `glab --version` and `glab auth status --hostname` run; their output is never shown with a token."""
    if not shutil.which(binary):
        raise GlabError(INSTALL.format(host=host))
    run = dict(capture_output=True, text=True, timeout=TIMEOUT, cwd=os.path.abspath(os.sep), env=env())
    try:
        version = subprocess.run([binary, '--version'], **run).stdout.strip().splitlines()[:1]
        status = subprocess.run([binary, 'auth', 'status', '--hostname', host], **run)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GlabError(f'Could not run glab: {type(exc).__name__}') from None
    if status.returncode:
        said = status.stdout + status.stderr
        if 'has not been authenticated' in said:  # glab answers this from its own config, without a request
            raise UnknownHost(LOGIN.format(host=host))
        offline = re.search(r'[^\n]*(dial tcp|no such host|connection refused|timeout|i/o timeout)[^\n]*', said, re.I)
        raise GlabError(f'Could not reach {host}: {redact(offline.group().strip())[:200]}' if offline else LOGIN.format(host=host))
    return version[0] if version else 'glab'
