import argparse
import asyncio
import logging
import os
import shutil
import sys
import time
import webbrowser
from urllib.parse import quote

from rich.console import Group
from rich.rule import Rule
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, ListItem, ListView, OptionList, Select, SelectionList, Static
from textual.widgets.option_list import Option

from .diagnostics import LOGGER, LogBuffer, LogPanel
from . import credentials
from .client import (Client, YouTrackError, assignable_field, custom_field, field_values, group_issues, movable_field,
                     project_lanes, uses_sprints)
from .demo import DemoClient, demo_gitlab, demo_state
from .markup import markdown
from .prefs import Prefs, default_path

HINT = '  ·  ←/→ column  ↑/↓ card  Enter details  C comments  ? keys'
ALL = '*'  # picker value for "all projects"; YouTrack IDs never look like this
# Terminal control characters except tab and newline. Textual passes ESC through, so YouTrack text could restyle the screen.
CONTROL = dict.fromkeys([*range(9), *range(11, 32), *range(127, 160)])


def plain(value):
    return (value or '').translate(CONTROL)


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


def ago(timestamp):
    minutes = int((time.time() - timestamp) / 60)
    return ('just now' if minutes < 1 else f'{minutes} min ago' if minutes < 60
            else f'{minutes // 60} h ago' if minutes < 1440 else f'{minutes // 1440} d ago')


class Picker(ModalScreen):
    """Type to narrow the list, ↑/↓ to choose, Enter to pick. Dismisses with the chosen value, or None on Escape."""
    BINDINGS = [Binding('escape', 'dismiss', 'Cancel'),
                Binding('down', 'cursor("cursor_down")', show=False), Binding('up', 'cursor("cursor_up")', show=False),
                Binding('pagedown', 'cursor("page_down")', show=False), Binding('pageup', 'cursor("page_up")', show=False)]

    def __init__(self, title, choices=None, value=None):
        """Each choice is (value, label, search text or None to always show it, disabled). None means still loading."""
        super().__init__()
        self.prompt, self.choices, self.value, self.shown = title, choices, value, []
        self.placeholder, self.error = 'Loading…' if choices is None else 'Type to search', None

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog'):
            yield Label(Text(self.prompt))
            yield Input(id='search', compact=True)
            yield OptionList(id='choices')
            yield Static('↑/↓ choose  ·  Enter pick  ·  Esc cancel', id='hint', classes='hint')

    def on_mount(self):
        self.query_one(OptionList).can_focus = False
        self.sync()
        self.query_one(Input).focus()

    def load(self, choices, placeholder='Type to search'):
        """Fill in choices that arrived after opening; harmless once the picker is gone."""
        self.choices, self.placeholder = choices, placeholder
        if self.is_mounted and self.is_attached:
            self.sync()

    def fail(self, message):
        LOGGER.error('%s', message)
        self.placeholder, self.error = 'Unavailable', message
        if self.is_mounted and self.is_attached:
            self.sync()

    def sync(self):
        self.query_one(Input).placeholder = self.placeholder
        if self.error:
            self.query_one('#hint', Static).update(Text(self.error + '  ·  Esc close'))
            self.query_one('#hint').add_class('error')
        self.show()

    @on(Input.Changed)
    def show(self):
        words = self.query_one(Input).value.casefold().split()
        self.shown = [c for c in self.choices or [] if c[2] is None or all(w in c[2] for w in words)]
        options = self.query_one(OptionList)
        options.set_options(Option(label, disabled=disabled) for _, label, _, disabled in self.shown)
        if self.choices is not None and not self.shown:
            options.add_option(Option(Text('No matches', 'dim'), disabled=True))
        enabled = [n for n, choice in enumerate(self.shown) if not choice[3]]
        options.highlighted = next((n for n in enabled if self.shown[n][0] == self.value), enabled[0] if enabled else None)

    @on(Input.Submitted)
    def submit(self):
        self.pick(self.query_one(OptionList).highlighted)

    @on(OptionList.OptionSelected)
    def clicked(self, event):
        self.pick(event.option_index)

    def pick(self, index):
        if index is not None and index < len(self.shown) and not self.shown[index][3]:
            self.dismiss(self.shown[index][0])

    def action_cursor(self, action):
        getattr(self.query_one(OptionList), 'action_' + action)()


class CommentDialog(ModalScreen):
    BINDINGS = [('escape', 'dismiss', 'Cancel')]

    def __init__(self, title, placeholder='Comment text', value=''):
        super().__init__()
        self.prompt, self.placeholder, self.value = title, placeholder, value

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog'):
            yield Label(Text(self.prompt))
            yield Input(self.value, placeholder=self.placeholder, id='value')
            yield Static('Enter save  ·  Esc cancel', classes='hint')

    def on_mount(self):
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def save_comment(self, event):
        if event.value.strip():
            self.dismiss(event.value.strip())


class InlineComments:
    """The selected issue's comments under its details, read when the details expand (Enter) and on r there, never
    while the cursor moves. An answer only shows while its issue is selected; a newer read cancels an older one."""
    thread = None  # {'id': issue id, 'comments': list or None, 'error': text or None, 'loading': bool}

    def with_comments(self, issue, details):
        thread = self.thread if self.thread and self.thread['id'] == issue['id'] else None
        if not thread:
            return details
        comments, parts = thread['comments'], [details, Text('')]
        count = f'{len(comments)}, newest first' if comments else 'none yet, or none you can see' if comments == [] else ''
        state = 'loading…' if thread['loading'] else count
        parts.append(Rule(Text.assemble(('Comments', 'bold'), (f' · {state}' if state else '', 'dim')), align='left',
                          style='dim'))
        if thread['error']:
            parts.append(Text(plain(f'Could not load the comments. {thread["error"]} Press r to retry.'), 'red'))
        for comment in comments or []:
            parts += [self.app.comment_view(comment), Text('')]
        return Group(*parts)

    @work(exclusive=True, group='comments')
    async def load_comments(self, issue):
        old = self.thread if self.thread and self.thread['id'] == issue['id'] else {}
        self.thread = {'id': issue['id'], 'comments': old.get('comments'), 'error': None, 'loading': True}
        self.show_details()
        try:
            comments = await self.app.read_comments(issue)
        except self.app.ERROR as exc:
            LOGGER.error('Could not load comments: %s', exc)
            self.thread = {**self.thread, 'error': str(exc), 'loading': False}
        else:
            self.thread = {**self.thread, 'comments': comments, 'loading': False}
        self.show_details()


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


class CommentsScreen(ModalScreen):
    """One issue's comments, newest first, loaded when opened. Closing it drops an answer that is still on its way."""
    BINDINGS = [Binding('escape,C', 'dismiss', 'Close'), Binding('r', 'load', 'Reload'),
                Binding('c', 'dismiss("write")', 'Write a comment'), Binding('s', 'source', 'Markdown source')]

    def __init__(self, client, issue, writable=True):
        super().__init__()
        self.client, self.issue, self.comments, self.writable = client, issue, [], writable

    def check_action(self, action, parameters):
        return self.writable or parameters != ('write',)  # from the backlog, which only reads

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog', classes='comments'):
            yield Label(Text(f'Comments on {self.issue["idReadable"]} · {plain(self.issue["summary"])}'))
            yield VerticalScroll(id='thread')
            yield Static('', id='hint', classes='hint')

    def on_mount(self):
        self.query_one('#thread').focus()
        self.load()

    def action_load(self):
        self.load()

    def action_source(self):
        self.app.action_source()
        for widget, comment in zip(self.query('#thread > .comment'), self.comments):
            widget.update(comment_text(comment, self.app.source))

    async def show(self, widgets, hint, error=False):
        if error:
            LOGGER.error('Could not load comments: %s', hint)
        thread = self.query_one('#thread')
        await thread.remove_children()
        await thread.mount_all(widgets)
        thread.scroll_home(animate=False)
        self.query_one('#hint', Static).update(Text(hint + '  ·  Esc close'))
        self.query_one('#hint').set_class(error, 'error')

    @work(exclusive=True)
    async def load(self):
        await self.show([Static('Loading comments…', classes='note')], 'Loading')
        try:
            comments = await self.client.comments(self.issue)
        except YouTrackError as exc:
            await self.show([Static(Text(f'Could not load the comments. {exc}'), classes='note')], 'r retry', error=True)
            return
        self.comments = comments
        if not comments:
            await self.show([Static('No comments yet, or none you can see.', classes='note')],
                            ('c write one  ·  ' if self.writable else '') + 'r reload')
            return
        count = f'{len(comments)} comment{"s" if len(comments) > 1 else ""}, newest first'
        await self.show([Static(comment_text(c, self.app.source), classes='comment') for c in comments],
                        f'{count}  ·  ↑/↓ scroll  ·  {"c write  ·  " if self.writable else ""}s source  ·  r reload')


class ViewDialog(ModalScreen):
    """Personal view options for one board. Each toggle applies immediately."""
    BINDINGS = [('escape,v', 'dismiss', 'Done')]

    def __init__(self, title, toggles, apply):
        super().__init__()
        self.prompt, self.toggles, self.apply = title, toggles, apply

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog'):
            yield Label(Text(self.prompt))
            yield SelectionList(*self.toggles)
            yield Static('Space toggle  ·  Esc done  ·  Saved on this computer', classes='hint')

    def on_mount(self):
        self.query_one(SelectionList).focus()

    @on(SelectionList.SelectedChanged)
    async def changed(self, event):
        await self.apply(event.selection_list.selected)


class Card(ListItem):
    def __init__(self, issue, compact=False):
        super().__init__(classes='-compact' if compact else None)
        self.issue = issue

    def compose(self):
        if self.has_class('-compact'):
            yield Static(Text.assemble((self.issue['idReadable'], 'bold'), ' ', plain(self.issue['summary'])))
        else:
            yield Static(Text.assemble((self.issue['idReadable'], 'bold'), '\n', plain(self.issue['summary']), '\n',
                                       (names(self.issue, 'Assignee') or 'Unassigned', 'dim')))


class Column(ListView):
    BINDINGS = [Binding('enter', 'app.details', 'Details', show=False),
                Binding('j', 'cursor_down', 'Next card', show=False), Binding('k', 'cursor_up', 'Previous card', show=False),
                Binding('left,h', 'app.column(-1)', 'Previous column', show=False),
                Binding('right,l', 'app.column(1)', 'Next column', show=False)]

    def watch_index(self, old_index, new_index):
        super().watch_index(old_index, new_index)
        # Update at once: a key pressed right after j/k must act on the new card, not wait for ListView.Highlighted.
        if self.has_focus:
            self.app.show_card(self.highlighted_child)
            self.app.reveal(self)

    def action_cursor_down(self):
        if self.has_class('-lane') and (self.index is None or self.index >= len(self) - 1):
            self.app.action_lane(1)
        else:
            super().action_cursor_down()

    def action_cursor_up(self):
        if self.has_class('-lane') and not self.index:
            self.app.action_lane(-1)
        else:
            super().action_cursor_up()


class Row(ListItem):
    """A backlog issue on one line: ID, summary, then the board's column value and the assignee."""

    def __init__(self, issue, field, id_width):
        super().__init__()
        self.issue, self.field, self.id_width = issue, field, id_width

    def compose(self):
        meta = f'{plain(names(self.issue, self.field)) or "—"} · {plain(names(self.issue, "Assignee")) or "Unassigned"}'
        yield Static(Text(self.issue['idReadable'].ljust(self.id_width), 'bold'), classes='id')
        yield Static(Text(plain(self.issue['summary'])), classes='summary')
        yield Static(Text(meta, 'dim'), classes='meta')


class More(ListItem):
    def __init__(self, text):
        super().__init__()
        self.text = text

    def compose(self):
        yield Static(Text(self.text, 'italic'))


class Issues(ListView):
    """A list whose paging keys move the cursor; ListView's own only scroll the view."""
    BINDINGS = [Binding('j', 'step(1)', 'Next issue', show=False), Binding('k', 'step(-1)', 'Previous issue', show=False),
                Binding('pagedown', 'step(1, True)', show=False), Binding('pageup', 'step(-1, True)', show=False),
                Binding('end', 'step(1000000)', 'Last issue', show=False), Binding('home', 'step(-1000000)', show=False)]

    def action_step(self, rows, page=False):
        if len(self):
            rows *= max(1, self.scrollable_content_region.height - 1) if page else 1
            self.index = max(0, min(len(self) - 1, (self.index or 0) + rows))


class BacklogScreen(InlineComments, ModalScreen):
    """A board's backlog: the issues of the saved search the board uses as its backlog, a page at a time, in the order
    YouTrack keeps for it. It covers the board and only reads. Closing it cancels its requests, so no late answer lands,
    and the board underneath is left exactly as it was."""
    PAGE = 50
    MORE = 'More in YouTrack'
    BINDINGS = [Binding('escape', 'back', 'Board'), Binding('B', 'dismiss', 'Board', show=False),
                Binding('slash', 'search', 'Search'), Binding('n', 'more', 'More'), Binding('C', 'comments', 'Comments'),
                Binding('o', 'open', 'Browser'), Binding('s', 'source', 'Markdown source', show=False),
                Binding('r', 'load', 'Reload', show=False), Binding('down', 'list', 'Back to the list', show=False),
                ('question_mark', 'app.keys', 'Keys'), ('q', 'app.quit', 'Quit')]

    def __init__(self, client, board):
        super().__init__()
        self.client, self.board = client, board  # the board as listed: id and name
        self.saved, self.field, self.issues, self.more = None, 'State', [], False
        self.explanation = 'Loading the backlog…'  # the details pane while no issue is selected
        self.rendering = asyncio.Lock()

    def compose(self) -> ComposeResult:
        yield Static(Text.assemble(('Backlog', 'bold'), f' · {plain(self.board["name"])}'), id='backlog-title')
        yield Input(placeholder='Search loaded issues: ID, summary, state or assignee  (/)', id='backlog-search', compact=True)
        yield Static('', id='status')
        yield Issues(id='backlog')
        with VerticalScroll(id='details-pane', can_maximize=True):
            yield Static('', id='details')
        yield Footer(show_command_palette=False)

    def on_mount(self):
        self.query_one('#details-pane').border_title = 'Details  (Enter expands with comments · s source · Esc returns)'
        self.query_one('#backlog').focus()
        self.load()

    def status(self, text, error=False):
        LOGGER.log(logging.ERROR if error else logging.DEBUG, '%s', text)
        status = self.query_one('#status', Static)
        status.update(Text(plain(text)))
        status.set_class(error, 'error')

    def note(self, status, explanation, error=False):
        self.status(status, error)
        self.explanation = explanation
        self.show_details()

    def current(self):
        return getattr(self.query_one('#backlog', ListView).highlighted_child, 'issue', None)

    def matches(self, issue):
        return self.query_one(Input).value.casefold() in f'{search_text(issue)} {names(issue, self.field).casefold()}'

    def action_load(self):
        if self.maximized and self.current():  # r on the expanded details reads its comments again
            self.load_comments(self.current())
        else:
            self.load()

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

    def action_more(self):
        if self.more:
            self.load_more()

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

    @on(Input.Changed, '#backlog-search')
    async def search_changed(self):
        await self.render_rows((self.current() or {}).get('id'))

    @on(Input.Submitted, '#backlog-search')
    def action_list(self):
        self.minimize()
        self.query_one('#backlog').focus()

    async def render_rows(self, keep=None):
        async with self.rendering:  # typing while a page arrives must not mount the rows twice
            shown = [i for i in self.issues if self.matches(i)]
            width = max((len(self.app.ref(i)) for i in shown), default=0)
            listing = self.query_one('#backlog', ListView)
            await listing.clear()
            more = More(f'{self.MORE} · Enter or n loads the next {self.PAGE}')
            await listing.extend([*(self.row(i, width) for i in shown), *([more] if self.more else [])])
            listing.index = next((n for n, i in enumerate(shown) if i['id'] == keep), 0 if len(listing) else None)
            self.show_counts(len(shown))

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

    @on(ListView.Highlighted, '#backlog')
    def show_details(self):
        item, details = self.query_one('#backlog', ListView).highlighted_child, self.query_one('#details', Static)
        if isinstance(item, Row):
            details.update(self.with_comments(item.issue, self.details(item.issue)))
        elif isinstance(item, More):
            details.update(Text(f'Enter or n loads the next {self.PAGE} issues of the backlog.', 'dim'))
        else:
            details.update(Text(self.explanation, 'dim'))

    @on(ListView.Selected, '#backlog')
    def selected(self, event):
        if isinstance(event.item, More):
            self.action_more()
        else:
            pane = self.query_one('#details-pane')
            pane.focus()
            self.maximize(pane)
            self.load_comments(event.item.issue)

    def action_back(self):
        """Escape steps back: from the details or the search to the list, then to the board."""
        if self.maximized or not isinstance(self.focused, ListView):
            self.action_list()
        else:
            self.dismiss()

    def action_search(self):
        self.minimize()
        self.query_one(Input).focus()

    def action_comments(self):
        if self.current():
            self.app.push_screen(self.app.comments_screen(self.current(), writable=False), lambda _: self.show_details())

    def action_open(self):
        if self.current():
            self.app.open_issue(self.current(), self.status)

    def action_source(self):
        self.app.action_source()
        self.show_details()


class BoardApp(InlineComments, App):
    TITLE = 'YouTrack Board'
    SERVICE = 'YouTrack'
    ERROR = YouTrackError  # what the client raises when the service refuses or cannot be reached
    CARD = Card
    REFRESH = 5  # seconds between auto-refreshes of the board on screen
    AUTO_FOCUS = None
    ESCAPE_TO_MINIMIZE = False
    ALLOW_IN_MAXIMIZED_VIEW = 'Footer, LogPanel'
    CSS = '''
    Screen { background: $surface; }
    #toolbar { height: 1; margin: 0 1; }
    #toolbar > * { width: 1fr; margin-right: 1; }
    #toolbar Button { width: auto; min-width: 0; max-width: 24; padding: 0; text-style: none;
                      content-align: left middle; }
    #toolbar Button:focus { background: $block-cursor-background; color: $block-cursor-foreground; }
    #toolbar #auto { width: 7; margin-right: 0; }  /* fits "↻ 60s" and the padding every Button has */
    #toolbar #auto.error { color: $text-error; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    #status.error { color: $text-error; }
    #heads { height: 1; padding: 0 1; display: none; }
    #heads > Static { margin-right: 1; text-style: bold; text-wrap: nowrap; text-overflow: ellipsis; }
    #board { height: 1fr; padding: 0 1; layout: horizontal; scrollbar-size: 1 1; }
    #board.-lanes { layout: vertical; }
    #board > .empty { padding: 1 2; color: $text-muted; }
    #board > .lane { height: 1; margin-top: 1; color: $text-accent; text-wrap: nowrap; text-overflow: ellipsis; }
    #board > .lane:first-child { margin-top: 0; }
    #board > .lane-row { width: auto; height: auto; }
    Column { width: 1fr; height: 1fr; margin-right: 1; scrollbar-size-vertical: 1; border: round $foreground 25%;
             border-title-color: $foreground; border-title-style: bold; }
    Column:focus { border: round $border; }
    Column.-lane { height: auto; min-height: 1; border: none; background: transparent; }
    Column.-lane:focus { border: none; background: $foreground 10%; }
    Column.-lane > Card:last-child { margin-bottom: 0; }
    Card { padding: 0 1; margin-bottom: 1; background: $panel; border-left: tall $panel; }
    Card.-compact { margin-bottom: 0; }
    Card.-compact Static { text-wrap: nowrap; text-overflow: ellipsis; }
    Column > Card.-highlight { background: $panel; color: $foreground; text-style: none; }
    Column:focus > Card.-highlight { background: $block-cursor-background; color: $block-cursor-foreground;
                                     text-style: $block-cursor-text-style; border-left: tall $accent; }
    #details-pane { height: 30%; max-height: 12; border-top: solid $foreground 25%; padding: 0 1; }
    #details-pane:focus { border-top: solid $border; }
    #details-pane.-maximized { height: 1fr; max-height: 100%; }
    ModalScreen { align: center middle; background: $background 70%; }
    #dialog { width: 64; max-width: 96%; height: auto; max-height: 96%; border: round $border; background: $surface;
              padding: 0 1; }
    #dialog Label { margin: 1 0; text-style: bold; }
    #dialog OptionList, #dialog SelectionList { height: auto; max-height: 50vh; margin-top: 1; padding: 0; border: none; }
    #dialog.comments { width: 100; height: 90%; }
    #thread { height: 1fr; scrollbar-size-vertical: 1; }
    #thread > .comment { margin-bottom: 1; padding: 0 1; background: $panel; border-left: tall $accent 50%; }
    #thread > .note { color: $text-muted; }
    BacklogScreen { align: left top; background: $surface; }
    #backlog-title { height: 1; padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #backlog-search { margin: 0 1; }
    #backlog { height: 1fr; margin: 0 1; border: round $foreground 25%; scrollbar-size-vertical: 1; }
    #backlog:focus { border: round $border; }
    #backlog > ListItem { layout: horizontal; height: 1; padding: 0 1; }
    #backlog .id { width: auto; }
    #backlog .summary { width: 1fr; margin: 0 2; text-wrap: nowrap; text-overflow: ellipsis; }
    #backlog .meta { width: auto; max-width: 38%; text-wrap: nowrap; text-overflow: ellipsis; }
    .hint { margin: 1 0; color: $text-muted; }
    .hint.error { color: $text-error; }
    '''
    BINDINGS = [('a', 'assign', 'Assign'), ('m', 'move', 'Move'), ('c', 'comment', 'Comment'),
                Binding('C', 'comments', 'Read comments', show=False),
                ('b', 'pick_board', 'Board'), ('B', 'backlog', 'Backlog'), ('p', 'pick_project', 'Project'), ('v', 'view', 'View'),
                Binding('M', 'mine', 'Only mine', show=False), ('slash', 'filter', 'Filter'),
                Binding('x', 'clear_filters', 'Show all issues', show=False),
                Binding('s', 'source', 'Markdown source', show=False),
                Binding('o', 'open', 'Browser', show=False), Binding('r', 'refresh', 'Refresh', show=False),
                Binding('R', 'auto', 'Auto-refresh', show=False), Binding('g', 'merge_requests', 'Merge requests', show=False),
                Binding('G', 'merge_requests(True)', 'All merge requests', show=False),
                Binding('f12', 'logs', 'Logs', priority=True),
                ('question_mark', 'keys', 'Keys'), ('q', 'quit', 'Quit'),
                Binding('escape,down', 'focus_board', 'Back to board', show=False)]

    def __init__(self, client, prefs=None, connection=None, gitlab=None, cwd=None, verbose=False):
        super().__init__()
        self.client = client
        self.log_buffer = None
        if verbose:
            headers = getattr(getattr(client, 'http', None), 'headers', {})
            self.log_buffer = LogBuffer([headers.get('Authorization', '').removeprefix('Bearer ')])
            LOGGER.addHandler(self.log_buffer)
        self.gitlab, self.gitlabs = gitlab, {}  # host -> GitLab client for merge requests; None: glab's, made on first use
        self.cwd = cwd  # the folder the board started in, whose Git remotes may name a project's repository; None: never looked at
        self.connection = connection  # which account and which token, shown once at start
        self.prefs = prefs or Prefs(None, client.base_url)
        self.boards = []
        self.me = {}
        self.board_id = None
        self.board_data = None
        self.sprint_data = None
        self.sprints = []
        self.issues_data = []
        self.selected = None
        self.busy = False
        self.people = {}  # project id -> (Assignee settings, users), until r
        self.sprint_memory = {}  # board id -> sprint id, for returning to a board in this session
        self.source = False  # show descriptions and comments as typed instead of formatted
        self.rendering = asyncio.Lock()
        self.slowdown = 1  # auto-refresh runs every REFRESH × slowdown seconds; more while YouTrack does not answer
        self.skip = 0  # timer ticks left to wait before the next try

    def compose(self):
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

    def action_logs(self):
        if self.log_buffer is None:
            self.notify('Start with --verbose to collect logs.', title='Logs')
            return
        panel = self.screen_stack[0].query_one(LogPanel)
        if panel.display and panel.has_focus:
            self.action_focus_board()
        panel.display = not panel.display

    def drain_logs(self):
        self.screen_stack[0].query_one(LogPanel).drain(self.log_buffer)

    def notify(self, message, *, severity='information', **options):
        LOGGER.log({'warning': logging.WARNING, 'error': logging.ERROR}.get(severity, logging.INFO), '%s', message)
        return super().notify(message, severity=severity, **options)

    def on_mount(self):
        if self.log_buffer is not None:
            LOGGER.info('%s started · verbose diagnostics enabled · F12 toggles logs', self.TITLE)
            self.set_interval(0.2, self.drain_logs)
        self.query_one('#details-pane').border_title = 'Details  (Enter expands with comments · g merge requests · s source)'
        self.watch(self.query_one('#board'), 'scroll_x', self.follow_board, init=False)
        if self.connection:
            self.notify(self.connection, title=f'{self.SERVICE} account', timeout=8)
        if self.prefs.warning:
            self.notify(self.prefs.warning, severity='warning')
        self.show_auto()
        self.set_interval(self.REFRESH, self.tick)
        self.load_boards()

    def status(self, text, error=False):
        LOGGER.log(logging.ERROR if error else logging.DEBUG, '%s', text)
        status = self.query_one('#status', Static)
        status.update(text)
        status.set_class(error, 'error')

    def save_prefs(self):
        self.prefs.warning = None
        try:
            self.prefs.save()
        except OSError as exc:
            self.prefs.warning = f'Could not save local view state: {exc}'
        if self.prefs.warning:
            self.notify(self.prefs.warning, severity='warning')

    def projects(self):
        return {p['id']: p for b in self.boards for p in b.get('projects') or []}

    def scope(self):
        return self.projects().get(self.prefs.data['project'])

    def view(self):
        return self.prefs.view(self.board_id)

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

    def open_board(self, board_id):
        if board_id != self.board_id or not self.board_data:
            self.load_board(board_id)

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

    def action_refresh(self):
        if self.screen.maximized and self.selected:  # r on the expanded details reads its comments again
            self.load_comments(self.selected)
            return
        self.people.clear()
        self.reload()

    def loaded(self):
        """A board and, if it uses sprints, a sprint are chosen, so its issues can be (re)loaded."""
        return bool(self.board_data) and (bool(self.sprint_data) or not uses_sprints(self.board_data))

    def reload(self):
        if self.busy:
            return
        if self.loaded():
            self.load_issues()
        elif self.board_id:
            self.load_board(self.board_id)
        else:
            self.load_boards()

    @work(exclusive=True, group='load')
    async def load_issues(self):
        await self.fetch_issues()

    async def fetch_issues(self):
        self.status('Loading issues…')
        try:
            self.issues_data = await self.read()
        except self.ERROR as exc:
            self.status(f'{exc} — press r to retry.', error=True)
            return
        self.answered()
        refocus = not self.screen.is_modal and not isinstance(self.focused, Input)
        await self.render_board()
        if refocus:
            self.action_focus_board()

    def may_poll(self):
        """Auto-refresh is on for this board, and a refresh would disturb nothing: no save is running, and no dialog,
        backlog, filter typing or expanded details is in front of the board."""
        return self.view()['auto'] and not (self.busy or self.screen.is_modal or self.screen.maximized
                                            or isinstance(self.focused, Input))

    def tick(self):
        """Every REFRESH seconds. One refresh at a time, never during another load, and less often while they fail."""
        if self.skip:
            self.skip -= 1
        elif self.loaded() and self.may_poll() and not any(w.group == 'load' and not w.is_finished for w in self.workers):
            self.poll()

    @work(exclusive=True, group='load')
    async def poll(self):
        """Read the issues on screen again; rebuild the board only if they changed. Any other load cancels this one, and
        an answer for a board or sprint no longer shown, or arriving under a dialog, is dropped."""
        shown = self.board_data, self.sprint_data
        try:
            issues = await self.read()
        except self.ERROR as exc:
            if self.slowdown == 1:  # once per outage, not on every retry
                self.notify(f'{exc} The board keeps what it showed; retrying less often until {self.SERVICE} answers.',
                            title='Auto-refresh failed', severity='warning', timeout=10)
            self.slowdown = min(self.slowdown * 2, 12)  # 10, 20, 40, then 60 s
            self.skip = self.slowdown - 1
            self.show_auto()
            return
        self.answered()
        if issues != self.issues_data and (self.board_data, self.sprint_data) == shown and self.may_poll():
            self.issues_data = issues
            await self.render_board()

    async def read(self):
        """The issues of the board (and sprint) on screen, as the service has them now."""
        return await self.client.issues(self.board_data, self.sprint_data)

    def answered(self):
        """YouTrack answered: auto-refresh is back to its normal pace."""
        if self.slowdown > 1:
            self.slowdown, self.skip = 1, 0
            self.show_auto()

    def show_auto(self):
        """The toolbar's auto-refresh indicator for this board: its interval, red while it fails, or off."""
        button, on = self.query_one('#auto', Button), self.view()['auto']
        button.label = f'↻ {self.REFRESH * self.slowdown:g}s' if on else '↻ off'
        button.set_class(on and self.slowdown > 1, 'error')

    def action_auto(self):
        if self.board_id:
            auto = not self.view()['auto']
            self.prefs.set_view(self.board_id, auto=auto)
            self.save_prefs()
            self.slowdown, self.skip = 1, 0
            self.show_auto()
            self.notify(f'Auto-refresh on: this board reloads every {self.REFRESH:g} s.' if auto
                        else 'Auto-refresh off for this board. Press r to reload.')
            self.tick()  # turned on: bring the board up to date now

    @on(Input.Changed, '#filter')
    async def filter_changed(self):
        await self.render_board()

    @on(Input.Submitted, '#filter')
    def filter_submitted(self):
        self.action_focus_board()

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

    async def render_board(self):
        """Rebuild the columns. The selected card stays selected, and the board stays scrolled where it was. If the
        focused card is gone, or focus was on an empty cell, focus stays at the same place on the board."""
        async with self.rendering:  # overlapping renders would mount two sets of columns
            wanted = self.selected and self.selected['id']
            board, heads = self.query_one('#board'), self.query_one('#heads')
            before = list(self.query(Column))
            spot = (before.index(self.focused), self.focused.index or 0) if self.focused in before else None
            if spot:  # otherwise removing the focused column moves focus into the filter
                self.screen.set_focus(None)
            columns = []
            with self.batch_update():  # never paint it half rebuilt; an empty board would also lose its scroll position
                await board.remove_children()
                await heads.remove_children()
                lanes = self.board_data and self.lanes(self.visible_issues())
                board.set_class(lanes is not None, '-lanes')
                heads.display = lanes is not None
                if self.board_data:
                    view = self.view()
                    groups = self.groups(self.visible_issues())
                    if lanes is not None:
                        columns = await self.render_lanes(lanes, [(t, c) for t, c in groups if t not in view['hidden']])
                    else:
                        for title, cards in groups:
                            if title not in view['hidden']:
                                columns.append(Column(*(self.CARD(i, view['compact']) for i in cards)))
                                columns[-1].border_title = self.column_label(title, cards)
                        await board.mount_all(columns)
                    if groups and not any(t not in view['hidden'] for t, _ in groups):
                        await board.mount(Static('All columns are hidden. Press v to choose columns.', classes='empty'))
                    self.fit_columns()
                    self.follow_board(board.scroll_x)
                    self.show_counts()
            # An issue can sit in several columns: look in the column that had focus first.
            ranked = ([columns[spot[0]]] if spot and spot[0] < len(columns) else []) + columns
            found = next(((c, n) for c in ranked for n, card in enumerate(c.children) if card.issue['id'] == wanted), None)
            column, index = found or (None, None)
            if spot and not found and columns:  # the card left, or focus was on an empty cell: stay at the same place
                column = columns[min(spot[0], len(columns) - 1)]
                index = min(spot[1], len(column) - 1) if len(column) else None
                if index is None and wanted:  # its column emptied: on to the first card, as when a filter hides it
                    column = None
            if index is not None:
                column.index = index
            self.show_card(column.highlighted_child if column is not None else None)
            if spot and column is not None:
                column.focus()
            elif spot:
                self.action_focus_board()

    async def render_lanes(self, lanes, groups):
        """A row per project swimlane, each split into the same status columns as the headers above."""
        compact, rows = self.view()['compact'], []
        await self.query_one('#heads').mount_all(Static(Text(self.column_label(title, cards))) for title, cards in groups)
        for value, issues in lanes:
            cells = dict(self.groups(issues))
            shown = {i['id'] for title, _ in groups for i in cells.get(title, [])}
            if not shown:  # an empty lane would only push the others down
                continue
            rows.append([Column(*(self.CARD(i, compact) for i in cells.get(title, [])), classes='-lane') for title, _ in groups])
            await self.query_one('#board').mount_all([Static(self.lane_label(value, len(shown)), classes='lane'),
                                                      Horizontal(*rows[-1], classes='lane-row')])
        if groups and not rows:
            reasons = ', '.join(self.filters())
            await self.query_one('#board').mount(Static(
                f'No cards match {reasons}. Press x to show all issues.' if reasons else 'No cards to show.', classes='empty'))
        return [cell for row in rows for cell in row]

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

    def column_label(self, title, cards):
        return f'{title} · {len(cards)}'

    def on_resize(self):
        self.fit_columns()

    def fit_columns(self):
        board, columns = self.query_one('#board'), self.query(Column)
        if board.has_class('-lanes'):  # fixed widths, so every lane lines up with the headers
            heads = self.query('#heads > Static')
            width = max(24, (board.size.width - 1) // max(1, len(heads)) - 1)
            for widget in [*heads, *columns]:
                widget.styles.width = width
            return
        # Overflowing 1fr columns in a scroll container each grow to the full width, so use a fixed width then.
        fit = board.size.width >= 25 * len(columns)
        for column in columns:
            column.styles.width = '1fr' if fit else 26

    def follow_board(self, x):
        """Keep the column headers and lane names in view while the lanes scroll sideways."""
        for head in self.query('#heads > Static'):
            head.styles.offset = (-round(x), 0)
        for label in self.query('#board > .lane'):
            label.styles.offset = (round(x), 0)

    def reveal(self, column):
        """Lane cells grow with their cards, so the board scrolls to the card, and to the lane name above a first card."""
        if not column.has_class('-lane'):
            return
        card, board = column.highlighted_child, self.query_one('#board')

        def scroll():
            if not column.is_attached:
                return
            board.scroll_to_widget(card if card and card.is_attached else column, animate=False, immediate=True)
            label = board.children[board.children.index(column.parent) - 1]
            if not column.index and label.virtual_region.y < board.scroll_y:  # only vertically: it spans the view
                board.scroll_to(y=label.virtual_region.y, animate=False, immediate=True)
        self.call_after_refresh(scroll)

    def action_focus_board(self):
        """Focus the selected card, or the first card if it is gone."""
        self.screen.minimize()
        columns = list(self.query(Column))
        column = next((c for c in columns if self.selected and getattr(c.highlighted_child, 'issue', None) is self.selected), None)
        column = column or next((c for c in columns if c.children), None) or next(iter(columns), None)
        if column:
            column.focus()

    def action_column(self, step):
        """The previous or next status column, in the same swimlane."""
        if isinstance(self.focused, Column):
            cells = [c for c in self.focused.parent.children if isinstance(c, Column)]
            # At once, unlike focus(): the next key may arrive before a deferred focus lands.
            self.screen.set_focus(cells[max(0, min(len(cells) - 1, cells.index(self.focused) + step))])

    def action_lane(self, step):
        """Past a lane's first or last card: the same status column in the previous or next lane."""
        rows = list(self.query('#board > .lane-row'))
        cell = self.focused
        if isinstance(cell, Column) and cell.parent in rows and 0 <= rows.index(cell.parent) + step < len(rows):
            target = rows[rows.index(cell.parent) + step].children[cell.parent.children.index(cell)]
            target.index = 0 if step > 0 else len(target) - 1
            self.screen.set_focus(target)

    def on_descendant_focus(self, event: events.DescendantFocus):
        if isinstance(event.widget, Column) and event.widget.is_attached:  # focus events can arrive while quitting
            self.show_card(event.widget.highlighted_child)
            self.reveal(event.widget)

    def show_card(self, card):
        self.selected = card.issue if isinstance(card, Card) else None
        self.show_details()

    def show_details(self):
        details = self.query_one('#details', Static)
        if self.selected:
            details.update(self.with_comments(self.selected, self.details(self.selected)))
        else:
            details.update('No card selected.')

    def ref(self, issue):
        return issue['idReadable']

    def details(self, issue):
        return issue_details(issue, column_field(self.board_data), self.source)

    def action_source(self):
        self.source = not self.source
        self.show_details()

    def action_details(self):
        if self.selected:
            pane = self.query_one('#details-pane')
            pane.focus()
            self.screen.maximize(pane)
            self.load_comments(self.selected)

    def action_filter(self):
        self.screen.minimize()
        self.query_one('#filter', Input).focus()

    def action_keys(self):
        if self.screen.query('HelpPanel'):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()

    def action_open(self):
        if self.selected:
            self.open_issue(self.selected, self.status)

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

    def backlog_closed(self, _):
        self.show_details()  # s in the backlog switches the board's details too
        if not self.focused:  # the board was re-rendered meanwhile
            self.action_focus_board()

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

    async def action_mine(self):
        if self.board_data:
            mine = not self.view()['mine']
            self.prefs.set_view(self.board_id, mine=mine)
            self.save_prefs()
            await self.render_board()
            self.action_focus_board()
            self.notify('Showing only issues assigned to you.' if mine else "Showing everyone's issues.")

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

    async def save_comment(self, issue, text):
        await self.client.comment(issue, text)
        if self.thread and self.thread['id'] == issue['id']:  # the details show its comments: show the new one too
            self.load_comments(issue)

    def comments_screen(self, issue, writable=True):
        return CommentsScreen(self.client, issue, writable)

    async def read_comments(self, issue):
        return await self.client.comments(issue)

    def comment_view(self, comment):
        return comment_text(comment, self.source)

    def action_comments(self):
        if self.selected:
            self.push_screen(self.comments_screen(self.selected), self.comments_closed)

    def action_merge_requests(self, everything=False):
        """g: open GitLab merge requests naming the selected story; G: every open one of the repository of its project
        (or of the project in scope, or the board's only project). The repository is chosen per project the first time."""
        story = None if everything else self.selected
        project = (self.selected or {}).get('project') or (everything and self.context_project())
        if not project:
            if everything:
                self.status('Select a story, or choose a project with p: G lists the open merge requests of its GitLab '
                            'repository.', error=True)
            return
        url = self.prefs.data['repos'].get(project.get('id'))
        if url:
            self.open_merge_requests(project, story, url)
        elif self.cwd and (self.gitlab or shutil.which('glab')):
            self.find_repository(project, story)
        else:
            self.ask_repository(project, story)

    def context_project(self):
        board = self.board_data or {}
        return self.scope() or (board['projects'][0] if len(board.get('projects') or []) == 1 else None)

    @work(exclusive=True, group='repository')
    async def find_repository(self, project, story):
        """A project's first g inside a Git checkout: its GitLab remote, without asking when there is one. Like a typed
        URL, it is saved only once GitLab shows it to you. A repository already linked to another project, or several
        remotes, are asked about instead: one checkout says nothing about which YouTrack project it belongs to."""
        from gitlab_board import remotes
        from gitlab_board.glab import GlabError
        self.status('Looking for the GitLab project of this checkout…')
        try:
            found, _ = await asyncio.to_thread(remotes.find, self.cwd)
        except remotes.NoRemote:
            self.show_counts()
            self.ask_repository(project, story)
            return
        except GlabError as exc:
            self.status(f'{exc} Nothing was saved; press g to try again.', error=True)
            return
        self.show_counts()
        urls = {f'https://{r.host}/{r.path}': r for r in found}
        if len(urls) == 1:
            url, remote = next(iter(urls.items()))
            taken = [pid for pid, saved in self.prefs.data['repos'].items() if saved.casefold() == url.casefold()]
            if taken:
                others = ', '.join((self.projects().get(pid) or {}).get('shortName', pid) for pid in taken)
                self.notify(f'This checkout’s repository is already linked to {others}. Confirm it for '
                            f'{project.get("shortName")} too, or enter another.', markup=False)
                self.ask_repository(project, story, url)
            else:
                self.open_merge_requests(project, story, url, check=True, remote=remote.names)
            return
        ask = '\x00ask'
        choices = [(url, Text.assemble(f'{r.host}/{r.path}', (f'  {r.names}', 'dim')), f'{url} {r.names}'.casefold(), False)
                   for url, r in urls.items()]
        choices.append((ask, Text('Another repository URL…', 'italic'), None, False))
        self.push_screen(Picker(f'GitLab repository of {project.get("shortName")} stories · this checkout has several',
                                choices), lambda url: self.ask_repository(project, story) if url == ask else url and
                         self.open_merge_requests(project, story, url, check=True, remote=urls[url].names))

    def ask_repository(self, project, story, current=''):
        if self.gitlab is None and not shutil.which('glab'):
            self.status('Merge requests come from GitLab through its CLI. Install it (brew install glab), sign in with '
                        'glab auth login --hostname <your GitLab host>, then press g again.', error=True)
            return
        from gitlab_board.merge_requests import repository
        from gitlab_board.target import TargetError

        def chosen(text):
            if text:
                try:
                    host, path = repository(text)
                except TargetError as exc:
                    self.status(f'Not a GitLab project URL: {exc} Nothing was saved; press g to try again.', error=True)
                    return
                self.open_merge_requests(project, story, f'https://{host}/{path}', check=True)
        self.push_screen(CommentDialog(f'GitLab repository of {project.get("shortName")} stories',
                                       'https://gitlab.example.com/group/project', current), chosen)

    def open_merge_requests(self, project, story, url, check=False, remote=None):
        """The panel for a repository: the MRs naming the story, or all open ones without one. A new repository (check)
        is saved for the project once GitLab shows it to you. remote: the checkout's Git remote it was found in."""
        from gitlab_board.merge_requests import MergeRequestsScreen, repository
        from gitlab_board.target import TargetError
        try:
            host, path = repository(url)
        except TargetError:  # a hand-edited state file: ask again
            self.ask_repository(project, story, url)
            return
        key = story and story['idReadable']

        async def load():
            gitlab = self.gitlab_client(host)
            if check:
                screen.home = (await gitlab.place(path, 'project'))['path']
                self.prefs.data['repos'][project['id']] = f'https://{host}/{screen.home}'
                self.save_prefs()
                found = f', the {remote} remote of this checkout' if remote else ''
                self.notify(f'{project.get("shortName")} merge requests now come from {host}/{screen.home}{found}. '
                            'Press u in the panel to change it.', markup=False)
            if key:
                return await gitlab.project_merge_requests(screen.home, key)
            found = await gitlab.open_merge_requests({'kind': 'project', 'path': screen.home})
            for mr in found['mrs']:
                named = self.named_stories(mr, host, screen.home)
                mr['why'] = 'Names ' + '; '.join(f'{k} in its {where}' for k, where in named.items()) if named else \
                    'Names no story of a project linked to this repository'
            return found

        async def links(mr):
            return [{'idReadable': k, 'ref': k, 'why': f'named in its {where}',
                     'title': next((i['summary'] for i in self.issues_data if i['idReadable'] == k), '')}
                    for k, where in self.named_stories(mr, host, screen.home).items()]
        if key:
            screen = MergeRequestsScreen(f'Open merge requests · {key} {plain(story["summary"])}',
                                         f'In {host}/{path}, naming {key} in title, branch or description',
                                         load, host, path, repo=True, links=links)
        else:
            screen = MergeRequestsScreen(f'All open merge requests · {host}/{path}',
                                         f'Most recently updated first · i opens a {project.get("shortName")} story an MR '
                                         'names', load, host, path, repo=True, links=links,
                                         empty='No open merge request in this repository.')
        self.push_screen(screen, lambda choice: self.ask_repository(project, story, url) if choice == 'repo' else
                         self.merge_requests_closed(choice))

    def named_stories(self, mr, host, path):
        """{story ID: where} for stories an MR names, of the YouTrack projects whose repository this is: the link g
        follows. Prefixes of other projects are never matched."""
        from gitlab_board.client import named
        repo = f'https://{host}/{path}'.casefold()
        prefixes = [p['shortName'] for pid, p in self.projects().items()
                    if (self.prefs.data['repos'].get(pid) or '').casefold() == repo]
        return named(prefixes, mr)

    def merge_requests_closed(self, choice):
        """Back from the merge requests: on to an issue an MR links, if one was chosen (i), else to the card."""
        if isinstance(choice, tuple):
            self.call_after_refresh(self.show_linked, choice[1])  # after the board has its focus back
        elif not self.focused:
            self.action_focus_board()

    def show_linked(self, issue):
        """The card of an issue an MR links, its details expanded with its comments, if the board shows it; else its
        page in the browser."""
        for column in self.query(Column):
            for n, card in enumerate(column.children):
                if self.same(card.issue, issue):
                    self.screen.set_focus(column)
                    column.index = n
                    self.show_card(card)  # now: the focus event that would select it comes later
                    self.action_details()
                    return
        self.action_focus_board()
        self.notify(f'{issue["ref"]} is not shown on this board; opening it in your browser.', markup=False)
        self.open_issue(issue, self.status)

    def same(self, issue, other):
        return issue['idReadable'] == other['idReadable']

    def gitlab_client(self, host):
        if host not in self.gitlabs:
            from gitlab_board.client import Client as GitLab  # only once g is used: the board never needs glab
            self.gitlabs[host] = (self.gitlab or GitLab)(host)
        return self.gitlabs[host]

    def comments_closed(self, choice):
        if not self.focused:  # the board was re-rendered meanwhile, so the card that had focus is gone
            self.action_focus_board()
        if choice == 'write':
            self.action_comment()

    @work(group='write')
    async def write(self, issue, save, done, reload=True):
        if self.busy:
            return
        self.busy = True
        self.query('#toolbar Select, #toolbar Button').set(disabled=True)
        self.status(f'Saving {self.ref(issue)}…')
        try:
            done = await save() or done
        except self.ERROR as exc:
            self.status(str(exc) + ' If the connection failed while saving, refresh before retrying.', error=True)
            return
        finally:
            self.busy = False
            self.query('#toolbar Select, #toolbar Button').set(disabled=False)
        self.notify(done)
        self.show_counts()
        if reload:
            self.reload()

    async def on_unmount(self):
        if self.log_buffer is not None:
            LOGGER.removeHandler(self.log_buffer)
            self.log_buffer.close()
        await self.client.close()


def main():
    parser = argparse.ArgumentParser(
        description='A small YouTrack Agile board TUI',
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
    args = parser.parse_args()
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
        sys.exit(f'youtrack-board: {exc}')
    BoardApp(client, Prefs(default_path(), client.base_url), connection, cwd=os.curdir, verbose=args.verbose).run()


if __name__ == '__main__':
    main()
