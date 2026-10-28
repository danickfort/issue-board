"""Shared board rendering, navigation, refresh and write lifecycle.

Tracker apps supply their own data queries, grouping, issue renderers and mutation actions. Issue dictionaries stay
in their tracker-native shape; this module only uses their stable `id` for selection and comments.
"""
import asyncio
import logging
import os
from urllib.parse import urlsplit

from rich.text import Text
from textual import events, on, work
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, Input, Static

from .diagnostics import LOGGER, LogBuffer, LogPanel
from .widgets import Card, Column, InlineComments, IssueActions, IssueScreen, Picker, plain


class BoardView(InlineComments, App):
    REFRESH = 5  # seconds between auto-refreshes of the board on screen

    AUTO_FOCUS = None

    ESCAPE_TO_MINIMIZE = False

    DETAILS = 'Details · Enter expands'  # the details pane's title

    ALLOW_IN_MAXIMIZED_VIEW = 'Footer, LogPanel'

    CSS = '''
    Screen { background: $surface; }
    #identity { height: 1; margin: 0 1; }
    #identity Button { width: auto; min-width: 0; max-width: 36; margin-right: 1; padding: 0; text-style: bold;
                       content-align: left middle; }
    #identity Button:focus { background: $block-cursor-background; color: $block-cursor-foreground; }
    #who { width: 1fr; color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis; }
    #toolbar { layout: grid; grid-size: 4; grid-columns: 21 1fr 1fr 8; grid-rows: 2 1;
               grid-gutter: 0 1; height: 3; margin: 0 1; }
    #toolbar > * { width: 1fr; }
    #toolbar #filter { column-span: 4; }
    #toolbar Button { width: 1fr; height: 2; min-width: 0; padding: 0; text-style: none;
                      content-align: left middle; }
    #toolbar Button:focus { background: $block-cursor-background; color: $block-cursor-foreground; }
    #toolbar #auto { width: 7; height: 1; margin-right: 0; }  /* fits "↻ 60s" and the padding every Button has */
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
    #details-pane { height: 30%; min-height: 7; max-height: 12; border-top: solid $foreground 25%; padding: 0 1; }
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
    BacklogScreen, IssueScreen { align: left top; background: $surface; }
    IssueScreen #details-pane { height: 1fr; max-height: 100%; }
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

    BINDINGS = [Binding('a', 'assign', 'Assign', show=False), Binding('m', 'move', 'Move', show=False),
                Binding('c', 'comment', 'Comment', show=False),
                Binding('C', 'comments', 'Read comments', show=False),
                Binding('b', 'pick_board', 'Board', show=False), ('B', 'backlog', 'Backlog'),
                Binding('p', 'pick_project', 'Project scope', show=False), ('v', 'view', 'View'),
                Binding('M', 'mine', 'Only mine', show=False), ('slash', 'filter', 'Filter'),
                Binding('x', 'clear_filters', 'Show all issues', show=False),
                Binding('s', 'source', 'Markdown source', show=False),
                Binding('o', 'open', 'Browser', show=False), Binding('r', 'refresh', 'Refresh', show=False),
                Binding('R', 'auto', 'Auto-refresh', show=False), Binding('g', 'merge_requests', 'Merge requests', show=False),
                Binding('G', 'merge_requests(True)', 'All merge requests', show=False),
                Binding('u', 'parent', 'Parent issue', show=False), Binding('i', 'linked', 'Linked issues', show=False),
                Binding('P', 'places', 'Switch folder', show=False), Binding('f12', 'logs', 'Logs', priority=True),
                ('question_mark', 'keys', 'All shortcuts'), ('q', 'quit', 'Quit'),
                Binding('escape,down', 'focus_board', 'Back to board', show=False)]

    def __init__(self, client, prefs, connection=None, verbose=False):
        super().__init__()
        self.client, self.prefs, self.connection = client, prefs, connection
        self.log_buffer = None
        if verbose:
            headers = getattr(getattr(client, 'http', None), 'headers', {})
            self.log_buffer = LogBuffer([headers.get('Authorization', '').removeprefix('Bearer ')])
            LOGGER.addHandler(self.log_buffer)
        self.boards, self.me = [], {}
        self.board_id = self.board_data = self.selected = None
        self.issues_data = []
        self.busy = False
        self.people = {}
        self.source = False
        self.rendering = asyncio.Lock()
        self.slowdown, self.skip = 1, 0
        # Set by issueboard before it runs the board: the saved folder on screen, a function giving the Picker choices
        # of every saved folder (P), and a notice to show once, such as why switching failed. None when run alone.
        self.folder = self.places = self.notice = None
        self.drop_drafts = False  # P was pressed once while Jira keeps an unsent comment

    def identity(self):
        """The connection strip, composed above the toolbar: the place (P) when launched by issueboard, then which
        tracker, account and server this board talks to."""
        with Horizontal(id='identity'):
            if self.places is not None:
                yield Button(Text(f'⌂ Folder (Shift+P): {plain(os.path.basename(self.folder))} ▾'), id='places', compact=True, action='app.places',
                             tooltip=f'{self.folder} · switch saved folder (Shift+P)')
            yield Static('', id='who')

    def board_kind(self):
        return ''  # Jira names Scrum or Kanban

    def show_identity(self, failed=False):
        """Tracker, account and server, as the server answered: never guessed from where the token came from."""
        me = self.me or {}
        name = plain(me.get('fullName') or me.get('name') or me.get('displayName'))
        login = plain(me.get('login') or (me.get('username') and '@' + me['username']) or me.get('emailAddress'))
        account = f'{name} ({login})' if name and login and name != login else name or login or \
            ('account unknown' if failed else 'signing in…')
        tracker = ' '.join(filter(None, [self.SERVICE, self.board_kind()]))
        self.query_one('#who', Static).update(Text.assemble((tracker, 'bold'), f' · {account} · ',
                                                            urlsplit(self.client.base_url).netloc))

    def check_action(self, action, parameters):
        return self.places is not None if action == 'places' else super().check_action(action, parameters)

    def action_places(self):
        """P: another saved folder's board, tracker and account. Never while a change is saving, a dialog is open or
        Jira keeps a comment that failed to send: the board exits for issueboard to open the other place."""
        if self.busy or self.screen.is_modal:
            self.notify('Finish or cancel what is open first, then press P to switch place.', severity='warning')
            return
        drafts = getattr(self, 'drafts', None)
        if drafts and not self.drop_drafts:
            self.drop_drafts = True
            self.notify(f'A comment on {", ".join(drafts)} was not sent and is kept only here: press c to send it, or '
                        'P again to switch and drop it.', severity='warning', timeout=10)
            return
        try:
            choices = self.places(self.folder)
        except SystemExit as exc:  # the folder list is unreadable: say why, keep this board
            self.notify(str(exc), severity='error', timeout=10)
            return
        self.push_screen(Picker('Folders · saved issueboard connections', choices, self.folder),
                         lambda folder: folder and folder != self.folder and self.exit(folder))
    def refresh_context(self):
        """Identify the board whose read result is still safe to display."""
        return self.board_data

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
        self.query_one('#details-pane').border_title = self.DETAILS
        self.show_identity()
        if self.notice:
            self.notify(self.notice, title='Place not switched', severity='error', timeout=15)
            self.notice = None
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
        self.show_identity(failed=error)  # every load step reports here, including the account and the board type
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

    def view(self):
        return self.prefs.view(self.board_id)

    def open_board(self, board_id):
        if board_id != self.board_id or not self.board_data:
            self.load_board(board_id)

    def action_refresh(self):
        if self.screen.maximized and self.selected:  # r on the expanded details reads its comments again
            self.load_comments(self.selected)
            return
        self.people.clear()
        self.reload()

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
        shown = self.refresh_context()
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
        if issues != self.issues_data and self.refresh_context() == shown and self.may_poll():
            self.issues_data = issues
            await self.render_board()

    def answered(self):
        """The tracker answered: auto-refresh is back to its normal pace."""
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
        """A row per swimlane, each split into the same status columns as the headers above."""
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
            if not column.is_attached or (card and board.region.contains_region(card.region)):
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
        self.query_one(IssueActions).sync(self.selected, self.thread)
        details = self.query_one('#details', Static)
        if self.selected:
            details.update(self.with_comments(self.selected, self.details(self.selected)))
        else:
            details.update('No card selected.')

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

    def backlog_closed(self, _):
        self.show_details()  # s in the backlog switches the board's details too
        if not self.focused:  # the board was re-rendered meanwhile
            self.action_focus_board()

    async def action_mine(self):
        if self.board_data:
            mine = not self.view()['mine']
            self.prefs.set_view(self.board_id, mine=mine)
            self.save_prefs()
            await self.render_board()
            self.action_focus_board()
            self.notify('Showing only issues assigned to you.' if mine else "Showing everyone's issues.")

    async def save_comment(self, issue, text):
        await self.client.comment(issue, text)
        if self.thread and self.thread['id'] == issue['id']:  # the details show its comments: show the new one too
            self.load_comments(issue)

    def action_comments(self):
        if self.selected:
            self.push_screen(self.comments_screen(self.selected), self.comments_closed)

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

    def current(self):
        return self.selected

    def open_linked(self, link, back):
        """A linked issue in a read-only view over the current one, Esc back to `back`; in the browser if the board
        cannot show it (a GitLab epic, flagged 'web')."""
        if link.get('web'):
            self.open_issue(link, lambda text, error=True: self.notify(text, severity='error', markup=False))
        else:
            self.push_screen(IssueScreen(link, back))

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
        for gitlab in getattr(self, 'gitlabs', {}).values():  # the merge requests' GitLab clients
            await gitlab.close()

