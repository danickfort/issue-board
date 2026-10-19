"""A keyboard-driven GitLab issue board in the terminal, on top of the YouTrack board's layout, keys and refresh.

GitLab decides which issues each list holds and what a move changes; this app shows the lists, sends the same
board mutations as GitLab's page, and explains their effects first. Requests go through glab (see glab.py).
"""
import argparse
import os
import sys
import webbrowser
from urllib.parse import urlsplit

from rich.console import Group
from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical, VerticalScroll
from textual.content import Content
from textual.widgets import Button, Footer, Input, Label, Static

from youtrack_board import prefs as base_prefs
from youtrack_board.app import (ALL, HINT, BacklogScreen, BoardApp, Card, Column, CommentDialog, CommentsScreen, Picker,
                                Row, ViewDialog, ago, plain)

from . import glab, remotes, target
from .client import FIRST, Client, describe_move, move_effects, project_path
from .demo import HOST as DEMO_HOST
from .demo import DemoClient, demo_state
from .glab import GlabError
from .markup import glfm
from .merge_requests import MergeRequestsScreen

ASK = '\x00ask'  # picker value for "enter a URL or path"; never a GitLab path


def clean_view(view):
    return {**base_prefs.clean_view(view), 'lanes': isinstance(view, dict) and view.get('lanes') is True}


class Prefs(base_prefs.Prefs):
    """The YouTrack app's local state file format, in gitlab-board/state.json, per GitLab host. Boards are keyed by
    their web path (group/project/-/boards/1), 'project' holds the project or group last used, and 'defaults' the
    board you chose to open for each project or group (group/project or groups/group -> board key)."""
    VIEW = staticmethod(clean_view)

    def clean(self, section):
        defaults = section.get('defaults') if isinstance(section, dict) else None
        return {**super().clean(section), 'defaults': {
            k: v for k, v in (defaults if isinstance(defaults, dict) else {}).items()
            if isinstance(v, str) and v.startswith(k + '/-/boards/') and v.rsplit('/', 1)[-1].isdigit()}}


def people(issue):
    return ', '.join(plain(a.get('name') or a['username']) for a in issue['assignees'])


def usernames(issue):
    return {a['username'] for a in issue['assignees']}


def search_text(issue):
    """What the local text filter matches: reference, title, assignees and labels."""
    names = ' '.join(f'{a.get("name", "")} {a["username"]}' for a in issue['assignees'])
    return f'{issue["reference"]} {issue["title"]} {names} {" ".join(issue["labels"])}'.casefold()


def body(text, empty, source=False):
    text = plain(text).strip()
    return Text(empty, 'dim') if not text else Text(text) if source else glfm(text)


def place_key(kind, path):
    return ('groups/' if kind == 'group' else '') + path


class GitLabCard(Card):
    def compose(self):
        issue, ref = self.issue, self.app.ref(self.issue)
        secret = ('  confidential', 'italic') if issue.get('confidential') else ''
        if self.has_class('-compact'):
            yield Static(Text.assemble((ref, 'bold'), secret, ' ', plain(issue['title'])))
        else:
            labels = ' '.join(f'~{plain(label)}' for label in issue['labels'])
            yield Static(Text.assemble((ref, 'bold'), secret, '\n', plain(issue['title']), '\n',
                                       (people(issue) or 'Unassigned', 'dim'), (f'  {labels}' if labels else '', 'dim')))


class GitLabRow(Row):
    def compose(self):
        meta = f'{people(self.issue) or "Unassigned"}' + ''.join(f' ~{plain(l)}' for l in self.issue['labels'][:3])
        yield Static(Text(self.app.ref(self.issue).ljust(self.id_width), 'bold'), classes='id')
        yield Static(Text(plain(self.issue['title'])), classes='summary')
        yield Static(Text(meta, 'dim'), classes='meta')


def note_text(note, source=False):
    author = note.get('author') or {}
    created, updated = note.get('created_at') or '', note.get('updated_at') or ''
    when = created[:16].replace('T', ' ') + ' UTC' if created else 'unknown time'
    header = Text.assemble(
        (plain(author.get('name') or author.get('username')) or 'Unknown user', 'bold'),
        (f'  {when}' + ('  · edited' if updated and updated != created and not note.get('resolved') else ''), 'dim'),
        ('  · internal note, for project members only' if note.get('internal') else '', 'italic'))
    return Group(header, body(note.get('body'), '(no text)', source))


class NotesScreen(CommentsScreen):
    """An issue's comments, newest first, without GitLab's system notes."""

    def compose(self):
        with Vertical(id='dialog', classes='comments'):
            yield Label(Text(f'Comments on {self.app.ref(self.issue)} · {plain(self.issue["title"])}'))
            yield VerticalScroll(id='thread')
            yield Static('', id='hint', classes='hint')

    def action_source(self):
        self.app.action_source()
        for widget, note in zip(self.query('#thread > .comment'), self.comments):
            widget.update(note_text(note, self.app.source))

    @work(exclusive=True)
    async def load(self):
        await self.show([Static('Loading comments…', classes='note')], 'Loading')
        try:
            notes = await self.client.notes(self.issue)
        except GlabError as exc:
            await self.show([Static(Text(plain(f'Could not load the comments. {exc}')), classes='note')], 'r retry',
                            error=True)
            return
        self.comments = notes
        if not notes:
            await self.show([Static('No comments yet, or none you can see.', classes='note')],
                            ('c write one  ·  ' if self.writable else '') + 'r reload')
            return
        count = f'{len(notes)} comment{"s" if len(notes) > 1 else ""}, newest first'
        await self.show([Static(note_text(n, self.app.source), classes='comment') for n in notes],
                        f'{count}  ·  ↑/↓ scroll  ·  {"c write  ·  " if self.writable else ""}s source  ·  r reload')


class ListScreen(BacklogScreen):
    """One board list in full, a page at a time in GitLab's order: the Open list is GitLab's backlog. Read-only."""
    MORE = 'More in GitLab'

    def __init__(self, client, place, board, lst):
        super().__init__(client, {'id': board['id'], 'name': board['name']})
        self.place, self.gitlab_board, self.list, self.cursor, self.count = place, board, lst, None, None

    def on_mount(self):
        super().on_mount()
        kind = 'GitLab’s backlog list' if self.list['listType'] == 'backlog' else f'{self.list["listType"]} list'
        self.query_one('#backlog-title', Static).update(Text.assemble(
            (plain(self.list['title']), 'bold'), f' · {plain(self.board["name"])}  ', (kind, 'dim')))
        self.query_one(Input).placeholder = 'Search loaded issues: #, title, assignee or label  (/)'

    def matches(self, issue):
        return self.query_one(Input).value.casefold() in search_text(issue)

    def row(self, issue, width):
        return GitLabRow(issue, None, width)

    def details(self, issue):
        return self.app.details(issue)

    async def fetch(self, after):
        return await self.client.list_page(self.place, self.gitlab_board, self.list['id'],
                                           self.gitlab_board['filters'], after, self.PAGE)

    @work(exclusive=True, group='backlog')
    async def load(self):
        keep = (self.current() or {}).get('id')
        self.issues, self.more, self.cursor = [], False, None
        await self.render_rows()
        self.note('Loading the list…', 'Loading the list…')
        try:
            page = await self.fetch(None)
        except GlabError as exc:
            self.failed(exc, 'Could not load the list', 'Press r to retry.')
            return
        self.issues, self.more, self.cursor, self.count = page['issues'], page['more'], page['cursor'], page['issuesCount']
        await self.render_rows(keep)

    @work(exclusive=True, group='backlog')
    async def load_more(self):
        self.status(f'Loading {self.PAGE} more…')
        try:
            page = await self.fetch(self.cursor)
        except GlabError as exc:
            self.failed(exc, 'Could not load more', 'Press n to retry.')
            return
        known = {i['id'] for i in self.issues}
        fresh = [i for i in page['issues'] if i['id'] not in known]  # the list can change between two pages
        self.issues, self.more, self.cursor, self.count = self.issues + fresh, page['more'], page['cursor'], page['issuesCount']
        await self.render_rows(next((i['id'] for i in fresh if self.matches(i)), (self.current() or {}).get('id')))

    def failed(self, exc, summary, retry):
        self.note(f'{summary}. {retry} {exc}', f'{summary}. {exc}\n\n{retry}', error=True)

    def show_counts(self, shown):
        loaded, query = len(self.issues), self.query_one(Input).value
        if not loaded and not self.more:
            if self.count is not None:
                self.note('The list is empty', 'No issue is in this list with the board’s filters.')
            return
        of = f' of {self.count}' if self.more and self.count else ''
        more = f'n {"searches" if query else "loads"} {self.PAGE} more' if self.more else ''
        if query:
            count = f'{shown} of {loaded}{" loaded" if self.more else ""} match' if shown else \
                f'No{" loaded" if self.more else ""} issue matches'
        else:
            count = f'{loaded}{of} issues, in board order'
        self.note(' · '.join(filter(None, [count, more])), 'No issue selected.')


class AskDialog(CommentDialog):
    def __init__(self, title, placeholder):
        super().__init__(title)
        self.placeholder = placeholder

    def on_mount(self):
        super().on_mount()
        self.query_one(Input).placeholder = self.placeholder


class GitLabApp(BoardApp):
    TITLE = 'GitLab Board'
    SERVICE = 'GitLab'
    ERROR = GlabError
    CARD = GitLabCard
    CSS = BoardApp.CSS + '''
    #scope { height: 1; padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #scope.warning { color: $text-warning; }
    '''
    BINDINGS = [('p', 'pick_project', 'Place'), ('B', 'backlog', 'List'),
                Binding('e', 'lanes', 'Epic swimlanes', show=False), Binding('D', 'make_default', 'Default board', show=False)]

    def __init__(self, client, prefs=None, via=None, start=None, remotes=(), note=None):
        """remotes: the GitLab projects of the Git checkout the app started in (several: it asks which). note: why the
        checkout gave none, shown once."""
        super().__init__(client, prefs or Prefs(None, client.host))
        self.start = start  # the Target to open: a board (with its URL filters), a project or a group
        self.via, self.place = via, None  # via: the glab version, named with the account once GitLab answers
        self.remotes, self.note = remotes, note

    def on_mount(self):
        super().on_mount()
        if self.note:
            self.notify(self.note, title='No GitLab project in this checkout', severity='warning', timeout=10)

    def compose(self):
        with Horizontal(id='toolbar'):
            yield Button('Place ▾', id='project', compact=True, action='app.pick_project', tooltip='Project or group (p)')
            yield Button('Board ▾', id='boards', compact=True, action='app.pick_board', tooltip='Board (b)')
            yield Input(placeholder='Filter by #, title, assignee or label  (/)', id='filter', compact=True)
            yield Button('', id='auto', compact=True, action='app.auto', tooltip='Auto-refresh this board (R)')
        yield Static('', id='scope', markup=False)  # the filters GitLab applies, from the board's scope and its URL
        yield Static('Loading boards…', id='status', markup=False)
        yield Horizontal(id='heads')
        yield ScrollableContainer(id='board', can_focus=False)
        with VerticalScroll(id='details-pane', can_maximize=True):
            yield Static('', id='details', markup=False)
        yield Footer(show_command_palette=False)

    def notify(self, message, *, markup=False, **options):
        """GitLab text (titles, labels, URL values) shows as typed: never as Textual markup or terminal controls."""
        super().notify(plain(message), markup=markup, **options)

    def status(self, text, error=False):
        super().status(plain(text), error)

    # Data on screen

    def board_lists(self):
        return self.issues_data.get('lists', []) if isinstance(self.issues_data, dict) else []

    def ref(self, issue):
        """#12 on a project board; project#12 on a group board, where issues come from several projects."""
        if self.place and self.place['kind'] == 'group':
            return f'{project_path(issue).rsplit("/", 1)[-1]}#{issue["iid"]}'
        return f'#{issue["iid"]}'

    def board_key(self, board_number):
        return f'{place_key(self.place["kind"], self.place["path"])}/-/boards/{board_number}'

    def loaded(self):
        return bool(self.board_data)

    async def read(self):
        return await self.client.lists(self.place, self.board_data, self.board_data['filters'], self.view()['lanes'])

    def visible_issues(self):
        query, mine, me = self.query_one('#filter', Input).value.casefold(), self.view()['mine'], self.me.get('username')
        seen, shown = set(), []
        for issue in (i for lst in self.board_lists() for i in lst['issues']):
            if issue['id'] not in seen and query in search_text(issue) and (not mine or me in usernames(issue)):
                seen.add(issue['id'])
                shown.append(issue)
        return shown

    def groups(self, issues):
        ids = {i['id'] for i in issues}
        return [(lst['title'], [i for i in lst['issues'] if i['id'] in ids]) for lst in self.board_lists()]

    def lanes(self, issues):
        """Epic swimlanes as on GitLab: the board's epics in its order, then issues with no epic. None when off."""
        epics = self.issues_data.get('epics') if isinstance(self.issues_data, dict) else None
        if not self.view()['lanes'] or epics is None:
            return None
        lanes, orphans = {e['id']: (e, []) for e in epics}, []
        for issue in issues:
            if issue.get('epic'):
                lanes.setdefault(issue['epic']['id'], (issue['epic'], []))[1].append(issue)
            else:
                orphans.append(issue)
        return list(lanes.values()) + ([(None, orphans)] if orphans else [])

    def lane_label(self, value, count):
        if value:
            return Text.assemble((f'&{value["iid"]}', 'bold'), f'  {plain(value["title"])}', (f'  {count}', 'dim'))
        return Text.assemble(('Issues with no epic assigned', 'bold'), (f'  {count}', 'dim'))

    def column_label(self, title, cards):
        lst = next((l for l in self.board_lists() if l['title'] == title), {})
        label = f'{title} · {len(cards)}'
        if lst.get('more'):
            label += f' of {lst["issuesCount"]}'
        if lst.get('maxIssueCount'):  # a WIP limit; GitLab marks the list when it holds more
            label += (' · ⚠ max ' if lst['issuesCount'] > lst['maxIssueCount'] else ' · max ') + str(lst['maxIssueCount'])
        return label

    def column_titles(self):
        return [lst['title'] for lst in self.board_lists()]

    def filters(self):
        return (['assigned to me'] if self.view()['mine'] else []) + \
            (['text filter'] if self.query_one('#filter', Input).value else [])

    def show_counts(self):
        lists, shown = self.board_lists(), len(self.visible_issues())
        total = len({i['id'] for lst in lists for i in lst['issues']})
        parts = [f'{shown} of {total} issues' if shown != total else f'{total} issues' if total else 'No issues on this board']
        parts += self.filters()
        if total and not shown:
            parts.append('x shows all')
        hidden = len(set(self.view()['hidden']) & set(self.column_titles()))
        if hidden:
            parts.append(f'{hidden} hidden list{"s" if hidden > 1 else ""}')
        cut = sum(1 for lst in lists if lst['more'])
        if cut:
            parts.append(f'{cut} list{"s show" if cut > 1 else " shows"} the first {FIRST}, B shows all')
        if self.view()['lanes'] and isinstance(self.issues_data, dict) and self.issues_data.get('lanes_error'):
            parts.append('no epic swimlanes: ' + self.issues_data['lanes_error'])
        self.status(' · '.join(parts) + HINT)

    def details(self, issue):
        lists = [lst['title'] for lst in self.board_lists() if any(i['id'] == issue['id'] for i in lst['issues'])]
        epic = issue.get('epic')
        fields = [('State', issue['state']), ('Lists', ', '.join(lists)), ('Status', issue.get('status')),
                  ('Assignees', people(issue) or 'none'), ('Labels', ' '.join(f'~{l}' for l in issue['labels'])),
                  ('Milestone', issue.get('milestone')), ('Iteration', issue.get('iteration')),
                  ('Epic', epic and f'&{epic["iid"]} {epic["title"]}'), ('Weight', issue.get('weight')),
                  ('Due', issue.get('dueDate'))]
        meta = '  ·  '.join(f'{name}: {value}' for name, value in fields if value not in (None, ''))
        return Group(Text.assemble((plain(issue['reference']), 'bold'), ('  confidential' if issue.get('confidential') else '', 'italic'),
                                   f'  {plain(issue["title"])}\n', plain(meta), '\n'),
                     body(issue.get('description'), 'No description.', self.source))

    def comments_screen(self, issue, writable=True):
        return NotesScreen(self.client, issue, writable)

    async def read_comments(self, issue):
        return await self.client.notes(issue)

    def comment_view(self, comment):
        return note_text(comment, self.source)

    def same(self, issue, other):
        return issue['id'] == other['id']

    def action_merge_requests(self, everything=False):
        """g: the open MRs GitLab links to the selected issue; G: every open MR of the board's project or group."""
        links = self.client.merge_request_issues
        if everything and self.place:
            place, group = self.place, self.place['kind'] == 'group'
            self.push_screen(MergeRequestsScreen(
                f'All open merge requests · {place["path"]}',
                f'In {place["path"]}{" and its subgroups" if group else ""}, most recently updated first · i opens an '
                'issue an MR closes or mentions', lambda: self.client.open_merge_requests(place), self.client.host,
                None if group else place['path'], links=links, empty=f'No open merge request in {place["path"]}.'),
                self.merge_requests_closed)
        elif self.selected and not everything:
            issue = self.selected
            self.push_screen(MergeRequestsScreen(
                f'Open merge requests · {self.ref(issue)} {plain(issue["title"])}',
                'Linked on GitLab, as on the issue page: closing or mentioning this issue',
                lambda: self.client.issue_merge_requests(issue), self.client.host, project_path(issue), links=links),
                self.merge_requests_closed)

    def list_of(self, issue=None):
        """The list the selected card sits in (it can sit in several), else the focused column's."""
        lists = self.board_lists()
        if issue and issue.get('listId'):
            return next((lst for lst in lists if lst['id'] == issue['listId']), None)
        if isinstance(self.focused, Column):
            cells = [c for c in self.focused.parent.children if isinstance(c, Column)]
            shown = [lst for lst in lists if lst['title'] not in self.view()['hidden']]
            index = cells.index(self.focused)
            return shown[index] if index < len(shown) else None
        return None

    # Places and boards

    def show_scope(self):
        board = next((b for b in self.boards if self.board_key(b['number']) == self.board_id), None) if self.place else None
        labels = (('#project', self.place['path'] if self.place else 'Place'), ('#boards', board['name'] if board else 'Board'))
        for button, label in labels:
            self.query_one(button, Button).label = Content(label + ' ▾')
            self.call_after_refresh(self.query_one(button).refresh, layout=True)
        scope = self.query_one('#scope', Static)
        filters, notes = (self.board_data or {}).get('filters') or {}, (self.board_data or {}).get('notes') or []
        scope.update(f'Board filters: {target.describe(filters)}' + (f'  ⚠ {notes[0]}' if notes else '')
                     + (f' (+{len(notes) - 1} more)' if len(notes) > 1 else ''))
        scope.set_class(bool(notes), 'warning')
        scope.display = bool(filters)

    def recent(self, keys):
        visited = self.prefs.data['visited']
        return sorted(keys, key=lambda k: -visited.get(k, 0))

    def from_checkout(self, place):
        return place['kind'] == 'project' and place['path'].casefold() in {r.path.casefold() for r in self.remotes}

    @work(exclusive=True, group='load')
    async def load_boards(self):
        start = self.start
        if not start:
            if self.remotes:
                self.choose_project()
            else:
                self.status('No board to open. Press p and enter a GitLab board, project or group URL.', error=True)
            return
        self.status(f'Loading the boards of {start.path}…')
        try:
            if not self.me:
                self.me = await self.client.me()
                if self.via:
                    self.notify(f'@{self.me.get("username")} on {self.client.host}, through {self.via}',
                                title='GitLab account', timeout=8)
            place = await self.client.place(start.path, start.kind)
        except GlabError as exc:
            self.status(f'{exc} — press r to retry, or p for another place.', error=True)
            return
        self.place, self.boards = place, place['boards']
        self.show_scope()
        if not self.boards:
            self.status(f'{place["path"]} has no issue boards. Create one in GitLab, or press p for another place.', error=True)
            return
        numbers = [b['number'] for b in self.boards]
        keys = [self.board_key(n) for n in numbers]
        default = self.prefs.data['defaults'].get(place_key(place['kind'], place['path']))
        if start.board in numbers:
            self.load_board(self.board_key(start.board))
            return
        if start.board:
            self.notify(f'Board {start.board} is not in {place["path"]}.', severity='warning')
        if default in keys:
            self.load_board(default)
        elif default or self.from_checkout(place):
            await self.choose_default(keys, gone=bool(default))
        else:
            self.load_board(self.recent(keys)[0])

    async def choose_default(self, keys, gone):
        """Ask which board opens for this place from now on: nothing opens or is saved until one is picked. A single
        board needs no question. GitLab keeps no board activity time, so none is guessed; your last visit on GitLab is
        only preselected."""
        path = self.place['path']
        why = f'Your default board of {path} is gone, or hidden from you now. ' if gone else ''
        if len(keys) == 1:
            self.set_default(keys[0], f'{why}{self.boards[0]["name"]}, the only board of {path}, is your default now.')
            self.load_board(keys[0])
            return
        try:
            recent = [self.board_key(n) for n in await self.client.recent_boards(self.place)]
        except GlabError:
            recent = []  # it only preselects
        visited, choices = self.prefs.data['visited'], []
        for board, key in zip(self.boards, keys):
            notes = (['your last visit on GitLab'] if recent[:1] == [key] else []) + \
                ([f'opened here {ago(visited[key])}'] if key in visited else [])
            choices.append((key, Text.assemble(plain(board['name']), (''.join(f'  · {n}' for n in notes), 'dim')),
                            board['name'].casefold(), False))
        pick = next((k for k in recent if k in keys), None) or next((k for k in self.recent(keys) if k in visited), None)
        self.status(f'{why}Choose the board to open for {path} from now on; Esc opens none and saves nothing.')
        self.push_screen(Picker(f'{path}: choose a new default board' if gone else
                                f'Found project {path} — choose your default board', choices, pick), self.default_picked)

    def default_picked(self, key):
        if key:
            name = next(b['name'] for b in self.boards if self.board_key(b['number']) == key)
            self.set_default(key, f'{name} is now the default board of {self.place["path"]}. D on another board changes it.')
            self.load_board(key)
        else:
            self.status(f'No default board for {self.place["path"]}; nothing was saved. Press b to open a board (D makes '
                        'it the default), or p for another place.', error=True)

    def set_default(self, key, message):
        self.prefs.data['defaults'][place_key(self.place['kind'], self.place['path'])] = key
        self.save_prefs()
        self.notify(message)

    def action_make_default(self):
        if self.board_data:
            path, name = self.place['path'], self.board_data['name']
            if self.prefs.data['defaults'].get(place_key(self.place['kind'], path)) == self.board_id:
                self.notify(f'{name} already is the default board of {path}.')
            else:
                self.set_default(self.board_id, f'{name} is now the default board of {path}.')

    def choose_project(self):
        """The checkout's remotes name several GitLab projects (a fork and its upstream, say): ask, never guess."""
        visited = self.prefs.data['visited']

        def used(remote):
            return max((v for k, v in visited.items() if k.startswith(remote.path + '/-/boards/')), default=0)
        choices = [(r.path, Text.assemble(r.path, (f'  {r.names}', 'dim')), f'{r.path} {r.names}'.casefold(), False)
                   for r in self.remotes]
        self.status('This checkout’s remotes name several GitLab projects.')
        self.push_screen(Picker(f'Found {len(self.remotes)} GitLab projects in this checkout’s remotes — choose one',
                                choices, max(self.remotes, key=used).path), self.project_chosen)

    def project_chosen(self, path):
        if path:
            self.go(path)
        else:
            self.status('No project chosen. Press p for a project or group.', error=True)

    @work(exclusive=True, group='load')
    async def load_board(self, key):
        self.board_id, self.board_data, self.issues_data = key, None, []
        self.show_scope()
        self.show_auto()
        await self.render_board()
        self.status('Loading board…')
        url = self.start if self.start and self.start.board and self.board_key(self.start.board) == key else None
        try:
            board = await self.client.board(self.place, int(key.rsplit('/', 1)[-1]))
            params, notes = target.scoped(url.params if url else {}, board)
            board['filters'] = target.filters(params)
        except (GlabError, target.TargetError) as exc:
            self.status(f'{exc} — press r to retry.', error=True)
            return
        repeated = (url.repeated if url else {}).items()
        board['notes'] = [f'The URL gives {k} {len(v)} times ({", ".join(v)}); GitLab’s board uses only the last, “{v[-1]}”.'
                          for k, v in repeated] + notes
        if url and url.params.get('group_by') == 'epic':
            self.prefs.set_view(key, lanes=True)
        self.board_data = board
        self.prefs.visit(key)
        self.prefs.data['project'] = place_key(self.place['kind'], self.place['path'])
        self.save_prefs()
        self.show_scope()
        self.show_auto()
        for note in board['notes']:
            self.notify(note, title='Board URL', severity='warning', timeout=15)
        await self.fetch_issues()

    def action_pick_board(self):
        if self.busy or not self.boards:
            return
        visited, choices = self.prefs.data['visited'], []
        default = self.prefs.data['defaults'].get(place_key(self.place['kind'], self.place['path']))
        for board in sorted(self.boards, key=lambda b: (-visited.get(self.board_key(b['number']), 0), b['name'].casefold())):
            key = self.board_key(board['number'])
            note = '  (current)' if key == self.board_id else f'  · opened {ago(visited[key])}' if key in visited else ''
            note += '  · default' if key == default else ''
            choices.append((key, Text.assemble(plain(board['name']), (note, 'dim')), board['name'].casefold(),
                            key == self.board_id))
        choices.append((ALL, Text('Other projects and groups…  (p)', 'italic'), None, False))
        self.push_screen(Picker(f'Boards in {self.place["path"]} · recently opened first', choices), self.board_picked)

    def board_picked(self, value):
        if value == ALL:
            self.action_pick_project()
        elif value:
            self.start = None  # URL filters belong to the board they came with, as on GitLab
            self.open_board(value)

    def action_pick_project(self):
        if self.busy:
            return
        current = place_key(self.place['kind'], self.place['path']) if self.place else None
        places = {}
        for key, when in self.prefs.data['visited'].items():
            places[key.split('/-/boards/')[0]] = max(places.get(key.split('/-/boards/')[0], 0), when)
        if self.place and self.place.get('parent'):
            places.setdefault(place_key('group', self.place['parent']), 0)
        choices = []
        for key in sorted(places, key=lambda k: (-places[k], k)):
            kind = 'group' if key.startswith('groups/') else 'project'
            note = '  (current)' if key == current else f'  · opened {ago(places[key])}' if places[key] else '  · parent group'
            choices.append((key, Text.assemble(key.removeprefix('groups/'), (f'  {kind}{note}', 'dim')), key.casefold(),
                            key == current))
        choices.append((ASK, Text('Enter a board, project or group URL or path…', 'italic'), None, False))
        self.push_screen(Picker(f'Projects and groups on {self.client.host}', choices, current), self.place_picked)

    def place_picked(self, value):
        if value == ASK:
            self.push_screen(AskDialog('Open a GitLab board, project or group',
                                       f'https://{self.client.host}/group/project/-/boards/1 or group/project'), self.go)
        elif value:
            self.go(value)

    def go(self, text):
        """Open what a URL or path names on this host: a board with its filters, or a project's or group's boards."""
        if not text:
            return
        try:
            found = target.parse(text, self.client.host)
            target.filters(found.params)
        except target.TargetError as exc:
            self.status(str(exc), error=True)
            return
        if found.host != self.client.host:
            self.status(f'That is on {found.host}; this session uses {self.client.host}. Start the app with that URL.',
                        error=True)
            return
        self.start, self.board_id, self.board_data, self.issues_data = found, None, None, []
        self.load_boards()

    def action_backlog(self):
        lists = self.board_lists()
        if not self.board_data or not lists:
            return
        lst = self.list_of(self.selected) or self.list_of() or next((l for l in lists if l['listType'] == 'backlog'), lists[0])
        self.push_screen(ListScreen(self.client, self.place, self.board_data, lst), self.backlog_closed)

    # Views

    def action_view(self):
        if not self.board_data:
            return
        view, titles = self.view(), self.column_titles()
        selections = [(Text('Only issues assigned to me  (M)'), 'mine', view['mine']),
                      (Text('Compact one-line cards'), 'compact', view['compact']),
                      (Text('Epic swimlanes  (e) · GitLab Premium'), 'lanes', view['lanes'])]
        selections += [(Text(f'Show list: {title}'), ('column', title), title not in view['hidden']) for title in titles]

        async def apply(selected):
            lanes = 'lanes' in selected
            reread = lanes != self.view()['lanes']
            hidden = [t for t in self.view()['hidden'] if t not in titles] + [t for t in titles if ('column', t) not in selected]
            self.prefs.set_view(self.board_id, mine='mine' in selected, compact='compact' in selected, hidden=hidden, lanes=lanes)
            self.save_prefs()
            if reread:
                self.load_issues()
            else:
                await self.render_board()

        self.push_screen(ViewDialog(f'My view of {self.board_data["name"]}', selections, apply),
                         lambda _: self.action_focus_board())

    def action_lanes(self):
        if self.board_data:
            lanes = not self.view()['lanes']
            self.prefs.set_view(self.board_id, lanes=lanes)
            self.save_prefs()
            self.notify('Epic swimlanes on.' if lanes else 'Epic swimlanes off.')
            self.load_issues()

    async def action_clear_filters(self):
        if self.board_data and self.filters():
            self.prefs.set_view(self.board_id, mine=False)
            self.save_prefs()
            self.query_one('#filter', Input).value = ''
            await self.render_board()
            self.action_focus_board()
            self.notify('Showing every issue the board’s filters allow.')

    # Changes

    def refused(self, issue, what):
        self.status(f'{self.ref(issue)}: your role in {project_path(issue)} cannot {what} '
                    '(GitLab needs at least the Planner role).', error=True)

    def action_move(self):
        if not self.selected or self.busy or not self.board_data:
            return
        issue, lists = self.selected, self.board_lists()
        source = self.list_of(issue)
        if not issue.get('canAdmin'):
            self.refused(issue, 'move issues')
            return
        if not source or len(lists) < 2:
            return
        choices = []
        for lst in lists:
            here = lst['id'] == source['id']
            note = '  (current)' if here else '  ' + plain(describe_move(move_effects(lists, issue, source, lst)))
            choices.append((lst['id'], Text.assemble(plain(lst['title']), (note, 'dim')), lst['title'].casefold(), here))
        after = lists[(lists.index(source) + 1) % len(lists)]['id']

        def picked(list_id):
            to = next((lst for lst in lists if lst['id'] == list_id), None)
            if to:
                self.write(issue, lambda: self.client.move(self.board_data, issue, source, to),
                           f'Moved {self.ref(issue)} to {to["title"]}.')
        self.push_screen(Picker(f'Move {self.ref(issue)} from {plain(source["title"])} · GitLab applies the effects shown',
                                choices, after), picked)

    def action_assign(self):
        if not self.selected or self.busy:
            return
        issue = self.selected
        if not issue.get('canAdmin'):
            self.refused(issue, 'assign issues')
            return
        many = len(issue['assignees']) > 1
        picker = Picker(f'Assign {self.ref(issue)} · {plain(issue["title"])}' + (f' · replaces all {len(issue["assignees"])}' if many else ''))
        self.push_screen(picker, lambda username: self.assigned(issue, username))
        self.load_people(picker, issue)

    @work(group='people')
    async def load_people(self, picker, issue):
        path = project_path(issue)
        try:
            if path not in self.people:
                self.people[path] = await self.client.members(issue)
        except GlabError as exc:
            picker.fail(plain(f'Could not load the members of {path}. {exc}'))
            return
        users, more = self.people[path]
        me, current = self.me.get('username'), usernames(issue)
        active = {u for lst in self.board_lists() for i in lst['issues'] for u in usernames(i)}
        eligible = sorted(users, key=lambda u: (u['username'] != me, u['username'] not in active,
                                                (u.get('name') or u['username']).casefold()))
        choices = [('', Text.assemble(plain(a.get('name') or a['username']), (f'  @{a["username"]}  (current, not a member)', 'dim')),
                    a['username'].casefold(), True) for a in issue['assignees'] if a['username'] not in {u['username'] for u in users}]
        for user in eligible:
            name, username = plain(user.get('name') or user['username']), user['username']
            note = ('  (current)' if current == {username} else '  · assigned' if username in current
                    else '  · you' if username == me else '  · on this board' if username in active else '')
            choices.append((username, Text.assemble(name, (f'  @{username}{note}', 'dim')), f'{name} {username}'.casefold(),
                            current == {username}))
        choices.append(('', Text.assemble('Unassigned', ('' if current else '  (current)', 'dim')), 'unassigned', not current))
        picker.load(choices, f'Search {len(eligible)} members of {path}' + (' (the first 500)' if more else ''))

    def assigned(self, issue, username):
        if username is not None:
            self.write(issue, lambda: self.client.assign(issue, [username] if username else []),
                       f'Assigned {self.ref(issue)} to @{username}.' if username else f'Unassigned {self.ref(issue)}.')

    def action_comment(self):
        if self.selected and not self.busy:
            issue = self.selected
            if not issue.get('canComment'):
                self.status(f'{self.ref(issue)}: you cannot comment here (locked discussion or no access).', error=True)
                return
            self.push_screen(CommentDialog(f'Comment on {self.ref(issue)}'), lambda value: value and self.write(
                issue, lambda: self.save_comment(issue, value), f'Comment added to {self.ref(issue)}.', reload=False))

    def open_issue(self, issue, status):
        url = issue.get('webUrl') or ''
        parts = urlsplit(url)
        if parts.scheme != 'https' or (parts.netloc or '').lower() != self.client.host:
            status(f'Not opening {plain(url)}: it is not an https link on {self.client.host}.', error=True)
        elif webbrowser.open(url):
            self.notify(url, title=f'Opening {self.ref(issue)}')
        else:
            status(f'No browser available. Open {url}', error=True)


def main():
    parser = argparse.ArgumentParser(
        prog='gitlab-board', description='A keyboard-driven GitLab issue board in the terminal',
        epilog='Requests run through the GitLab CLI (glab) with its own sign-in: glab auth login --hostname <host>. '
               'Without a URL, inside a Git checkout whose remote is a GitLab project, that project opens on your '
               'default board, chosen the first time. Elsewhere, the board you opened last on --hostname opens again.')
    parser.add_argument('url', nargs='?', help='a board URL (filters included), or a project or group URL or path')
    parser.add_argument('--hostname', help='GitLab host for a bare path or resuming (default: gitlab.com); in a '
                                           'checkout, only its remotes on this host count')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--demo', action='store_true', help='try with in-memory sample data; runs no glab, saves nothing')
    modes.add_argument('--auth-status', action='store_true', help='check glab and its sign-in for the host, then exit')
    args = parser.parse_args()
    try:
        start = target.parse(args.url, DEMO_HOST if args.demo else args.hostname or 'gitlab.com') if args.url else None
        if start:
            target.filters(start.params)  # refuse filters the board would not apply before anything starts
    except target.TargetError as exc:
        sys.exit(f'gitlab-board: {exc}')
    if args.demo:
        client = DemoClient()
        state = demo_state()
        GitLabApp(client, Prefs(None, client.host, state), start=start or target.parse(state['project'], client.host)).run()
        return
    host, found, versions, note = start.host if start else args.hostname or 'gitlab.com', [], {}, None
    try:
        if not start:  # a URL wins over the checkout
            try:
                found, versions = remotes.find(os.curdir, args.hostname)
            except remotes.NoRemote as exc:
                note = str(exc) if exc.checkout else None
        if len(versions) > 1:
            sys.exit('gitlab-board: this checkout has GitLab remotes on several hosts: ' + '; '.join(map(str, found))
                     + '. Choose one with --hostname, or give a board or project URL.')
        host = next(iter(versions), host)
        version = versions.get(host) or glab.check(host)
    except GlabError as exc:
        sys.exit(f'gitlab-board: {exc}')
    if args.auth_status:
        print(f'{version}: signed in to {host} (glab auth status --hostname {host} shows the details).'
              + ''.join(f'\nThis checkout: {r}' for r in found))
        return
    prefs = Prefs(base_prefs.default_path('gitlab-board'), host)
    if len(found) == 1:
        start = target.Target(host, found[0].path, 'project')
    elif not found and not start and prefs.data['project']:
        start = target.parse(prefs.data['project'], host)
    if not start and not found:
        sys.exit(f'gitlab-board: {note + " " if note else ""}Give a board URL, for example '
                 f'https://{host}/group/project/-/boards/1 (or try --demo).')
    GitLabApp(Client(host), prefs, version, start, found, note).run()


if __name__ == '__main__':
    main()
