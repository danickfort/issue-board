"""Terminal-safe text, dialogs and issue-list navigation shared by both trackers."""
import asyncio
import logging
import time

from rich.console import Group
from rich.rule import Rule
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.fuzzy import Matcher
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, ListItem, ListView, OptionList, SelectionList, Static
from textual.widgets.option_list import Option

from .diagnostics import LOGGER

HINT = '  ·  ←/→ column  ↑/↓ card  Enter details  C comments  ? keys'
ALL = '*'
# Keep tab and newline, but strip terminal control characters from tracker text.
CONTROL = dict.fromkeys([*range(9), *range(11, 32), *range(127, 160)])


def plain(value):
    return (value or '').translate(CONTROL)


def ago(timestamp):
    minutes = int((time.time() - timestamp) / 60)
    return ('just now' if minutes < 1 else f'{minutes} min ago' if minutes < 60
            else f'{minutes // 60} h ago' if minutes < 1440 else f'{minutes // 1440} d ago')


class Picker(ModalScreen):
    """Type to narrow the list, ↑/↓ to choose, Enter to pick. Dismisses with the chosen value, or None on Escape."""
    BINDINGS = [Binding('escape', 'dismiss', 'Cancel'),
                Binding('down', 'cursor("cursor_down")', show=False), Binding('up', 'cursor("cursor_up")', show=False),
                Binding('pagedown', 'cursor("page_down")', show=False), Binding('pageup', 'cursor("page_up")', show=False)]

    def __init__(self, title, choices=None, value=None, *, fuzzy=False):
        """Each choice is (value, label, search text or None to always show it, disabled). None means still loading."""
        super().__init__()
        self.prompt, self.choices, self.value, self.shown = title, choices, value, []
        self.fuzzy = fuzzy
        self.placeholder, self.error = ('Loading…' if choices is None else
                                        'Type to fuzzy search' if fuzzy else 'Type to search'), None

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
        query = self.query_one(Input).value.strip()
        if self.fuzzy and query:
            matcher = Matcher(query)
            matches = [(1 if c[2] is None else matcher.match(c[2]), c) for c in self.choices or []]
            self.shown = [c for score, c in sorted(matches, key=lambda item: item[0], reverse=True) if score > 0]
        else:
            words = query.casefold().split()
            self.shown = [c for c in self.choices or [] if c[2] is None or all(w in c[2] for w in words)]
        options = self.query_one(OptionList)
        options.set_options(Option(label, disabled=disabled) for _, label, _, disabled in self.shown)
        if self.choices is not None and not self.shown:
            options.add_option(Option(Text('No matches', 'dim'), disabled=True))
        enabled = [n for n, choice in enumerate(self.shown) if not choice[3]]
        options.highlighted = next((n for n in enabled if self.shown[n][0] == self.value and not (self.fuzzy and query)),
                                   enabled[0] if enabled else None)

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


def link_label(link):
    """A linked issue on one line: ↑ for a parent, how it is linked, its reference and title, and whether it is done."""
    return Text.assemble('↑ ' if link['parent'] else '  ', (plain(link['why']), 'dim'), ' ', (plain(link['ref']), 'bold'),
                         ' ', plain(link['title']), (' · done' if link['done'] else '', 'dim'), no_wrap=True,
                         overflow='ellipsis')


def links_view(thread):
    """Above the details: the issues this one is linked to, parents first, or why they could not be read."""
    if thread['links_error']:
        return [Text(plain(f'Could not load the linked issues. {thread["links_error"]} Press r to retry.'), 'red'), Text('')]
    if not thread['links']:
        return []
    keys = ' · u parent' if any(link['parent'] for link in thread['links']) else ''
    return [Rule(Text.assemble(('Linked issues', 'bold'), (f'{keys} · i opens one', 'dim')), align='left', style='dim'),
            *map(link_label, thread['links']), Text('')]


class InlineComments:
    """The selected issue's comments under its details and the issues it is linked to above them, read when the details
    expand (Enter) and on r there, never while the cursor moves. An answer only shows while its issue is selected; a
    newer read cancels an older one. u opens the issue's parent and i any issue linked to it, each in an IssueScreen.

    Each tracker's read_links returns its links parents first, as {'ref', 'title', 'why' (how it is linked, as the
    tracker words it), 'parent', 'done'}, plus what its read_issue and open_issue need to find that issue."""
    thread = None  # {'id': issue id, 'comments' and 'links': list or None, 'error' and 'links_error': text or None,
    #                 'loading': bool}

    def with_comments(self, issue, details):
        thread = self.thread if self.thread and self.thread['id'] == issue['id'] else None
        if not thread:
            return details
        comments, parts = thread['comments'], [*links_view(thread), details, Text('')]
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
        self.thread = {'id': issue['id'], 'comments': old.get('comments'), 'links': old.get('links'), 'error': None,
                       'links_error': None, 'loading': True}
        self.show_details()
        found = await asyncio.gather(self.app.read_comments(issue), self.app.read_links(issue), return_exceptions=True)
        thread = {**self.thread, 'loading': False}
        for answer, key, error, name in zip(found, ('comments', 'links'), ('error', 'links_error'),
                                            ('comments', 'linked issues')):
            if isinstance(answer, self.app.ERROR):  # one failing still shows the other
                LOGGER.error('Could not load %s: %s', name, answer)
                thread[error] = str(answer)
            elif isinstance(answer, BaseException):
                raise answer
            else:
                thread[key] = answer
        self.thread = thread
        self.show_details()

    def action_parent(self):
        self.follow(True)

    def action_linked(self):
        self.follow(False)

    @work(exclusive=True, group='links')
    async def follow(self, parents):
        """u: the issue's parent; i: any issue linked to it. One opens at once, several are offered first. Links read
        with the comments are used, else they are read now."""
        issue = self.current()
        if not issue:
            return
        ref = self.app.ref(issue)
        links = (self.thread if self.thread and self.thread['id'] == issue['id'] else {}).get('links')
        if links is None:
            try:
                links = await self.app.read_links(issue)
            except self.app.ERROR as exc:
                self.notify(f'Could not read the issues {ref} is linked to. {exc}', severity='error', markup=False)
                return
        found = [link for link in links if link['parent'] or not parents]
        if not found:
            self.notify(f'{ref} has no parent issue.' if parents else f'{ref} is linked to no issue you can see.',
                        markup=False)
        elif len(found) == 1:
            self.app.open_linked(found[0], ref)
        else:
            choices = [(n, link_label(link), f'{link["ref"]} {link["title"]}'.casefold(), False)
                       for n, link in enumerate(found)]
            self.app.push_screen(Picker(f'{"Parents of" if parents else "Issues linked to"} {ref} · Enter opens one',
                                        choices), lambda n: n is not None and self.app.open_linked(found[n], ref))


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
    def __init__(self, issue, field, id_width):
        super().__init__()
        self.issue, self.field, self.id_width = issue, field, id_width


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


class CommentsScreen(ModalScreen):
    """One issue's comments, newest first, loaded when opened. Closing it drops an answer that is still on its way."""

    BINDINGS = [Binding('escape,C', 'dismiss', 'Close'), Binding('r', 'load', 'Reload'),
                Binding('c', 'dismiss("write")', 'Write a comment'), Binding('s', 'source', 'Markdown source')]

    def __init__(self, client, issue, writable=True):
        super().__init__()
        self.client, self.issue, self.comments, self.writable = client, issue, [], writable

    def check_action(self, action, parameters):
        return self.writable or parameters != ('write',)  # from the backlog, which only reads

    def on_mount(self):
        self.query_one('#thread').focus()
        self.load()

    def action_load(self):
        self.load()

    def action_source(self):
        self.app.action_source()
        for widget, comment in zip(self.query('#thread > .comment'), self.comments):
            widget.update(self.app.comment_view(comment))

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
            comments = await self.app.read_comments(self.issue)
        except self.app.ERROR as exc:
            await self.show([Static(Text(plain(f'Could not load the comments. {exc}')), classes='note')], 'r retry', error=True)
            return
        self.comments = comments
        if not comments:
            await self.show([Static('No comments yet, or none you can see.', classes='note')],
                            ('c write one  ·  ' if self.writable else '') + 'r reload')
            return
        count = f'{len(comments)} comment{"s" if len(comments) > 1 else ""}, newest first'
        await self.show([Static(self.app.comment_view(c), classes='comment') for c in comments],
                        f'{count}  ·  ↑/↓ scroll  ·  {"c write  ·  " if self.writable else ""}s source  ·  r reload')


class BacklogScreen(InlineComments, ModalScreen):
    """Shared paged issue list; tracker screens supply fetching, filtering, rows and counts."""

    PAGE = 50

    SEARCH = 'Search loaded issues  (/)'

    BINDINGS = [Binding('escape', 'back', 'Board'), Binding('B', 'dismiss', 'Board', show=False),
                Binding('slash', 'search', 'Search'), Binding('n', 'more', 'More'), Binding('C', 'comments', 'Comments'),
                Binding('o', 'open', 'Browser'), Binding('s', 'source', 'Markdown source', show=False),
                Binding('u', 'parent', 'Parent issue', show=False), Binding('i', 'linked', 'Linked issues', show=False),
                Binding('r', 'load', 'Reload', show=False), Binding('down', 'list', 'Back to the list', show=False),
                ('question_mark', 'app.keys', 'Keys'), ('q', 'app.quit', 'Quit')]

    def __init__(self, client, board):
        super().__init__()
        self.client, self.board = client, board  # the board as listed: id and name
        self.issues, self.more = [], False
        self.explanation = 'Loading…'  # the details pane while no issue is selected
        self.rendering = asyncio.Lock()

    def compose(self) -> ComposeResult:
        yield Static(Text.assemble(('Backlog', 'bold'), f' · {plain(self.board["name"])}'), id='backlog-title')
        yield Input(placeholder=self.SEARCH, id='backlog-search', compact=True)
        yield Static('', id='status')
        yield Issues(id='backlog')
        with VerticalScroll(id='details-pane', can_maximize=True):
            yield Static('', id='details')
        yield Footer(show_command_palette=False)

    def on_mount(self):
        self.query_one('#details-pane').border_title = 'Details  (Enter expands with comments · u parent · i linked · s source · Esc returns)'
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

    def action_load(self):
        if self.maximized and self.current():  # r on the expanded details reads its comments again
            self.load_comments(self.current())
        else:
            self.load()

    def action_more(self):
        if self.more:
            self.load_more()

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


class IssueScreen(InlineComments, ModalScreen):
    """A linked issue over the view it was opened from, read when it opens, with its comments and its own links: u and
    i open its parent and linked issues over it, and Esc goes back. It only reads, as the backlog: board keys are inert."""

    BINDINGS = [Binding('escape', 'dismiss', 'Back'), Binding('u', 'parent', 'Parent'), Binding('i', 'linked', 'Linked'),
                Binding('C', 'comments', 'Comments'), Binding('o', 'open', 'Browser'),
                Binding('s', 'source', 'Markdown source', show=False), Binding('r', 'load', 'Reload', show=False),
                ('question_mark', 'app.keys', 'Keys'), ('q', 'app.quit', 'Quit')]

    def __init__(self, link, back):
        """link: the issue as read_links names it; back: the reference of the issue Esc returns to."""
        super().__init__()
        self.link, self.back, self.issue = link, back, None
        self.note = Text(plain(f'Loading {link["ref"]} {link["title"]}…'), 'dim')  # until the issue is read

    def compose(self) -> ComposeResult:
        with VerticalScroll(id='details-pane'):
            yield Static('', id='details')
        yield Footer(show_command_palette=False)

    def on_mount(self):
        pane = self.query_one('#details-pane')
        pane.border_title = Text(plain(f'{self.link["ref"]}  ·  Esc back to {self.back}  ·  u parent · i linked'))
        pane.focus()
        self.show_details()
        self.load()

    def current(self):
        return self.issue

    def show_details(self):
        self.query_one('#details', Static).update(
            self.with_comments(self.issue, self.app.details(self.issue)) if self.issue else self.note)

    @work(exclusive=True, group='issue')
    async def load(self):
        try:
            self.issue = await self.app.read_issue(self.link)
        except self.app.ERROR as exc:
            LOGGER.error('Could not load %s: %s', self.link['ref'], exc)
            self.note = Text(plain(f'Could not load {self.link["ref"]}. {exc} Press r to retry, or o to open it in a '
                                   'browser.'), 'red')
            self.show_details()
            return
        self.load_comments(self.issue)

    def action_load(self):
        """r: the comments and links again once the issue is read, else the issue."""
        if self.issue:
            self.load_comments(self.issue)
        else:
            self.load()

    def action_comments(self):
        if self.issue:
            self.app.push_screen(self.app.comments_screen(self.issue, writable=False), lambda _: self.show_details())

    def action_open(self):
        self.app.open_issue(self.issue or self.link, self.status)

    def action_source(self):
        self.app.action_source()
        self.show_details()

    def status(self, text, error=False):
        self.notify(text, severity='error' if error else 'information', markup=False)
