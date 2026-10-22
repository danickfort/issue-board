"""The GitLab projects a Git checkout's remotes point to, so a board can start from inside a code repository.

This follows glab's own repository detection (internal/cmdutils/remote_resolver.go, internal/glrepo/resolver.go):
the fetch URLs of `git remote -v`, SSH host aliases translated, a base remote chosen earlier in glab
(remote.<name>.glab-resolved) respected, and only remotes on hosts glab is signed in to. Unlike glab, which takes the
first remote when it cannot ask, several projects stay several here: the caller asks which one.

Only `git` and `ssh -G` run in the checkout, and both only read. API calls still run outside it (see glab.py). Remote
URLs can hold a user name and token, so only the host and project path are kept or shown.
"""
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import glab
from .merge_requests import repository
from .target import TargetError

SCP = re.compile(r'(?:[^@/]+@)?([A-Za-z0-9_][A-Za-z0-9_.\-]*):(?!//)(.+)')  # [user@]host:group/project.git
ALIAS = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.\-]*')
SSH_CONFIG = None  # tests give `ssh -G` their own config file instead of the user's
TIMEOUT = 10


class NoRemote(Exception):
    """Why the checkout names no GitLab project."""
    checkout = True  # False: not inside a Git checkout at all


@dataclass(frozen=True)
class Remote:
    names: str  # the git remote, or 'origin, upstream' when several name the same project
    host: str
    path: str

    def __str__(self):
        return f'{self.names} → {self.host}/{self.path}'


def git(cwd, *args):
    """git's output in the checkout, or None when git is missing or refuses (not a repository, unsafe owner…)."""
    try:
        done = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT,
                              env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def ssh_host(alias):
    """The host an SSH alias stands for in the SSH config, as `ssh -G` resolves it without connecting."""
    if not ALIAS.fullmatch(alias):
        return alias
    try:
        out = subprocess.run(['ssh', '-G', *(['-F', SSH_CONFIG] if SSH_CONFIG else []), alias], capture_output=True,
                             text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.TimeoutExpired):
        return alias
    return next((line.split()[1] for line in out.splitlines() if len(line.split()) == 2 and line.startswith('hostname ')),
                alias)


def project(url):
    """(host, path) of a remote URL on a network host, without its user, password or SSH port; None otherwise."""
    if '://' in url:
        parts = urlsplit(url)
        if parts.scheme not in ('https', 'http', 'ssh', 'git+ssh', 'ssh+git') or not parts.hostname:
            return None
        host, path = parts.hostname, parts.path
        try:
            if parts.scheme.startswith('http') and parts.port:  # an SSH port is not the web port
                host += f':{parts.port}'
        except ValueError:
            return None
        if not parts.scheme.startswith('http'):
            host = ssh_host(host)
    elif found := SCP.fullmatch(url):
        host, path = ssh_host(found.group(1)), found.group(2)
    else:
        return None  # a path on this computer
    try:
        return repository(f'https://{host}/{path.strip("/")}')
    except TargetError:
        return None


def remotes(cwd):
    """The checkout's fetch remotes that name a project on a network host, one Remote per project. A base remote
    chosen in glab wins alone. Raises NoRemote saying why there is none."""
    out = git(cwd, 'remote', '-v')
    if out is None:
        error = NoRemote('Not inside a Git checkout.' if shutil.which('git') else 'git is not installed.')
        error.checkout = False
        raise error
    found = {}
    for line in out.splitlines():
        name, _, rest = line.partition('\t')
        url, _, kind = rest.rpartition(' ')
        if kind == '(fetch)' and (place := project(url)):
            found.setdefault((place[0], place[1].casefold()), (place, []))[1].append(name)
    if not found:
        raise NoRemote('This Git checkout has no remote on a network host.' if out.strip() else 'This Git checkout has no remote.')
    projects = [Remote(', '.join(names), *place) for place, names in found.values()]
    chosen = git(cwd, 'config', '--get-regexp', r'^remote\..*\.glab-resolved(-base)?$') or ''
    for line in chosen.splitlines():  # glab's `remote.<name>.glab-resolved-base base`, or `base:group/project`
        key, _, value = line.partition(' ')
        remote = next((p for p in projects if key[len('remote.'):key.rindex('.')] in p.names.split(', ')), None)
        if remote and value == 'base':
            return [remote]
        if remote and value.startswith('base:'):
            try:
                return [Remote(remote.names, *repository(f'https://{remote.host}/{value[len("base:"):]}'))]
            except TargetError:
                pass
    return projects


def find(cwd, hostname=None):
    """(projects, versions): the checkout's remotes on GitLab hosts glab is signed in to, optionally on one host only,
    and glab's version line per host. A host glab has no sign-in for is left out without contacting it: `glab api`
    would send requests to any host. Raises NoRemote saying why there is none, or GlabError when glab is missing or
    a host it knows refuses the sign-in."""
    found = [r for r in remotes(cwd) if not hostname or r.host == hostname]
    if not found:
        raise NoRemote(f'No remote of this checkout is on {hostname}.')
    versions = {}
    for host in dict.fromkeys(r.host for r in found):
        try:
            versions[host] = glab.check(host)
        except glab.UnknownHost:
            pass
    projects = [r for r in found if r.host in versions]
    if not projects:
        raise NoRemote(f'No remote of this checkout is on a GitLab host glab is signed in to ({"; ".join(map(str, found))}).'
                       ' For a GitLab host: glab auth login --hostname <host>.')
    return projects, versions
