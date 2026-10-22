"""Issue Board: one command for YouTrack Board, GitLab Board and Jira Board that remembers which board each project
folder opens.

A folder is its Git checkout's root, so subfolders share it and each worktree has its own; outside Git, the folder
itself. The choices live in the user's config folder, never in the project, and one is saved only after its setup
succeeds. Credentials stay where each board keeps them: the OS credential store for YouTrack and Jira, glab for GitLab.
"""
import argparse
import asyncio
import json
import os
import re
import subprocess
import sys

from rich.text import Text
from textual.app import App

from gitlab_board import app as gitlab_app
from gitlab_board import glab, remotes, target
from gitlab_board.client import Client as GitLab
from gitlab_board.glab import GlabError
from jira_board import app as jira_app
from jira_board import credentials as jira_credentials
from jira_board.client import Client as Jira
from jira_board.client import JiraError
from youtrack_board import app as youtrack_app
from youtrack_board import credentials
from youtrack_board.client import Client as YouTrack
from youtrack_board.client import YouTrackError
from .prefs import default_path, write_json
from .widgets import Picker, plain

BACKENDS = {'youtrack': 'YouTrack', 'gitlab': 'GitLab', 'jira': 'Jira'}
ISSUE_ID = re.compile(r'(?<![A-Za-z0-9])([A-Z][A-Z0-9_]*)-\d+')  # an issue key such as WEB-12, as YouTrack and Jira use


class Cancelled(Exception):
    pass


def config_path():
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return os.path.join(base, 'issueboard', 'directories.json')


def directory(cwd=os.curdir):
    """The folder a choice belongs to: the checkout's root (a worktree is its own), else this folder."""
    root = remotes.git(cwd, 'rev-parse', '--show-toplevel')
    return os.path.realpath(root.strip() if root else cwd)


def read_directories():
    """Saved choices by folder. An unreadable file stops the app, so the next save cannot overwrite it."""
    try:
        with open(config_path(), encoding='utf-8') as file:
            data = json.load(file)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise SystemExit(f'issueboard: cannot read {config_path()} ({type(exc).__name__}). Fix or move it, then retry.')
    if not isinstance(data, dict) or not isinstance(data.get('directories', {}), dict):
        raise SystemExit(f'issueboard: {config_path()} is not an Issue Board folder list. Fix or move it, then retry.')
    return data.get('directories', {})


def saved_choice(choices, folder):
    choice = choices.get(folder)
    if not isinstance(choice, dict):
        return None
    if choice.get('backend') in ('youtrack', 'jira') and isinstance(choice.get('url'), str) \
            and isinstance(choice.get('board'), str):
        return choice
    if choice.get('backend') == 'gitlab' and isinstance(choice.get('board'), str):
        return choice
    return None


def describe(choice):
    if choice['backend'] == 'gitlab':
        return f'GitLab board {choice["board"]}'
    return f'{BACKENDS[choice["backend"]]} board {choice.get("name") or choice["board"]} on {choice["url"]}'


def read_state(path):
    """Another board's saved state, read only, or {} (that board itself reports a damaged file)."""
    try:
        with open(path, encoding='utf-8') as file:
            data = json.load(file)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def evidence(cwd, choices=None):
    """(the backends local clues point to, lines saying why, the checkout's remotes). Only local clues count: GitLab hosting
    the code says nothing about the tracker, as YouTrack and Jira projects often keep their code on GitLab. Nor does an
    issue key in the branch name alone: YouTrack and Jira both write them as WEB-12."""
    try:
        found = remotes.remotes(cwd)
    except remotes.NoRemote:
        found = []
    clues = {'youtrack': [], 'gitlab': [], 'jira': []}
    urls = {f'https://{r.host}/{r.path}'.casefold(): r for r in found}
    for server, section in read_state(default_path('youtrack-board')).items():
        repos = section.get('repos') if isinstance(section, dict) else None
        for repo in repos.values() if isinstance(repos, dict) else ():
            if isinstance(repo, str) and repo.casefold() in urls:
                clues['youtrack'].append(f'YouTrack Board shows merge requests of {repo} for a project on {server}.')
    gitlab_state = read_state(default_path('gitlab-board'))
    for r in found:
        section = gitlab_state.get(r.host) if isinstance(gitlab_state.get(r.host), dict) else {}
        keys = [*(section.get('defaults') or {}), *(section.get('visited') or {})]
        if any(isinstance(k, str) and (k == r.path or k.startswith(r.path + '/-/boards/')) for k in keys):
            clues['gitlab'].append(f'GitLab Board has opened boards of {r.host}/{r.path}.')
    branch = (remotes.git(cwd, 'branch', '--show-current') or '').strip()
    neutral = []
    if issue := ISSUE_ID.search(branch):
        jira = sorted({c['url'] for c in (choices or {}).values() if isinstance(c, dict) and c.get('backend') == 'jira'
                       and c.get('project') == issue.group(1)})
        if jira:
            clues['jira'].append(f'The branch {branch} names {issue.group()}, and other folders open Jira boards of '
                                 f'project {issue.group(1)} on {", ".join(jira)}.')
        else:
            neutral.append(f'The branch {branch} names {issue.group()}, an issue key as YouTrack and Jira both write '
                           'them, so that alone decides nothing.')
    hosted = [f'The code is on {r.host}/{r.path}; GitLab also hosts code of YouTrack and Jira projects, so that alone '
              'decides nothing.' for r in found]
    return [backend for backend, lines in clues.items() if lines], [*(l for lines in clues.values() for l in lines),
                                                                     *neutral, *hosted], found


def ask(prompt):
    try:
        return input(prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        raise Cancelled('Setup cancelled. Nothing was saved for this folder.') from None


class SetupPicker(App):
    CSS = """
    Screen { background: $surface; }
    ModalScreen { align: center middle; }
    #dialog { width: 80; max-width: 96%; height: auto; max-height: 96%; border: round $border;
              background: $surface; padding: 0 1; }
    #dialog Label { margin: 1 0; text-style: bold; }
    #dialog OptionList { height: auto; max-height: 50vh; margin-top: 1; padding: 0; border: none; }
    .hint { margin: 1 0; color: $text-muted; }
    """

    def __init__(self, title, options, default=None):
        super().__init__()
        self.title, self.options, self.default = title, options, default

    def on_mount(self):
        choices = [(n, Text(plain(label)), plain(label), False)
                   for n, (_, label) in enumerate(self.options)]
        self.push_screen(Picker(self.title, choices, self.default, fuzzy=True), self.exit)


def pick(title, options, default=None):
    index = SetupPicker(title, options, default).run()
    if index is None:
        raise Cancelled('Setup cancelled. Nothing was saved for this folder.')
    return options[index][0]


def pick_tracker(title, options, default=None):
    """The value of one of the (value, label) options, by number; Enter takes the default, if there is one."""
    print(title)
    for n, (_, label) in enumerate(options, 1):
        print(f'  {n}. {label}{"  (suggested)" if n - 1 == default else ""}')
    while True:
        answer = ask(f'Choose 1-{len(options)}{f", Enter for {default + 1}" if default is not None else ""}: ')
        if not answer and default is not None:
            return options[default][0]
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]


def choose_backend(folder, cwd, current):
    pointing, why, found = evidence(cwd, read_directories())
    print(f'Issue Board: which issue tracker does {folder} use?')
    for line in why or ['No clues found here.']:
        print(f'  · {line}')
    if len(pointing) > 1:
        print('  · The clues point both ways, so nothing is suggested.')
    default = current or (pointing[0] if len(pointing) == 1 else None)
    options = list(BACKENDS.items())
    return pick_tracker('Issue tracker:', options, [k for k, _ in options].index(default) if default else None), found


def youtrack_boards(account):
    async def load():
        client = YouTrack(account.url, account.token)
        try:
            return await client.boards()
        finally:
            await client.close()
    return asyncio.run(load())


def setup_youtrack():
    """Sign in as the YouTrack board does (saved, MCP or new credentials), then choose a project and a board."""
    account, connection = credentials.connect()
    boards = youtrack_boards(account)
    if not boards:
        raise credentials.SetupError(f'No accessible boards on {account.url}. Nothing was saved for this folder.')
    projects = {p['id']: p for b in boards for p in b.get('projects') or []}
    project = pick('YouTrack project:', [(None, 'All projects'), *(
        (pid, f'{p["shortName"]} · {p.get("name") or ""}') for pid, p in
        sorted(projects.items(), key=lambda item: item[1]['shortName'].casefold()))], 0)
    shown = sorted((b for b in boards if not project or project in youtrack_app.project_ids(b)),
                   key=lambda b: b['name'].casefold())
    board = pick('YouTrack board:', [(b['id'], b['name']) for b in shown], 0 if len(shown) == 1 else None)
    name = next(b['name'] for b in shown if b['id'] == board)
    return {'backend': 'youtrack', 'url': account.url, 'project': project, 'board': board, 'name': name}, (account, connection)


def jira_call(account, call):
    async def load():
        client = Jira(account.url, account.email, account.token)
        try:
            return await call(client)
        finally:
            await client.close()
    return asyncio.run(load())


def setup_jira():
    """Sign in as the Jira board does (saved, ALS keychain or new credentials), then choose a project and a board."""
    account, connection = jira_credentials.connect()
    print(f'Jira site: {account.url} (another site: run issueboard jira --setup first).')
    recent = jira_call(account, lambda client: client.recent_projects())
    other = '\x00other'
    project = pick('Jira project:', [(None, 'All projects (every board on the site; slower on big sites)'),
                                     *((p['key'], f'{p["key"]} · {p.get("name") or ""}') for p in recent if p.get('key')),
                                     (other, 'Another project key: type it')], 0)
    while project == other:
        project = ask('Jira project key, e.g. ABC: ').upper() or None
        if project and not jira_app.PROJECT_KEY.fullmatch(project):
            print(f'{project} is not a Jira project key.')
            project = other
    print(f'Loading the boards{f" of {project}" if project else ""}…')
    boards = sorted(jira_call(account, lambda client: client.boards(project)), key=lambda b: (b.get('name') or '').casefold())
    if not boards:
        raise jira_credentials.SetupError(f'No boards you can see{f" in {project}" if project else ""} on {account.url}. '
                                          'Nothing was saved for this folder.')
    labels = [(str(b['id']), f'{b.get("name")} · {b.get("type", "")} · {(b.get("location") or {}).get("projectKey") or ""}')
              for b in boards]
    board = pick('Jira board:', labels, 0 if len(boards) == 1 else None)
    name = next(b['name'] for b in boards if str(b['id']) == board)
    return {'backend': 'jira', 'url': account.url, 'project': project, 'board': board, 'name': name}, (account, connection)


def signed_in_glab(host):
    """glab's version line once it is signed in to the host, offering glab's own sign-in when it is not."""
    while True:
        try:
            return glab.check(host)
        except glab.UnknownHost as exc:
            print(exc)
            if ask(f'Run glab auth login --hostname {host} now? [Y/n] ').lower() not in ('', 'y', 'yes'):
                raise Cancelled('Setup cancelled. Nothing was saved for this folder.') from None
            # glab asks for and keeps the token itself; this app never sees it.
            subprocess.run(['glab', 'auth', 'login', '--hostname', host],
                           env={k: v for k, v in os.environ.items() if k not in glab.DROP}, check=False)


def gitlab_place(host, path, kind):
    async def load():
        client = GitLab(host)
        try:
            return await client.place(path, kind)
        finally:
            await client.close()
    return asyncio.run(load())


def setup_gitlab(found):
    """Confirm the GitLab project (from the remotes or a typed URL), make sure glab is signed in, choose a board."""
    other = (None, 'Another project, group or board: type its URL')
    url = pick('GitLab project:', [*((f'https://{r.host}/{r.path}', f'{r.host}/{r.path}  ({r.names})') for r in found),
                                   other], 0 if len(found) == 1 else None) if found else None
    while True:
        url = url or ask('GitLab project or board URL, e.g. https://gitlab.com/group/project: ')
        try:
            start = target.parse(url)
            target.filters(start.params)
            break
        except target.TargetError as exc:
            print(exc)
            url = None
    version = signed_in_glab(start.host)
    place = gitlab_place(start.host, start.path, start.kind)
    numbers = [b['number'] for b in place['boards']]
    if start.board in numbers:
        return {'backend': 'gitlab', 'board': url}, version  # as typed, with its filters
    if not numbers:
        raise Cancelled(f'{place["path"]} has no issue boards. Nothing was saved for this folder.')
    board = pick(f'Board of {place["path"]}:', [(b['number'], b['name']) for b in place['boards']],
                 0 if len(numbers) == 1 else None)
    key = target.Target(start.host, place['path'], place['kind'], board).key
    return {'backend': 'gitlab', 'board': f'https://{start.host}/{key}'}, version


def open_youtrack(choice, verbose, signed=None):
    account, connection = signed or credentials.connect(interactive=sys.stdin.isatty())
    if credentials.normalize(choice['url']) != credentials.normalize(account.url):
        # The token only works on its own server, and that server's boards are not this folder's.
        raise credentials.SetupError(
            f'This folder opens a board on {choice["url"]}, but the YouTrack token {account.source} is for '
            f'{account.url}. Nothing was opened. Run issueboard youtrack --setup to sign in to {choice["url"]} '
            '(or set YOUTRACK_URL and YOUTRACK_TOKEN for it), or issueboard --setup to pick a board on '
            f'{account.url} for this folder.')
    client = YouTrack(account.url, account.token)
    prefs = youtrack_app.Prefs(default_path('youtrack-board'), client.base_url)
    prefs.data['project'] = choice.get('project') if isinstance(choice.get('project'), str) else None
    prefs.visit(choice['board'])  # the most recently opened board in the scope is the one that opens
    youtrack_app.BoardApp(client, prefs, connection, cwd=os.curdir, verbose=verbose).run()


def open_jira(choice, verbose, signed=None):
    # Only credentials for the folder's own site: a token is never sent to another Jira.
    account, connection = signed or jira_credentials.connect(choice['url'], interactive=sys.stdin.isatty())
    client = Jira(account.url, account.email, account.token)
    prefs = jira_app.Prefs(default_path('jira-board'), client.base_url)
    prefs.data['project'] = choice.get('project') if isinstance(choice.get('project'), str) else None
    prefs.visit(choice['board'])  # the most recently opened board is the one that opens
    jira_app.JiraApp(client, prefs, connection, verbose=verbose).run()


def open_gitlab(choice, verbose, version=None):
    start = target.parse(choice['board'])
    target.filters(start.params)
    version = version or glab.check(start.host)
    prefs = gitlab_app.Prefs(default_path('gitlab-board'), start.host)
    gitlab_app.GitLabApp(GitLab(start.host), prefs, version, start, verbose=verbose).run()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in BACKENDS:
        backend = argv.pop(0)
        return {'youtrack': youtrack_app, 'gitlab': gitlab_app, 'jira': jira_app}[backend].main(argv)
    parser = argparse.ArgumentParser(
        prog='issueboard', description='A keyboard-driven YouTrack, GitLab or Jira issue board in the terminal',
        epilog='The first start in a project folder asks which tracker it uses, suggesting one from local clues, then '
               'sets it up and remembers the board for the folder (a Git checkout counts as one folder; each worktree '
               'as its own). Use issueboard youtrack, issueboard gitlab or issueboard jira for tracker-specific '
               'setup, authentication and board options; add --help to see them.')
    parser.add_argument('--setup', action='store_true', help='choose again which tracker and board this folder opens')
    parser.add_argument('--demo', choices=BACKENDS, help='try a board with in-memory sample data; reads and saves nothing')
    parser.add_argument('--verbose', action='store_true', help='show diagnostic logs; F12 toggles the panel')
    args = parser.parse_args(argv)
    verbose = ['--verbose'] if args.verbose else []
    if args.demo:
        {'youtrack': youtrack_app, 'gitlab': gitlab_app, 'jira': jira_app}[args.demo].main(['--demo', *verbose])
        return
    folder = directory()
    choice = saved_choice(read_directories(), folder)
    try:
        if args.setup or not choice:
            if not sys.stdin.isatty():
                sys.exit(f'issueboard: {"setup needs" if choice else f"{folder} has no board yet; choosing one needs"} '
                         'an interactive terminal. Run issueboard there, or use issueboard youtrack, issueboard gitlab or '
                         'issueboard jira directly (issueboard gitlab takes a board URL).')
            backend, found = choose_backend(folder, os.curdir, choice and choice['backend'])
            choice, signed = {'youtrack': setup_youtrack, 'jira': setup_jira}.get(backend, lambda: setup_gitlab(found))()
            try:
                write_json(config_path(), {'directories': {**read_directories(), folder: choice}})
                print(f'{folder} opens {describe(choice)} from now on; issueboard --setup changes it.')
            except OSError as exc:
                print(f'Could not save {config_path()} ({exc}); the next start asks again.', file=sys.stderr)
        else:
            signed = None
        {'youtrack': open_youtrack, 'jira': open_jira}.get(choice['backend'], open_gitlab)(choice, args.verbose, signed)
    except (Cancelled, credentials.SetupError, YouTrackError, GlabError, target.TargetError, jira_credentials.SetupError,
            JiraError, OSError) as exc:
        sys.exit(f'issueboard: {exc}')


if __name__ == '__main__':
    main()
