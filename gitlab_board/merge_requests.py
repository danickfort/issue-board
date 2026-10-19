"""Open GitLab merge requests, read-only, for the GitLab and the YouTrack board: those linked to a card, or all of a
project or group. An issue an MR links can be opened from here (i).

Status words never claim more than GitLab says: a mergeability GitLab has not checked is "not checked yet", and no
pipeline is "no pipeline", not "passed". See docs/plans/linked-merge-requests.md.
"""
import webbrowser
from urllib.parse import urlsplit

from rich.text import Text
from textual import on, work
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option

from youtrack_board.app import Picker, plain
from youtrack_board.diagnostics import LOGGER

from . import target
from .glab import GlabError

OPEN = ('opened', 'locked')  # locked: GitLab is merging it right now
MERGE = {'MERGEABLE': 'ready to merge', 'UNCHECKED': 'mergeability not checked yet', 'CHECKING': 'checking mergeability',
         'PREPARING': 'preparing the diff', 'CONFLICT': 'conflicts', 'NEED_REBASE': 'needs a rebase',
         'CI_MUST_PASS': 'pipeline must succeed', 'CI_STILL_RUNNING': 'waits for the pipeline',
         'DISCUSSIONS_NOT_RESOLVED': 'unresolved threads', 'DRAFT_STATUS': 'draft', 'NOT_APPROVED': 'needs approval',
         'REQUESTED_CHANGES': 'changes requested', 'BLOCKED_STATUS': 'blocked by another MR', 'NOT_OPEN': 'not open'}
REVIEW = {'UNREVIEWED': 'awaiting review', 'REVIEW_STARTED': 'reviewing', 'REVIEWED': 'commented',
          'REQUESTED_CHANGES': 'changes requested', 'APPROVED': 'approved', 'UNAPPROVED': 'removed approval'}


def repository(text):
    """(host, path) of the GitLab project a URL shows, from any of its pages or its https clone URL, or TargetError."""
    text = text.strip()
    url = urlsplit(text if '://' in text else 'https://' + text)
    found = target.parse(f'{url.scheme}://{url.netloc}{url.path.split("/-/")[0].removesuffix(".git")}')
    if found.kind == 'group':
        raise target.TargetError('That is a group. Give the URL of the project that holds the code.')
    return found.host, found.path


def merge_status(mr):
    value = mr.get('detailedMergeStatus')
    return MERGE.get(value) or (value.lower().replace('_', ' ') if value else 'merge status unknown')


def pipeline(mr):
    head = mr.get('headPipeline')
    if not head:
        return 'no pipeline'
    return 'pipeline ' + ((head.get('detailedStatus') or {}).get('label') or str(head.get('status') or 'unknown').lower())


def discussion_status(mr):
    total, resolved = mr.get('resolvableDiscussionsCount'), mr.get('resolvedDiscussionsCount')
    if total is None or resolved is None:
        return 'unknown'
    if total == 0:
        return 'no resolvable threads'
    return f'{total - resolved} unresolved · {resolved} resolved'


def person(user):
    if not user:
        return 'unknown'
    name, username = plain(user.get('name') or ''), plain(user.get('username') or '')
    if name and username and name != username:
        return f'{name} (@{username})'
    return name or (f'@{username}' if username else 'unknown')


def participants(mr, role):
    connection = mr.get(role)
    if connection is None:
        return 'unknown'
    names = []
    for user in connection.get('nodes') or []:
        state = (user.get('mergeRequestInteraction') or {}).get('reviewState') if role == 'reviewers' else None
        label = person(user)
        if state:
            label += ' — ' + REVIEW.get(state, plain(state).lower().replace('_', ' '))
        names.append(label)
    if (connection.get('pageInfo') or {}).get('hasNextPage'):
        names.append('more in GitLab')
    return '; '.join(names) or 'none'


def summary(mr):
    """What stands between the MR and a merge, in a few words."""
    count = mr.get('userNotesCount')
    activity = [f'{count} comment{"s" if count != 1 else ""}' if count is not None else None]
    if mr.get('resolvableDiscussionsCount') is not None and mr.get('resolvedDiscussionsCount') is not None:
        activity.append('threads: ' + discussion_status(mr))
    changes = any((user.get('mergeRequestInteraction') or {}).get('reviewState') == 'REQUESTED_CHANGES'
                  for user in (mr.get('reviewers') or {}).get('nodes') or [])
    words = ['being merged' if mr['state'] == 'locked' else None, 'draft' if mr.get('draft') else None,
             'auto-merge on' if mr.get('autoMergeEnabled') is True else None,
             'changes requested' if changes else None, *activity, pipeline(mr),
             merge_status(mr), 'conflicts' if mr.get('conflicts') else None]
    return ' · '.join(dict.fromkeys(plain(w) for w in words if w))


def approvals(mr):
    required, left = mr.get('approvalsRequired'), mr.get('approvalsLeft')  # Premium only; absent elsewhere
    if not required:
        return None
    if left is None:
        return f'unknown ({required} required)'
    return 'approved' if not left else f'{left} of {required} still needed'


def reference(mr, home=None):
    path = mr['project']['fullPath']
    return f'!{mr["iid"]}' if path == home else f'{path}!{mr["iid"]}'


def web_url(mr, host):
    """The MR's page, only if it is https on this host and at the MR's own project; else None."""
    url = mr.get('webUrl') or ''
    parts = urlsplit(url)
    page = f'/{mr["project"]["fullPath"]}/-/merge_requests/{mr["iid"]}'
    ok = parts.scheme == 'https' and parts.netloc.lower() == host and parts.path == page and not (parts.query or parts.fragment)
    return url if ok else None


def details(mr, home=None):
    source = (mr.get('sourceProject') or {}).get('fullPath')  # None when the fork is hidden from you
    fork = f'  (from the fork {source})' if source and source != mr['project']['fullPath'] else ''
    status = [('Pipeline', pipeline(mr).removeprefix('pipeline ')), ('Merge', merge_status(mr)),
              ('Approvals', approvals(mr)), ('Conflicts', 'yes' if mr.get('conflicts') else None)]
    updated = (mr.get('updatedAt') or '')[:16].replace('T', ' ')
    count = mr.get('userNotesCount')
    activity = f'Comments: {count if count is not None else "unknown"} · Threads: {discussion_status(mr)}'
    auto = 'on' if mr.get('autoMergeEnabled') is True else 'off' if mr.get('autoMergeEnabled') is False else 'unknown'
    return Text.assemble(
        (reference(mr, home), 'bold'), f'  {"draft · " if mr.get("draft") else ""}{"being merged" if mr["state"] == "locked" else "open"} · auto-merge {auto}\n',
        plain(f'Branch: {mr.get("sourceBranch")} → {mr.get("targetBranch")}{fork}\n'),
        plain(activity), '\n',
        plain(f'Author: {person(mr.get("author"))}\nReviewers: {participants(mr, "reviewers")}\n'
              f'Assignees: {participants(mr, "assignees")}\n'),
        plain('  ·  '.join(f'{name}: {value}' for name, value in status if value)), '\n',
        (plain(' · '.join(filter(None, [mr.get('why'), f'updated {updated} UTC' if updated else None]))), 'dim'))


class MergeRequestsScreen(ModalScreen):
    """Open MRs, read when it opens and on r. Closing it cancels a request still running, so no late answer lands, and
    the board behind it stays as it was. Nothing here changes an MR. It dismisses with ('issue', issue) when i picks an
    issue the selected MR links."""
    BINDINGS = [Binding('escape,g,G', 'dismiss', 'Close'), Binding('r', 'load', 'Reload'), Binding('o', 'open', 'Browser'),
                Binding('i', 'issue', 'Linked issue'), Binding('u', 'dismiss("repo")', 'Change repository'),
                Binding('pagedown', 'details_down', 'Scroll details', show=False, priority=True),
                Binding('pageup', 'details_up', 'Scroll details', show=False, priority=True)]
    # Own ids: the boards' #dialog and .hint rules would outrank DEFAULT_CSS.
    DEFAULT_CSS = '''
    #mr-dialog { width: 100; max-width: 96%; height: 90%; max-height: 96%; padding: 0 1; border: round $border;
                 background: $surface; }
    #mr-title { text-style: bold; }
    #mr-about { color: $text-muted; }
    #merge-requests { height: 1fr; min-height: 3; margin-top: 1; padding: 0; border: none; scrollbar-size-vertical: 1; }
    #mr-detail-scroll { height: auto; max-height: 6; border-top: solid $foreground 25%; scrollbar-size-vertical: 1; }
    #mr-details { height: auto; }
    #mr-hint { color: $text-muted; }
    #mr-hint.error { color: $text-error; }
    '''

    def __init__(self, title, about, load, host, home=None, repo=False, links=None,
                 empty='No open merge request is linked.'):
        """load: an async function returning {'mrs', 'more'}, more saying what was not read, if anything. host: where MR pages may open. home: the project whose
        MRs are named !iid only. repo: u may dismiss with 'repo' to choose another repository. links: an async function
        returning the issues an MR links, each with 'ref', 'title' and 'why'. empty: what shows when no MR is open."""
        super().__init__()
        self.prompt, self.about, self.loader, self.host, self.home, self.repo = title, about, load, host, home, repo
        self.links, self.empty, self.mrs, self.notes = links, empty, [], ''

    def check_action(self, action, parameters):
        return (self.repo or parameters != ('repo',)) and (action != 'issue' or self.links is not None)

    def compose(self):
        with Vertical(id='mr-dialog'):
            yield Label(Text(self.prompt), id='mr-title')
            yield Static(Text(self.about), id='mr-about')
            yield OptionList(id='merge-requests')
            with VerticalScroll(id='mr-detail-scroll') as pane:
                pane.border_title = 'Details · PgUp/PgDn scroll'
                yield Static('', id='mr-details')
            yield Static('', id='mr-hint')

    def on_mount(self):
        self.query_one(OptionList).focus()
        self.load()

    def action_load(self):
        self.load()

    def show(self, message, hint, error=False):
        self.mrs = []
        options = self.query_one(OptionList)
        options.set_options([Option(Text(message, 'dim'), disabled=True)])
        self.query_one('#mr-details', Static).display = False
        self.query_one('#mr-detail-scroll').display = False
        self.hint(hint, error)

    def hint(self, text, error=False):
        if error:
            LOGGER.error('%s', text)
        keys = '  ·  r reload' + ('  ·  i linked issue' if self.links and self.mrs else '') + \
            ('  ·  u repository' if self.repo else '') + '  ·  Esc back'
        self.query_one('#mr-hint', Static).update(Text(plain(text) + keys))
        self.query_one('#mr-hint').set_class(error, 'error')

    @work(exclusive=True)
    async def load(self):
        keep = (self.current() or {}).get('webUrl')
        self.show('Loading merge requests…', 'Loading')
        try:
            found = await self.loader()
        except GlabError as exc:
            self.show('Could not load the merge requests.', str(exc), error=True)
            return
        self.mrs = [mr for mr in found['mrs'] if mr.get('state') in OPEN]
        notes = [f'{len(self.mrs) or "No"} open']
        hidden = len(found['mrs']) - len(self.mrs)
        if hidden:
            notes.append(f'{hidden} merged or closed not shown')
        if found.get('more'):
            notes.append(found['more'])
        if not self.mrs:
            self.show(self.empty, ' · '.join(notes))
            return
        options = self.query_one(OptionList)
        options.set_options([Option(Text.assemble((reference(mr, self.home), 'bold'), '  ', plain(mr['title']), '\n',
                                                  ('  Branch: ' + plain(mr.get('sourceBranch') or 'unknown') + '\n', 'dim'),
                                                  ('  ' + summary(mr), 'dim'))) for mr in self.mrs])
        options.highlighted = next((n for n, mr in enumerate(self.mrs) if mr.get('webUrl') == keep), 0)
        self.notes = ' · '.join(notes)
        self.query_one('#mr-details', Static).display = True
        self.query_one('#mr-detail-scroll').display = True
        self.hint(' · '.join(notes) + '  ·  Enter/o browser')
        self.show_details()

    def current(self):
        index = self.query_one(OptionList).highlighted
        return self.mrs[index] if self.mrs and index is not None and index < len(self.mrs) else None

    @on(OptionList.OptionHighlighted)
    def show_details(self):
        if self.current():
            self.query_one('#mr-details', Static).update(details(self.current(), self.home))
            self.query_one('#mr-detail-scroll').scroll_home(animate=False)

    def action_details_down(self):
        self.query_one('#mr-detail-scroll').scroll_page_down(animate=False)

    def action_details_up(self):
        self.query_one('#mr-detail-scroll').scroll_page_up(animate=False)

    @on(OptionList.OptionSelected)
    def action_open(self):
        mr = self.current()
        if not mr:
            return
        url = web_url(mr, self.host)
        if not url:
            self.hint(f'Not opening {mr.get("webUrl")}: it is not a merge request page on {self.host}.', error=True)
        elif webbrowser.open(url):
            self.app.notify(url, title=f'Opening {reference(mr, self.home)}', markup=False)
        else:
            self.hint(f'No browser available. Open {url}', error=True)

    def action_issue(self):
        if self.current():
            self.find_issues(self.current())

    @work(exclusive=True, group='links')
    async def find_issues(self, mr):
        """The issues the MR links: one opens at once, several are offered, none is said."""
        ref = reference(mr, self.home)
        self.hint(f'Looking for the issues {ref} links…')
        try:
            found = await self.links(mr)
        except GlabError as exc:
            self.hint(f'Could not read the issues {ref} links. {exc}', error=True)
            return
        if not found:
            self.hint(f'{ref} links no issue you can see  ·  {self.notes}  ·  Enter/o browser')
        elif len(found) == 1:
            self.dismiss(('issue', found[0]))
        else:
            self.hint(f'{self.notes}  ·  Enter/o browser')
            choices = [(n, Text.assemble((plain(i['ref']), 'bold'), '  ', plain(i.get('title') or ''),
                                         (f'  · {plain(i["why"])}', 'dim')), f'{i["ref"]} {i.get("title") or ""}'.casefold(),
                        False) for n, i in enumerate(found)]
            self.app.push_screen(Picker(f'{len(found)} issues {ref} links · Enter opens one', choices),
                                 lambda n: n is not None and self.dismiss(('issue', found[n])))
