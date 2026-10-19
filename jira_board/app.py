import argparse
import asyncio
import re
import sys
import webbrowser
from urllib.parse import quote

from rich.console import Group
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical, VerticalScroll
from textual.content import Content
from textual.widgets import Button, Footer, Input, Label, Select, Static

from issueboard.board import BoardView
from issueboard.diagnostics import LogPanel
from issueboard.prefs import Prefs, default_path
from issueboard.widgets import (ALL, HINT, BacklogScreen as BaseBacklogScreen, Card as BaseCard, CommentDialog,
                                CommentsScreen as BaseCommentsScreen, Picker, Row as BaseRow, ViewDialog, ago, plain)
from . import credentials
from .client import Client, JiraError, columns, group_issues, required_fields
from .demo import DemoClient, demo_state
from .markup import render

OTHER = '\x00other'
NOT_LOADED = object()  # a description not read yet: it is read with the comments, when the details expand
SPRINT_ORDER = {'active': 0, 'future': 1, 'closed': 2}
PROJECT_KEY = re.compile(r'[A-Z][A-Z0-9_]{0,254}')


def person(issue):
    return (issue.get('assignee') or {}).get('displayName') or ''


def search_text(issue):
    """What the local text filters match: key, summary, assignee, status and type."""
    return f'{issue["key"]} {issue["summary"]} {person(issue)} {issue["status"]["name"]} {issue["type"]}'.casefold()


def sprint_label(sprint):
    return plain(sprint.get('name') or str(sprint['id'])) + ('' if sprint.get('state') == 'active' else f' ({sprint.get("state")})')


def issue_details(issue, description, source):
    """The details pane: key, summary, the card fields, and the description once it is read."""
    fields = '  ·  '.join(f'{name}: {plain(value) or "—"}' for name, value in (
        ('Type', issue['type']), ('Status', issue['status']['name']), ('Priority', issue['priority']),
        ('Assignee', person(issue))))
    body = Text('Enter expands the details and reads the description and comments.', 'dim') \
        if description is NOT_LOADED else render(description, 'No description.', source)
    return Group(Text.assemble((issue['key'], 'bold'), f'  {plain(issue["summary"])}\n', fields, '\n'), body)


def comment_text(comment, source=False):
    author, created, updated = comment.get('author') or {}, comment.get('created') or '', comment.get('updated') or ''
    visibility = comment.get('visibility') or {}
    header = Text.assemble(
        (plain(author.get('displayName')) or 'Unknown user', 'bold'),
        (f'  {created[:16].replace("T", " ") or "unknown time"}' + ('  · edited' if updated[:19] != created[:19] else ''), 'dim'),
        (f'  · visible to {plain(visibility.get("type") or "")} {plain(visibility.get("value") or "")}'.rstrip()
         if visibility else '', 'italic'),
        ('  · internal' if comment.get('jsdPublic') is False else '', 'italic'))
    return Group(header, render(comment.get('body'), '(no text)', source))


class CommentsScreen(BaseCommentsScreen):
    def compose(self) -> ComposeResult:
        with Vertical(id='dialog', classes='comments'):
            yield Label(Text(f'Comments on {self.issue["key"]} · {plain(self.issue["summary"])}'))
            yield VerticalScroll(id='thread')
            yield Static('', id='hint', classes='hint')


class Card(BaseCard):
    def compose(self):
        issue = self.issue
        if self.has_class('-compact'):
            yield Static(Text.assemble((issue['key'], 'bold'), ' ', plain(issue['summary'])))
            return
        meta = ' · '.join(filter(None, [plain(person(issue)) or 'Unassigned', plain(issue['status']['name']),
                                        plain(issue['priority'])]))
        yield Static(Text.assemble((issue['key'], 'bold'), (f'  {plain(issue["type"])}', 'dim'), '\n',
                                   plain(issue['summary']), '\n', (meta, 'dim')))


class Row(BaseRow):
    """A backlog issue on one line: key, summary, then status and assignee."""

    def compose(self):
        meta = f'{plain(self.issue["status"]["name"]) or "—"} · {plain(person(self.issue)) or "Unassigned"}'
        yield Static(Text(self.issue['key'].ljust(self.id_width), 'bold'), classes='id')
        yield Static(Text(plain(self.issue['summary'])), classes='summary')
        yield Static(Text(meta, 'dim'), classes='meta')


class BacklogScreen(BaseBacklogScreen):
    MORE = 'More in Jira'
    SEARCH = 'Search loaded issues: key, summary, status, assignee or type  (/)'

    def __init__(self, client, board):
        super().__init__(client, board)
        self.token, self.available = None, True
        self.explanation = 'Loading the backlog…'

    def matches(self, issue):
        return self.query_one(Input).value.casefold() in search_text(issue)

    @work(exclusive=True, group='backlog')
    async def load(self):
        """The first page of the board's backlog, as Jira ranks it now."""
        keep = (self.current() or {}).get('id')
        self.issues, self.more, self.available = [], False, False  # nothing to count until Jira answers
        await self.render_rows()
        self.note('Loading the backlog…', 'Loading the backlog…')
        try:
            self.issues, self.token = await self.client.backlog(self.board['id'], None, self.PAGE)
        except JiraError as exc:
            if exc.status == 400:  # Jira's answer for a board without a backlog
                self.note('No backlog', f'Jira has no backlog for this board: scrum boards have one, kanban boards only '
                          f'with the kanban backlog turned on. {exc}')
                return
            self.failed(exc, 'Could not load the backlog', 'Press r to retry.')
            return
        self.more, self.available = bool(self.token), True
        await self.render_rows(keep)

    @work(exclusive=True, group='backlog')
    async def load_more(self):
        self.status(f'Loading {self.PAGE} more…')
        try:
            page, token = await self.client.backlog(self.board['id'], self.token, self.PAGE)
        except JiraError as exc:
            self.failed(exc, 'Could not load more', 'Press n to retry.')
            return
        known = {i['id'] for i in self.issues}
        page = [i for i in page if i['id'] not in known]  # the backlog can change between two pages
        self.issues, self.token, self.more = self.issues + page, token, bool(token)
        await self.render_rows(next((i['id'] for i in page if self.matches(i)), (self.current() or {}).get('id')))

    def failed(self, exc, summary, retry):
        self.note(f'{summary}. {retry} {exc}', f'{summary}. {exc}\n\n{retry}', error=True)

    def show_counts(self, shown):
        loaded, query = len(self.issues), self.query_one(Input).value
        if not self.available:
            return
        if not (loaded or self.more):
            self.note('The backlog is empty', 'No issue is in the backlog of this board now.')
            return
        more = f'n {"searches" if query else "loads"} {self.PAGE} more' if self.more else ''
        if query:
            count = f'{shown} of {loaded}{" loaded" if self.more else ""} match' if shown else \
                f'No{" loaded" if self.more else ""} issue matches'
            self.note(' · '.join(filter(None, [count, more])), 'No issue selected.')
        else:
            self.note(' · '.join(filter(None, [f'{loaded} issues{" loaded" if self.more else ""}, in rank order', more])),
                      'No issue selected.')

    def row(self, issue, width):
        return Row(issue, None, width)

    def details(self, issue):
        return issue_details(issue, self.app.descriptions.get(issue['key'], NOT_LOADED), self.app.source)


class JiraApp(BoardView):
    TITLE = 'Jira Board'
    SERVICE = 'Jira'
    ERROR = JiraError
    CARD = Card
    REFRESH = 10  # Jira Cloud rate-limits by cost; a board's issues every 10 s stays well inside it
    CSS = BoardView.CSS + '#toolbar #auto { width: 8; }'  # fits "↻ 120s", the slowest pace after failures
    DETAILS = 'Details  (Enter expands with description and comments · s source)'

    def __init__(self, client, prefs=None, connection=None, verbose=False):
        super().__init__(client, prefs or Prefs(None, client.base_url), connection, verbose)
        if self.log_buffer is not None:
            self.log_buffer.secrets += tuple(getattr(client, 'secrets', ()))
        self.sprint_data, self.sprint_memory = None, {}
        self.boards_scope = None  # the project the loaded board list is of
        self.descriptions, self.drafts = {}, {}

    def check_action(self, action, parameters):
        if action == 'merge_requests':  # GitLab merge requests are linked to YouTrack and GitLab issues only
            return False
        return super().check_action(action, parameters)

    def refresh_context(self):
        return self.board_data, self.sprint_data

    def compose(self):
        with Horizontal(id='toolbar'):
            yield Button('All projects ▾', id='project', compact=True, action='app.pick_project', tooltip='Project (p)')
            yield Button('Board ▾', id='boards', compact=True, action='app.pick_board', tooltip='Board (b)')
            yield Select([], prompt='Sprint', id='sprints', compact=True)
            yield Input(placeholder='Filter by key, summary, assignee or status  (/)', id='filter', compact=True)
            yield Button('', id='auto', compact=True, action='app.auto', tooltip='Auto-refresh this board (R)')
        yield Static('Loading boards…', id='status', markup=False)
        yield Horizontal(id='heads')
        yield ScrollableContainer(id='board', can_focus=False)
        with VerticalScroll(id='details-pane', can_maximize=True):
            yield Static('', id='details', markup=False)
        if self.log_buffer is not None:
            yield LogPanel()
        yield Footer(show_command_palette=False)

    def notify(self, message, *, markup=False, **options):
        """Jira text (summaries, names) shows as typed: never as Textual markup or terminal controls."""
        super().notify(plain(message), markup=markup, **options)

    def status(self, text, error=False):
        super().status(plain(text), error)

    # Boards and sprints

    def board_name(self):
        return (self.board_data or {}).get('name') or ''

    def show_scope(self):
        labels = (('#project', self.prefs.data['project'] or 'All projects'), ('#boards', self.board_name() or 'Board'))
        for button, label in labels:
            self.query_one(button, Button).label = Content(label + ' ▾')
            # A new label doesn't resize an auto-width Button, and a refresh during a modal callback is dropped.
            self.call_after_refresh(self.query_one(button).refresh, layout=True)

    @work(exclusive=True, group='load')
    async def load_boards(self):
        """Who is signed in, then the board opened here last; the first time, the first board of the project scope."""
        self.status('Loading boards…')
        visited = self.prefs.data['visited']
        try:
            self.me = await self.client.me()
            if not visited:
                await self.list_boards()
        except JiraError as exc:
            self.status(f'{exc} — press r to retry.', error=True)
            return
        if visited:
            self.load_board(max(visited, key=visited.get))
        elif self.boards:
            self.load_board(str(sorted(self.boards, key=lambda b: (b.get('name') or '').casefold())[0]['id']))
        else:
            scope = self.prefs.data['project']
            self.status(f'No boards you can see{f" in {scope}" if scope else ""}. Press p to choose another project.',
                        error=True)

    async def list_boards(self):
        """The boards of the project scope, read once per scope: a big site has thousands."""
        scope = self.prefs.data['project']
        if self.boards_scope != scope or not self.boards:
            self.boards, self.boards_scope = await self.client.boards(scope), scope
        return self.boards

    @work(exclusive=True, group='load')
    async def load_board(self, board_id):
        self.board_id = board_id
        self.board_data = self.sprint_data = None
        self.issues_data = []
        self.show_scope()
        self.show_auto()
        await self.render_board()
        self.status('Loading board…')
        selector = self.query_one('#sprints', Select)
        selector.set_options([])
        try:
            board = await self.client.board(board_id)
        except JiraError as exc:
            self.status(f'{exc} — press r to retry, or b for another board.', error=True)
            return
        self.board_data = board
        self.prefs.visit(board_id)
        self.save_prefs()
        self.show_scope()
        sprints = board['sprints']
        selector.display = sprints is not None
        if sprints is None:  # kanban: every issue on the board
            await self.fetch_issues()
            return
        # Active first, then future, then closed ones newest first.
        ordered = sorted(enumerate(sprints), key=lambda p: (SPRINT_ORDER.get(p[1].get('state'), 3),
                                                            -p[0] if p[1].get('state') == 'closed' else p[0]))
        board['sprints'] = [s for _, s in ordered]
        selector.prompt = 'Sprint' if any(s.get('state') == 'active' for s in sprints) else 'No active sprint'
        selector.set_options([(sprint_label(s), str(s['id'])) for s in board['sprints']])
        remembered = self.sprint_memory.get(board_id)
        choice = next((s for s in board['sprints'] if str(s['id']) == remembered), None)
        choice = choice or next((s for s in board['sprints'] if s.get('state') == 'active'), None)
        if choice:
            selector.value = str(choice['id'])
        else:  # never show another sprint, or every issue, as if it were the current one
            await self.render_board()

    @on(Select.Changed, '#sprints')
    async def sprint_changed(self, event):
        if not self.board_data or self.board_data['sprints'] is None:  # a late event from clearing the selector
            return
        self.sprint_data = next((s for s in self.board_data['sprints'] if str(s['id']) == event.value), None)
        self.issues_data = []
        await self.render_board()
        if self.sprint_data:
            self.sprint_memory[self.board_id] = str(self.sprint_data['id'])
            self.load_issues()

    def scrum(self):
        return bool(self.board_data) and self.board_data['sprints'] is not None

    def loaded(self):
        """A board and, if it has sprints, a sprint are chosen, so its issues can be (re)loaded."""
        return bool(self.board_data) and (bool(self.sprint_data) or not self.scrum())

    async def read(self):
        return await self.client.issues(self.board_data, self.sprint_data)

    def no_sprint(self):
        """Why a sprint board shows nothing yet, or None."""
        if not self.scrum() or self.sprint_data:
            return None
        sprints = self.board_data['sprints']
        if not sprints:
            return f'{plain(self.board_name())} has no sprints yet. Press B for its backlog.'
        return (f'No active sprint on {plain(self.board_name())}. Choose a future or closed sprint in the Sprint list, '
                'or press B for the backlog.')

    async def render_board(self):
        await super().render_board()
        if (reason := self.no_sprint()) and not self.query('#board > .empty'):
            await self.query_one('#board').mount(Static(reason, classes='empty'))

    # Cards

    def visible_issues(self):
        query, mine, me = self.query_one('#filter', Input).value.casefold(), self.view()['mine'], self.me.get('accountId')
        return [i for i in self.issues_data if query in search_text(i)
                and (not mine or (i.get('assignee') or {}).get('accountId') == me)]

    def groups(self, issues):
        return [] if self.no_sprint() else group_issues(self.board_data['config'], issues)

    def lanes(self, issues):
        return None  # shortcut: one flat board; Jira's swimlane settings are not in its public board API

    def column_titles(self):
        return [title for title, _ in group_issues(self.board_data['config'], self.issues_data)] if self.board_data else []

    def filters(self):
        """The local filters hiding loaded issues. None of them changes the board in Jira."""
        reasons = ['assigned to me'] if self.view()['mine'] else []
        if self.query_one('#filter', Input).value:
            reasons.append('text filter')
        return reasons

    def show_counts(self):
        if self.no_sprint():  # the board itself explains it
            self.status('No sprint shown' + HINT)
            return
        shown, total = len(self.visible_issues()), len(self.issues_data)
        empty = f'No issues in {sprint_label(self.sprint_data)}' if self.sprint_data else 'No issues on this board'
        parts = [f'{shown} of {total} issues' if shown != total else f'{total} issues' if total else empty]
        parts += self.filters()
        if total and not shown:
            parts.append('x shows all')
        hidden = len(set(self.view()['hidden']) & set(self.column_titles()))
        if hidden:
            parts.append(f'{hidden} hidden column{"s" if hidden > 1 else ""}')
        if not self.scrum() and ((self.board_data or {}).get('config') or {}).get('subQuery'):
            parts.append('kanban sub-filter applied')
        self.status(' · '.join(parts) + HINT)

    def ref(self, issue):
        return issue['key']

    def same(self, issue, other):
        return issue['key'] == other['key']

    def details(self, issue):
        return issue_details(issue, self.descriptions.get(issue['key'], NOT_LOADED), self.source)

    def open_issue(self, issue, status):
        url = self.client.base_url + '/browse/' + quote(issue['key'], safe='')
        if webbrowser.open(url):
            self.notify(url, title=f'Opening {issue["key"]}')
        else:
            status(f'No browser available. Open {url}', error=True)

    async def read_comments(self, issue):
        """The comments, and the description with them: both are only read when the details expand."""
        description, comments = await asyncio.gather(self.client.description(issue), self.client.comments(issue))
        self.descriptions[issue['key']] = description
        return comments

    def comment_view(self, comment):
        return comment_text(comment, self.source)

    def comments_screen(self, issue, writable=True):
        return CommentsScreen(self.client, issue, writable)

    # Choosing what to show

    def action_backlog(self):
        if self.board_data:
            self.push_screen(BacklogScreen(self.client, {'id': self.board_data['id'], 'name': self.board_name()}),
                             self.backlog_closed)

    def action_pick_board(self):
        if self.busy:
            return
        scope = self.prefs.data['project']
        picker = Picker(f'Boards{f" of {scope}" if scope else ""} · recently opened first')
        self.push_screen(picker, self.board_picked)
        self.load_board_list(picker)

    @work(group='boards')
    async def load_board_list(self, picker):
        scope = self.prefs.data['project']
        try:
            boards = await self.list_boards()
        except JiraError as exc:
            picker.fail(f'Could not load the boards{f" of {scope}" if scope else ""}. {exc}')
            return
        visited, choices = self.prefs.data['visited'], []
        for board in sorted(boards, key=lambda b: (-visited.get(str(b['id']), 0), (b.get('name') or '').casefold())):
            board_id, location = str(board['id']), board.get('location') or {}
            where = location.get('projectKey') or location.get('displayName') or ''
            note = '  (current)' if board_id == self.board_id else f'  · opened {ago(visited[board_id])}' \
                if board_id in visited else ''
            choices.append((board_id, Text.assemble(plain(board.get('name')), (f'  {board.get("type", "")} · {plain(where)}'
                                                                               f'{note}', 'dim')),
                            f'{board.get("name")} {where} {location.get("projectName") or ""}'.casefold(),
                            board_id == self.board_id))
        if scope:
            choices.append((ALL, Text(f'All projects…  (boards outside {scope})', 'italic'), None, False))
        picker.load(choices, f'Search {len(boards)} boards')

    def board_picked(self, value):
        if value == ALL:
            self.set_scope(None)
            self.action_pick_board()
        elif value:
            self.open_board(value)

    def action_pick_project(self):
        if self.busy:
            return
        picker = Picker('Project · narrows the boards b lists')
        self.push_screen(picker, self.project_picked)
        self.load_projects(picker)

    @work(group='projects')
    async def load_projects(self, picker):
        try:
            recent = await self.client.recent_projects()
        except JiraError as exc:
            picker.fail(f'Could not load your recent projects. {exc}')
            return
        current = self.prefs.data['project']
        choices = [(ALL, Text('All projects'), 'all projects', not current)]
        for project in recent:
            key, name = project.get('key') or '', project.get('name') or ''
            choices.append((key, Text.assemble((plain(key), 'bold'), f'  {plain(name)}', ('  (current)' if key == current
                                                                                          else '', 'dim')),
                            f'{key} {name}'.casefold(), key == current))
        choices.append((OTHER, Text('Another project key…', 'italic'), None, False))
        picker.load(choices, 'Your recent projects · type to search')

    def project_picked(self, value):
        if value == OTHER:
            self.push_screen(CommentDialog('Project key', 'e.g. ABC'), self.project_typed)
        elif value:
            self.set_scope(None if value == ALL else value)
            self.action_pick_board()

    def project_typed(self, key):
        key = (key or '').strip().upper()
        if key and not PROJECT_KEY.fullmatch(key):
            self.status(f'{key} is not a Jira project key, such as ABC.', error=True)
        elif key:
            self.project_picked(key)

    def set_scope(self, project):
        self.prefs.data['project'] = project
        self.save_prefs()
        self.show_scope()

    def action_view(self):
        if not self.board_data:
            return
        view, titles = self.view(), self.column_titles()
        selections = [(Text('Only issues assigned to me  (M)'), 'mine', view['mine']),
                      (Text('Compact one-line cards'), 'compact', view['compact'])]
        selections += [(Text(f'Show column: {title}'), ('column', title), title not in view['hidden']) for title in titles]

        async def apply(selected):
            hidden = [t for t in view['hidden'] if t not in titles] + [t for t in titles if ('column', t) not in selected]
            self.prefs.set_view(self.board_id, mine='mine' in selected, compact='compact' in selected, hidden=hidden)
            self.save_prefs()
            await self.render_board()

        self.push_screen(ViewDialog(f'My view of {plain(self.board_name())}', selections, apply),
                         lambda _: self.action_focus_board())

    async def action_clear_filters(self):
        if self.board_data and self.filters():
            self.prefs.set_view(self.board_id, mine=False)
            self.save_prefs()
            self.query_one('#filter', Input).value = ''
            await self.render_board()
            self.action_focus_board()
            self.notify('Showing every issue on this board.')

    # Changes, each read back from Jira afterwards

    def action_assign(self):
        if not self.selected or self.busy:
            return
        issue = self.selected
        picker = Picker(f'Assign {issue["key"]} · {plain(issue["summary"])}')
        self.push_screen(picker, lambda user: self.assigned(issue, user))
        self.load_people(picker, issue)

    @work(group='people')
    async def load_people(self, picker, issue):
        me = self.me.get('accountId')
        try:
            if issue['key'] not in self.people:
                self.people[issue['key']] = await self.client.assignable(issue, me)
        except JiraError as exc:
            picker.fail(f'Could not load who can be assigned {issue["key"]}. {exc}')
            return
        users, cut = self.people[issue['key']]
        current = (issue.get('assignee') or {}).get('accountId')
        active = {(i.get('assignee') or {}).get('accountId') for i in self.issues_data}
        eligible = sorted(users, key=lambda u: (u['accountId'] != me, u['accountId'] not in active,
                                                (u.get('displayName') or '').casefold()))
        choices = []
        if current and current not in {u['accountId'] for u in eligible}:  # e.g. someone who left; still worth seeing
            choices.append(({}, Text.assemble(plain(person(issue)), ('  (current)', 'dim')), None, True))
        for user in eligible:
            account = user['accountId']
            note = ('  (current)' if account == current else '  · you' if account == me
                    else '  · on this board' if account in active else '')
            name = plain(user.get('displayName')) or account
            choices.append((user, Text.assemble(name, (note, 'dim')), name.casefold(), account == current))
        choices.append(({'accountId': None}, Text.assemble('Unassigned', ('' if current else '  (current)', 'dim')),
                        'unassigned', not current))
        picker.load(choices, f'Search {len(eligible)}{" (the first 1000)" if cut else ""} people who can be assigned '
                             f'{issue["key"]}')

    def assigned(self, issue, user):
        if user:
            done = f'Assigned {issue["key"]} to {user.get("displayName")}.' if user['accountId'] else \
                f'Unassigned {issue["key"]}.'
            self.write(issue, lambda: self.client.assign(issue, user['accountId']), done)

    def action_move(self):
        if not self.selected or self.busy or not self.board_data:
            return
        issue = self.selected
        picker = Picker(f'Move {issue["key"]} · workflow transitions')
        self.push_screen(picker, lambda transition: transition and self.chose_transition(issue, transition))
        self.load_transitions(picker, issue)

    @work(group='transitions')
    async def load_transitions(self, picker, issue):
        """Ask Jira now: the board may be stale, and the workflow decides which transitions the issue has."""
        try:
            transitions = await self.client.transitions(issue)
        except JiraError as exc:
            picker.fail(f'Could not load the transitions of {issue["key"]}. {exc}')
            return
        if not transitions:
            picker.fail(f'Jira offers no transition for {issue["key"]} from {issue["status"]["name"]}.')
            return
        # Preselect the transition into the nearest later column: the usual next step on a board.
        place = {status: n for n, (_, ids) in enumerate(columns(self.board_data['config'])) for status in ids}
        here = place.get(issue['status']['id'], len(place))
        ahead = [t for t in transitions if place.get(str((t.get('to') or {}).get('id')), -1) > here]
        picker.value = min(ahead, key=lambda t: place[str(t['to']['id'])]) if ahead else None
        choices = []
        for transition in transitions:
            choosable, other = required_fields(transition)
            asks = [f.get('name') or k for k, f in choosable.items()] + other
            note = f'  to {plain((transition.get("to") or {}).get("name"))}' + \
                (f'  · asks for {plain(", ".join(asks))}' if asks else '')
            choices.append((transition, Text.assemble('→ ', plain(transition.get('name')), (note, 'dim')),
                            f'{transition.get("name")} {note}'.casefold(), False))
        picker.load(choices, f'Transitions from {plain(issue["status"]["name"])} · type to search')

    def chose_transition(self, issue, transition):
        choosable, other = required_fields(transition)
        if other:
            self.status(f'{transition.get("name")} needs {", ".join(other)}, which only Jira\'s own form can fill in. '
                        f'Press o to open {issue["key"]} in Jira.', error=True)
            return
        self.ask_fields(issue, transition, list(choosable.items()), {})

    def ask_fields(self, issue, transition, pending, chosen):
        """Each required choice field of the transition, such as the resolution, then the transition itself."""
        if not pending:
            target = (transition.get('to') or {}).get('name') or transition.get('name')
            self.write(issue, lambda: self.client.transition(issue, transition['id'], chosen),
                       f'Moved {issue["key"]} to {target} ({transition.get("name")}).')
            return
        (field_id, field), rest = pending[0], pending[1:]
        values = [v for v in field['allowedValues'] if isinstance(v, dict) and v.get('id')]
        choices = [(str(v['id']), Text(plain(v.get('name') or v.get('value') or str(v['id']))),
                    str(v.get('name') or v.get('value') or '').casefold(), False) for v in values]
        default = str((field.get('defaultValue') or {}).get('id') or '') or None
        self.push_screen(Picker(f'{plain(transition.get("name"))} {issue["key"]} · {plain(field.get("name") or field_id)} '
                                '(required)', choices, default),
                         lambda value: value and self.ask_fields(issue, transition, rest, {**chosen, field_id: value}))

    def action_comment(self):
        if self.selected and not self.busy:
            issue = self.selected
            self.push_screen(CommentDialog(f'Comment on {issue["key"]}', value=self.drafts.get(issue['key'], '')),
                             lambda value: value and self.write(issue, lambda: self.send_comment(issue, value),
                                                                f'Comment added to {issue["key"]}.', reload=False))

    async def send_comment(self, issue, text):
        self.drafts[issue['key']] = text  # kept until Jira takes it, so c brings it back after a failure
        try:
            await self.save_comment(issue, text)
        except JiraError as exc:
            raise JiraError(f'{exc} Your comment is kept: press c to send it again.') from exc
        self.drafts.pop(issue['key'], None)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='jira-board', description='A keyboard-driven Jira Cloud board in the terminal',
        epilog='Credentials: JIRA_URL, JIRA_EMAIL and JIRA_API_TOKEN if set, else the saved ones (site and email in a '
               'config file, API token in the OS credential store), else the ALS keychain item als-atlassian-api, '
               'for https://richemont.atlassian.net only, else setup asks for them.')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--demo', action='store_true', help='try with in-memory sample data; reads and saves nothing')
    modes.add_argument('--setup', action='store_true', help='enter a Jira site, email and API token, save them, then start')
    modes.add_argument('--logout', action='store_true', help='remove the saved Jira sites and tokens, then exit')
    modes.add_argument('--auth-status', action='store_true', help='check the credential source and account without opening the board')
    parser.add_argument('--verbose', action='store_true', help='show diagnostic logs; F12 toggles the panel')
    args = parser.parse_args(argv)
    if args.demo:
        client = DemoClient()
        JiraApp(client, Prefs(None, client.base_url, demo_state()), verbose=args.verbose).run()
        return
    try:
        if args.logout:
            print(credentials.logout())
            return
        account, connection = credentials.connect(force_setup=args.setup, interactive=not args.auth_status)
        if args.auth_status:
            print(connection)
            return
        client = Client(account.url, account.email, account.token)
    except (credentials.SetupError, JiraError, OSError) as exc:
        sys.exit(f'jira-board: {exc}')
    JiraApp(client, Prefs(default_path('jira-board'), client.base_url), connection, verbose=args.verbose).run()


if __name__ == '__main__':
    main()
