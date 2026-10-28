import argparse
import os
import sys
import time
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
from issueboard.markup import markdown
from issueboard.prefs import Prefs, default_path
from issueboard.story_merge_requests import StoryMergeRequests, demo_gitlab
from issueboard.widgets import (ALL, HINT, BacklogScreen as BaseBacklogScreen, Card as BaseCard, CommentDialog,
                                CommentsScreen as BaseCommentsScreen, Picker, Row as BaseRow, ViewDialog, ago, plain)
from . import credentials
from .client import (Client, YouTrackError, assignable_field, custom_field, field_values, group_issues, movable_field,
                     project_lanes, uses_sprints)
from .demo import DemoClient, demo_state


def names(issue, field):
    return ', '.join(v.get('name') or v.get('login', '') for v in field_values(issue, field))


def logins(issue):
    return {v['login'] for v in field_values(issue, 'Assignee') if v.get('login')}


def project_ids(board):
    return {p['id'] for p in board.get('projects') or []}


def column_field(board):
    return (((board or {}).get('columnSettings') or {}).get('field') or {}).get('name')


def search_text(issue):
    """What the local text filters match: ID, summary and assignee names and logins."""
    return f'{issue["idReadable"]} {issue["summary"]} {names(issue, "Assignee")} {" ".join(logins(issue))}'.casefold()


def body(text, empty, source=False):
    """An issue description or comment: Markdown as YouTrack would format it, or its source as typed."""
    text = plain(text).strip()
    return Text(empty, 'dim') if not text else Text(text) if source else markdown(text)


def issue_details(issue, field, source):
    """The details pane: ID, summary, the fields a board shows, and the description."""
    shown = ('State', 'Priority', 'Assignee', field)
    fields = '  ·  '.join(f'{f["name"]}: {names(issue, f["name"]) or "—"}' for f in issue['customFields'] if f['name'] in shown)
    return Group(Text.assemble((issue['idReadable'], 'bold'), f'  {plain(issue["summary"])}\n', plain(fields), '\n'),
                 body(issue.get('description'), 'No description.', source))


def swimlane_note(board):
    """Swimlanes the board has in YouTrack that this app does not draw, so the flat columns are not mistaken for them."""
    settings = board.get('swimlaneSettings') or {}
    if not settings.get('enabled') or project_lanes(board, []) is not None:
        return None
    values = [v.get('name') for v in settings.get('values') or [] if v.get('name')]
    field = settings.get('field') or {}
    kind = '/'.join(values) if settings.get('$type') == 'IssueBasedSwimlaneSettings' and values \
        else field.get('presentation') or field.get('name') or 'Board'
    return f'{plain(kind)} swimlanes not shown'


def comment_text(comment, source=False):
    author = comment.get('author') or {}
    created, updated = comment.get('created'), comment.get('updated')
    when = time.strftime('%Y-%m-%d %H:%M', time.localtime(created / 1000)) if isinstance(created, int) else 'unknown time'
    visibility = comment.get('visibility') or {}
    audience = [g.get('name') for g in visibility.get('permittedGroups') or []] + \
        [u.get('fullName') or u.get('login') for u in visibility.get('permittedUsers') or []]
    limited = visibility.get('$type') == 'LimitedVisibility'
    header = Text.assemble(
        (plain(author.get('fullName') or author.get('login')) or 'Unknown user', 'bold'),
        (f'  {when}' + ('  · edited' if updated and updated != created else ''), 'dim'),
        (f'  · visible to {plain(", ".join(filter(None, audience))) or "a limited audience"}' if limited else '', 'italic'))
    return Group(header, body(comment.get('text'), '(no text)', source))


class CommentsScreen(BaseCommentsScreen):
    def compose(self) -> ComposeResult:
        with Vertical(id='dialog', classes='comments'):
            yield Label(Text(f'Comments on {self.issue["idReadable"]} · {plain(self.issue["summary"])}'))
            yield VerticalScroll(id='thread')
            yield Static('', id='hint', classes='hint')


class Card(BaseCard):
    def compose(self):
        if self.has_class('-compact'):
            yield Static(Text.assemble((self.issue['idReadable'], 'bold'), ' ', plain(self.issue['summary'])))
        else:
            yield Static(Text.assemble((self.issue['idReadable'], 'bold'), '\n', plain(self.issue['summary']), '\n',
                                       (names(self.issue, 'Assignee') or 'Unassigned', 'dim')))


class Row(BaseRow):
    """A backlog issue on one line: ID, summary, then the board's column value and the assignee."""

    def compose(self):
        meta = f'{plain(names(self.issue, self.field)) or "—"} · {plain(names(self.issue, "Assignee")) or "Unassigned"}'
        yield Static(Text(self.issue['idReadable'].ljust(self.id_width), 'bold'), classes='id')
        yield Static(Text(plain(self.issue['summary'])), classes='summary')
        yield Static(Text(meta, 'dim'), classes='meta')


class BacklogScreen(BaseBacklogScreen):
    MORE = 'More in YouTrack'
    SEARCH = 'Search loaded issues: ID, summary, state or assignee  (/)'

    def __init__(self, client, board):
        super().__init__(client, board)
        self.saved, self.field = None, 'State'
        self.explanation = 'Loading the backlog…'

    def matches(self, issue):
        return self.query_one(Input).value.casefold() in f'{search_text(issue)} {names(issue, self.field).casefold()}'

    @work(exclusive=True, group='backlog')
    async def load(self):
        """The board's backlog setting as YouTrack has it now, then the first page."""
        keep = (self.current() or {}).get('id')
        self.saved, self.issues, self.more = None, [], False
        await self.render_rows()
        self.note('Loading the backlog…', 'Loading the backlog…')
        try:
            board = await self.client.board(self.board['id'])
            saved, self.field = board.get('backlog'), column_field(board) or 'State'
            self.saved = saved if (saved or {}).get('id') else None
            self.query_one('#backlog-title', Static).update(Text.assemble(
                ('Backlog', 'bold'), f' · {plain(self.board["name"])}  ',
                (f'saved search {plain(saved.get("name"))}: {plain(saved.get("query"))}' if self.saved else '', 'dim')))
            if not self.saved:
                self.note('No backlog', 'YouTrack returned no backlog for this board: none is set, or its saved search may '
                          "not be shared with you. A board's backlog is a saved search chosen in the board settings.")
                return
            self.issues, self.more = await self.client.backlog(saved, 0, self.PAGE)
        except YouTrackError as exc:
            self.failed(exc, 'Could not load the backlog', 'Press r to retry.')
            return
        await self.render_rows(keep)

    @work(exclusive=True, group='backlog')
    async def load_more(self):
        self.status(f'Loading {self.PAGE} more…')
        try:
            page, more = await self.client.backlog(self.saved, len(self.issues), self.PAGE)
        except YouTrackError as exc:
            self.failed(exc, 'Could not load more', 'Press n to retry.')
            return
        known = {i['id'] for i in self.issues}
        page = [i for i in page if i['id'] not in known]  # the backlog can change between two pages
        self.issues, self.more = self.issues + page, more
        # Continue at the first new issue the search shows.
        await self.render_rows(next((i['id'] for i in page if self.matches(i)), (self.current() or {}).get('id')))

    def failed(self, exc, summary, retry):
        shared = ' The backlog saved search may not be shared with you, or no longer exists.' \
            if self.saved and exc.status in (403, 404) else ''
        self.note(f'{summary}. {retry} {exc}', f'{summary}. {exc}{shared}\n\n{retry}', error=True)

    def show_counts(self, shown):
        loaded, query = len(self.issues), self.query_one(Input).value
        if not self.saved or not (loaded or self.more):
            if self.saved:
                self.note('The backlog is empty', f'No issue matches the saved search {plain(self.saved.get("name"))} now.')
            return
        more = f'n {"searches" if query else "loads"} {self.PAGE} more' if self.more else ''
        if query:
            count = f'{shown} of {loaded}{" loaded" if self.more else ""} match' if shown else \
                f'No{" loaded" if self.more else ""} issue matches'
            self.note(' · '.join(filter(None, [count, more])), 'No issue selected.')
        else:
            self.note(' · '.join(filter(None, [f'{loaded} issues{" loaded" if self.more else ""}, in backlog order', more])),
                      'No issue selected.')

    def row(self, issue, width):
        return Row(issue, self.field, width)

    def details(self, issue):
        return issue_details(issue, self.field, self.app.source)


class BoardApp(StoryMergeRequests, BoardView):
    TITLE = 'YouTrack Board'
    SERVICE = 'YouTrack'
    ERROR = YouTrackError  # what the client raises when the service refuses or cannot be reached
    CARD = Card

    def __init__(self, client, prefs=None, connection=None, gitlab=None, cwd=None, verbose=False):
        super().__init__(client, prefs or Prefs(None, client.base_url), connection, verbose)
        self.gitlab, self.gitlabs = gitlab, {}
        self.cwd = cwd
        self.sprint_data = None
        self.sprints, self.sprint_memory = [], {}

    def refresh_context(self):
        return self.board_data, self.sprint_data

    def compose(self):
        yield from self.identity()
        with Horizontal(id='toolbar'):
            yield Button('All projects ▾', id='project', compact=True, action='app.pick_project', tooltip='Project (p)')
            yield Button('Board ▾', id='boards', compact=True, action='app.pick_board', tooltip='Board (b)')
            yield Select([], prompt='Sprint', id='sprints', compact=True)
            yield Input(placeholder='Filter by ID, summary or assignee  (/)', id='filter', compact=True)
            yield Button('', id='auto', compact=True, action='app.auto', tooltip='Auto-refresh this board (R)')
        yield Static('Loading boards…', id='status', markup=False)
        yield Horizontal(id='heads')  # the status columns, shared by every swimlane
        yield ScrollableContainer(id='board', can_focus=False)
        with VerticalScroll(id='details-pane', can_maximize=True):
            yield Static('', id='details', markup=False)
        if self.log_buffer is not None:
            yield LogPanel()
        yield Footer(show_command_palette=False)

    def projects(self):
        return {p['id']: p for b in self.boards for p in b.get('projects') or []}

    def scope(self):
        return self.projects().get(self.prefs.data['project'])

    def ranked_boards(self):
        """Boards in the project scope, most recently opened here first, then by name."""
        scope, visited = self.prefs.data['project'], self.prefs.data['visited']
        boards = [b for b in self.boards if not scope or scope in project_ids(b)]
        return sorted(boards, key=lambda b: (-visited.get(b['id'], 0), b['name'].casefold()))

    def show_scope(self):
        scope = self.scope()
        board = next((b for b in self.boards if b['id'] == self.board_id), None)
        labels = (('#project', scope['shortName'] if scope else 'All projects'), ('#boards', board['name'] if board else 'Board'))
        for button, label in labels:
            self.query_one(button, Button).label = Content(label + ' ▾')
            # A new label doesn't resize an auto-width Button, and a refresh during a modal callback is dropped.
            self.call_after_refresh(self.query_one(button).refresh, layout=True)

    @work(exclusive=True, group='load')
    async def load_boards(self):
        self.status('Loading boards…')
        try:
            self.boards = await self.client.boards()
            self.me = await self.client.me()
        except YouTrackError as exc:
            self.status(f'{exc} — press r to retry.', error=True)
            return
        if not self.boards:
            self.status('No accessible boards found.', error=True)
            return
        if not self.scope():
            self.prefs.data['project'] = None
        self.show_scope()
        self.load_board(self.ranked_boards()[0]['id'])

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
            sprints = await self.client.sprints(board_id) if uses_sprints(board) else []
        except YouTrackError as exc:
            self.status(f'{exc} — press r to retry.', error=True)
            return
        self.board_data, self.sprints = board, sprints
        self.prefs.visit(board_id)
        self.save_prefs()
        selector.display = uses_sprints(board)
        if not selector.display:  # every issue on the board; there is no sprint to wait for
            await self.fetch_issues()
            return
        selector.set_options([(s['name'] + (' (archived)' if s.get('archived') else ''), s['id']) for s in sprints])
        current = (board.get('currentSprint') or {}).get('id')
        choice = next((s for s in sprints if s['id'] == self.sprint_memory.get(board_id)), None)
        choice = choice or next((s for s in sprints if s['id'] == current), None)
        choice = choice or next((s for s in sprints if not s.get('archived')), None)
        if choice:
            selector.value = choice['id']
        else:
            self.status('No active sprint. Select an archived sprint if available.')

    @on(Select.Changed, '#sprints')
    async def sprint_changed(self, event):
        if not uses_sprints(self.board_data):  # a late event from clearing the hidden selector
            return
        self.sprint_data = next((s for s in self.sprints if s['id'] == event.value), None) if self.board_data else None
        self.issues_data = []
        await self.render_board()
        if self.sprint_data:
            self.sprint_memory[self.board_id] = self.sprint_data['id']
            self.load_issues()

    def loaded(self):
        """A board and, if it uses sprints, a sprint are chosen, so its issues can be (re)loaded."""
        return bool(self.board_data) and (bool(self.sprint_data) or not uses_sprints(self.board_data))

    async def read(self):
        """The issues of the board (and sprint) on screen, as the service has them now."""
        return await self.client.issues(self.board_data, self.sprint_data)

    def visible_issues(self):
        query = self.query_one('#filter', Input).value.casefold()
        scope, mine, me = self.prefs.data['project'], self.view()['mine'], self.me.get('login')
        return [i for i in self.issues_data
                if query in search_text(i)
                and (not scope or (i.get('project') or {}).get('id') == scope)
                and (not mine or me in logins(i))]

    def column_titles(self):
        return [title for title, _ in group_issues(self.board_data, self.issues_data)] if self.board_data else []

    def filters(self):
        """The local filters hiding loaded issues. None of them changes the board in YouTrack."""
        scope, reasons = self.scope(), []
        if scope:
            reasons.append(f'{scope["shortName"]} only')
        if self.view()['mine']:
            reasons.append('assigned to me')
        if self.query_one('#filter', Input).value:
            reasons.append('text filter')
        return reasons

    def show_counts(self):
        shown, total = len(self.visible_issues()), len(self.issues_data)
        empty = f'No issues in {plain(self.sprint_data["name"])}' if self.sprint_data else 'No issues on this board'
        parts = [f'{shown} of {total} issues' if shown != total else f'{total} issues' if total else empty]
        parts += self.filters()
        if total and not shown:
            parts.append('x shows all')
        hidden = len(set(self.view()['hidden']) & set(self.column_titles()))
        if hidden:
            parts.append(f'{hidden} hidden column{"s" if hidden > 1 else ""}')
        lanes, shown_lanes = project_lanes(self.board_data, self.visible_issues()), len(self.query('#board > .lane'))
        if lanes and shown_lanes < len(lanes):
            parts.append(f'{shown_lanes} of {len(lanes)} lanes have cards')
        parts += filter(None, [swimlane_note(self.board_data)])
        self.status(' · '.join(parts) + HINT)

    def groups(self, issues):
        """(column title, its issues) for the given issues, in board order."""
        return group_issues(self.board_data, issues)

    def lanes(self, issues):
        """(lane value, its issues) per swimlane, or None when the board shows no swimlanes."""
        return project_lanes(self.board_data, issues)

    def lane_label(self, value, count):
        if value:
            name, full = plain(value.get('name')), plain(value.get('presentation'))
            return Text.assemble((name, 'bold'), f'  {full}' if full and full != name else '', (f'  {count}', 'dim'))
        hidden = self.board_data.get('hideOrphansSwimlane')
        return Text.assemble(('Uncategorized cards', 'bold'), (f'  {count}', 'dim'),
                             ('  · not a lane on the YouTrack board' if hidden else '', 'dim'))

    def ref(self, issue):
        return issue['idReadable']

    def details(self, issue):
        return issue_details(issue, column_field(self.board_data), self.source)

    def open_issue(self, issue, status):
        url = self.client.base_url + '/issue/' + quote(issue['idReadable'], safe='')
        if webbrowser.open(url):
            self.notify(url, title=f'Opening {issue["idReadable"]}')
        else:
            status(f'No browser available. Open {url}', error=True)

    def action_backlog(self):
        board = next((b for b in self.boards if b['id'] == self.board_id), None)
        if board:
            self.push_screen(BacklogScreen(self.client, board), self.backlog_closed)

    def action_pick_board(self):
        if self.busy or not self.boards:
            return
        scope, visited = self.scope(), self.prefs.data['visited']
        choices = []
        for board in self.ranked_boards():
            spans = ', '.join(p['shortName'] for p in board.get('projects') or [])
            note = '  (current)' if board['id'] == self.board_id else f'  · opened {ago(visited[board["id"]])}' \
                if board['id'] in visited else ''
            search = ' '.join([board['name'], spans] + [p.get('name', '') for p in board.get('projects') or []])
            choices.append((board['id'], Text.assemble(board['name'], (f'  {spans}{note}', 'dim')), search.casefold(),
                            board['id'] == self.board_id))
        if scope:
            choices.append((ALL, Text(f'All projects…  (boards outside {scope["shortName"]})', 'italic'), None, False))
        title = f'Boards with {scope["shortName"]}' if scope else 'Boards'
        self.push_screen(Picker(title + ' · recently opened first', choices), self.board_picked)

    def board_picked(self, value):
        if value == ALL:
            self.set_scope(None)
            self.action_pick_board()
        elif value:
            self.open_board(value)

    def action_pick_project(self):
        if self.busy or not self.boards:
            return
        current, visited = self.prefs.data['project'], self.prefs.data['visited']
        boards = {pid: [b for b in self.boards if pid in project_ids(b)] for pid in self.projects()}
        recent = {pid: max(visited.get(b['id'], 0) for b in group) for pid, group in boards.items()}
        ordered = sorted(self.projects().values(), key=lambda p: (-recent[p['id']], p['shortName'].casefold()))
        note = f'  {len(self.boards)} boards' + ('' if current else '  (current)')
        choices = [(ALL, Text.assemble('All projects', (note, 'dim')), 'all projects', not current)]
        for project in ordered:
            count = len(boards[project['id']])
            note = f'  {count} board{"s" if count > 1 else ""}' + ('  (current)' if project['id'] == current else '')
            name = project.get('name', '')
            choices.append((project['id'], Text.assemble((project['shortName'], 'bold'), f'  {name}', (note, 'dim')),
                            f'{project["shortName"]} {name}'.casefold(), project['id'] == current))
        self.push_screen(Picker('Project', choices), self.project_picked)

    async def project_picked(self, value):
        if value is None:
            return
        self.set_scope(None if value == ALL else value)
        board = next((b for b in self.boards if b['id'] == self.board_id), None)
        if value != ALL and (not board or value not in project_ids(board)):
            target = self.ranked_boards()[0]
            self.notify(f'Opened {target["name"]}, the most recently used board in {self.scope()["shortName"]}.'
                        + ('' if len(self.ranked_boards()) == 1 else ' Press b for its other boards.'))
            self.open_board(target['id'])
        else:  # same board: keep the sprint and selection, only narrow or widen the cards
            await self.render_board()
            self.action_focus_board()

    def set_scope(self, project_id):
        self.prefs.data['project'] = project_id
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

        self.push_screen(ViewDialog(f'My view of {self.board_data["name"]}', selections, apply),
                         lambda _: self.action_focus_board())

    async def action_clear_filters(self):
        """Undo every local filter at once: project scope, only mine and the text filter."""
        if self.board_data and self.filters():
            self.prefs.set_view(self.board_id, mine=False)
            self.set_scope(None)
            self.query_one('#filter', Input).value = ''
            await self.render_board()
            self.action_focus_board()
            self.notify('Showing every issue on this board.')

    def action_assign(self):
        if not self.selected or self.busy:
            return
        issue = self.selected
        if not assignable_field(issue):
            reason = 'uses a multi-user Assignee field, which cannot be changed here' if custom_field(issue, 'Assignee') \
                else 'has no Assignee field'
            self.status(f'{issue["idReadable"]} {reason}.', error=True)
            return
        picker = Picker(f'Assign {issue["idReadable"]} · {issue["summary"]}')
        self.push_screen(picker, lambda user: self.assigned(issue, user))
        self.load_people(picker, issue)

    @work(group='people')
    async def load_people(self, picker, issue):
        project = issue.get('project') or {}
        try:
            if project.get('id') not in self.people:
                self.people[project.get('id')] = await self.client.assignees(issue)
        except YouTrackError as exc:
            picker.fail(f'Could not load who can be assigned in {project.get("shortName", "this project")}. {exc}')
            return
        settings, users = self.people[project.get('id')]
        me, current = self.me.get('login'), logins(issue)
        active = {login for i in self.issues_data for login in logins(i)}
        eligible = sorted((u for u in users if isinstance(u, dict) and u.get('login') and not u.get('banned')),
                          key=lambda u: (u['login'] != me, u['login'] not in active,
                                         (u.get('fullName') or u['login']).casefold()))
        choices = []
        if current - {u['login'] for u in eligible}:  # e.g. a banned or removed user; still worth seeing
            choices.append(({}, Text.assemble(names(issue, 'Assignee'), ('  (current)', 'dim')),
                            names(issue, 'Assignee').casefold(), True))
        for user in eligible:
            name, login = user.get('fullName') or user['login'], user['login']
            note = ('  (current)' if login in current else '  · you' if login == me
                    else '  · on this board' if login in active else '')
            choices.append((user, Text.assemble(name, (f'  {login}{note}', 'dim')), f'{name} {login}'.casefold(), login in current))
        if settings.get('canBeEmpty'):
            empty = settings.get('emptyFieldText') or 'Unassigned'
            choices.append(({}, Text.assemble(empty, ('' if current else '  (current)', 'dim')), empty.casefold(), not current))
        picker.load(choices, f'Search {len(eligible)} people who can be assigned in {project.get("shortName", "this project")}')

    def assigned(self, issue, user):
        if user is not None:
            name = user.get('fullName') or user.get('login')
            self.write(issue, lambda: self.client.assign(issue, user.get('login')),
                       f'Assigned {issue["idReadable"]} to {name}.' if user else f'Unassigned {issue["idReadable"]}.')

    def action_move(self):
        if not self.selected or self.busy or not self.board_data:
            return
        issue, field = self.selected, column_field(self.board_data)
        columns = (self.board_data.get('columnSettings') or {}).get('columns', [])
        options = list(dict.fromkeys(v['name'] for c in columns for v in c['fieldValues']))
        found = custom_field(issue, field) if field else None
        if found and found.get('$type') == 'StateMachineIssueCustomField':
            # A workflow decides which states can follow, so offer its transitions rather than the board's columns.
            picker = Picker(f'Move {issue["idReadable"]} · {field} workflow transitions')
            self.push_screen(picker, lambda choice: choice and self.write(
                issue, lambda: self.apply_transition(issue, *choice), f'Moved {issue["idReadable"]}.'))
            self.load_transitions(picker, issue, field, columns)
            return
        if not options or not movable_field(issue, field):
            reason = ('this board has no columns to move to' if not options else f'it has no {field} field' if not found
                      else f'{field} is a {found.get("$type", "").removesuffix("IssueCustomField")} field')
            self.status(f'{issue["idReadable"]} cannot be moved: {reason}. '
                        'Moving supports State and Enum fields, including workflow-driven State fields.', error=True)
            return
        current = names(issue, field)
        # Start on the value after the current one: the usual next step on a board.
        following = options[(options.index(current) + 1) % len(options)] if current in options else None
        choices = [(name, Text.assemble(name, ('  (current)' if name == current else '', 'dim')), name.casefold(),
                    name == current) for name in options]
        self.push_screen(Picker(f'Move {issue["idReadable"]} · {field}', choices, following), lambda value: value and self.write(
            issue, lambda: self.client.move(issue, field, value), f'Moved {issue["idReadable"]} to {value}.'))

    @work(group='transitions')
    async def load_transitions(self, picker, issue, name, columns):
        """Ask YouTrack now: the board may be stale, and the workflow only allows some states from the current one."""
        try:
            field = await self.client.transitions(issue, name)
        except YouTrackError as exc:
            picker.fail(f'Could not load the workflow transitions for {issue["idReadable"]}. {exc}')
            return
        if not isinstance(field, dict) or field.get('$type') != 'StateMachineIssueCustomField':
            picker.fail(f'{name} on {issue["idReadable"]} is no longer workflow-driven. Press r to refresh the board.')
            return
        before, current = names(issue, name) or None, (field.get('value') or {}).get('name')
        events = [e for e in field.get('possibleEvents') or [] if isinstance(e, dict) and e.get('id')]
        if not events:
            picker.fail(f'YouTrack offers no transitions from {current or "an empty " + name} for {issue["idReadable"]}.')
            return
        # Preselect the transition into the nearest later column: the usual next step on a board.
        column = {v['name']: n for n, c in enumerate(columns) for v in c['fieldValues']}
        ahead = [e for e in events if column.get(e.get('presentation'), -1) > column.get(current, len(columns))]
        picker.value = (field, min(ahead, key=lambda e: column[e['presentation']])) if ahead else None
        choices = [((field, e), Text.assemble('→ ', e.get('presentation') or e['id'], (f'  {e["id"]}', 'dim')),
                    f'{e.get("presentation") or ""} {e["id"]}'.casefold(), False) for e in events]
        choices.append((None, Text('YouTrack may still refuse one if a workflow condition fails.', 'dim italic'), None, True))
        picker.load(choices, f'Now {current} in YouTrack (board showed {before})' if current != before
                    else f'Transitions from {current or "empty"} · type to search')

    async def apply_transition(self, issue, field, event):
        before = (field.get('value') or {}).get('name')
        after = await self.client.transition(issue, field, event)
        if after == before:  # a workflow guard can ignore a listed transition without an error
            raise YouTrackError(f'YouTrack kept {issue["idReadable"]} in {before}: a workflow condition blocked "{event["id"]}".')
        return f'Moved {issue["idReadable"]} to {after} ({event["id"]}).'

    def action_comment(self):
        if self.selected and not self.busy:
            issue = self.selected
            self.push_screen(CommentDialog(f'Comment on {issue["idReadable"]}'), lambda value: value and self.write(
                issue, lambda: self.save_comment(issue, value), f'Comment added to {issue["idReadable"]}.', reload=False))

    def comments_screen(self, issue, writable=True):
        return CommentsScreen(self.client, issue, writable)

    async def read_comments(self, issue):
        return await self.client.comments(issue)

    def comment_view(self, comment):
        return comment_text(comment, self.source)

    def story_project(self, issue):
        return issue.get('project')

    def context_project(self):
        board = self.board_data or {}
        return self.scope() or (board['projects'][0] if len(board.get('projects') or []) == 1 else None)

    def prefix(self, project_id):
        return (self.projects().get(project_id) or {}).get('shortName')

    def link(self, key):
        return {'idReadable': key}

    def same(self, issue, other):
        return issue['idReadable'] == other['idReadable']

    async def read_links(self, issue):
        """The issues linked to this one, parents first. A link type that aggregates (Subtask, or a custom one like
        it) goes from the parent, so a parent is linked inward; each link reads as its type words it from this side."""
        found = []
        for link in await self.client.links(issue):
            kind, inward = link.get('linkType') or {}, link.get('direction') == 'INWARD'
            why = (kind.get('targetToSource') if inward else kind.get('sourceToTarget')) or kind.get('name') or 'linked to'
            found += [{**self.link(other['idReadable']), 'ref': other['idReadable'], 'title': other.get('summary') or '',
                       'why': why, 'parent': inward and bool(kind.get('aggregation')), 'done': bool(other.get('resolved'))}
                      for other in link.get('issues') or [] if isinstance(other, dict) and other.get('idReadable')]
        return sorted(found, key=lambda link: not link['parent'])

    async def read_issue(self, link):
        return await self.client.issue(link['idReadable'])


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='issueboard youtrack', description='A small YouTrack Agile board TUI',
        epilog='Credentials: YOUTRACK_URL and YOUTRACK_TOKEN if set, else the saved ones (URL in a config file, token '
               'in the OS credential store), else a single YouTrack entry in the Codex or Claude Code MCP config, '
               'else setup asks for them.')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--demo', action='store_true', help='try with in-memory sample data; reads and saves nothing')
    modes.add_argument('--setup', action='store_true', help='enter or replace the saved YouTrack URL and token, then start')
    modes.add_argument('--logout', action='store_true', help='remove the saved YouTrack URL and token, then exit')
    modes.add_argument('--auth-status', action='store_true', help='check the credential source and account without opening the board')
    parser.add_argument('--open', action='store_true', help='with --setup, open the instance token page in your browser')
    parser.add_argument('--verbose', action='store_true', help='show diagnostic logs; F12 toggles the panel')
    args = parser.parse_args(argv)
    if args.open and not args.setup:
        parser.error('--open requires --setup')
    if args.demo:
        client = DemoClient()
        BoardApp(client, Prefs(None, client.base_url, demo_state()), gitlab=demo_gitlab, verbose=args.verbose).run()
        return
    try:
        if args.logout:
            print(credentials.logout())
            return
        account, connection = credentials.connect(force_setup=args.setup, open_browser=args.open,
                                                  interactive=not args.auth_status)
        if args.auth_status:
            print(connection)
            return
        client = Client(account.url, account.token)
    except (credentials.SetupError, YouTrackError, OSError) as exc:
        sys.exit(f'issueboard youtrack: {exc}')
    BoardApp(client, Prefs(default_path('youtrack-board'), client.base_url), connection, cwd=os.curdir, verbose=args.verbose).run()


if __name__ == '__main__':
    main()
