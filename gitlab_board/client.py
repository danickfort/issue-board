"""GitLab boards through `glab api`: the same GraphQL board queries and mutations GitLab's own board page uses.

GitLab computes which issues belong in which list, and in what order, so nothing here guesses list membership. Values
are written into the GraphQL document as literals: strings JSON-escaped (valid GraphQL string syntax), enums and input
keys checked against a strict pattern. The document travels on glab's stdin, never in argv.
"""
import json
import re
from urllib.parse import quote

from .glab import Glab, GlabError
from .target import ENUMS

NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
FIRST = 100  # issues per list on the board; GitLab's maximum page size
MR_BATCH = 50  # reviewer and assignee connections must stay below GitLab's query complexity limit
PEOPLE_PAGES = 5  # up to 500 project members in the assign picker
OPEN_PAGES = 5  # up to 500 open merge requests searched for a story ID
NOTE_PAGES = 20  # up to 2,000 comments
ALL_PAGES = 10  # up to 500 open merge requests, MR_BATCH at a time, in the panel of all of them
ISSUE = ('id iid title reference(full: true) webUrl state confidential closedAt dueDate type '
         'milestone { title } assignees { nodes { username name } } labels { nodes { title } } '
         'author { username name } description userPermissions { adminIssue createNote }')
ISSUE_EE = ISSUE + ' weight healthStatus iteration { title } epic { id iid title } status { name }'
LIST = 'id title listType position collapsed issuesCount label { title }'
LIST_EE = LIST + ' maxIssueCount limitMetric assignee { username name } milestone { title } iteration { title }'
# A merge request's status, as the panel shows it. Approvals are Premium.
MR = ('iid title webUrl state draft sourceBranch targetBranch conflicts detailedMergeStatus updatedAt '
      'userNotesCount resolvableDiscussionsCount resolvedDiscussionsCount '
      'autoMergeEnabled author { username name } '
      'reviewers(first: 20) { pageInfo { hasNextPage } nodes { username name mergeRequestInteraction { reviewState } } } '
      'assignees(first: 20) { pageInfo { hasNextPage } nodes { username name } } '
      'project { fullPath } sourceProject { fullPath } headPipeline { status detailedStatus { label } }')
MR_EE = MR + ' approvalsLeft approvalsRequired'
BOARD = 'id name webUrl hideBacklogList hideClosedList'
BOARD_EE = BOARD + (' weight milestone { id title } iteration { id title } iterationCadence { id } assignee { username } '
                    'labels(first: 100) { nodes { title } }')


class Enum(str):
    """A GraphQL enum value, written without quotes."""


def literal(value):
    """A GraphQL input literal. Strings can never close their quotes; names and enums must be plain identifiers."""
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, Enum):
        if not NAME.fullmatch(value):
            raise GlabError(f'Not a GraphQL enum value: {value!r}')
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(literal(v) for v in value) + ']'
    if isinstance(value, dict):
        if not all(isinstance(k, str) and NAME.fullmatch(k) for k in value):
            raise GlabError('Not a GraphQL input field name.')
        return '{' + ', '.join(f'{k}: {literal(v)}' for k, v in value.items()) + '}'
    raise GlabError(f'Cannot send a {type(value).__name__} to GitLab.')


def board_input(found):
    """Board filters with wildcard and health values marked as enums."""
    def mark(part):
        return {k: (Enum(v) if k in ENUMS and isinstance(v, str) else v) for k, v in part.items()}
    result = mark({k: v for k, v in found.items() if k not in ('not', 'or')})
    for key in ('not', 'or'):
        if found.get(key):
            result[key] = mark(found[key])
    return result


def number(gid):
    return str(gid).rsplit('/', 1)[-1]


def project_path(issue):
    """'group/project' from a full reference such as 'group/project#12'."""
    return issue['reference'].rsplit('#', 1)[0]


def flat_issue(node, list_id=None):
    issue = {k: v for k, v in node.items() if k not in ('assignees', 'labels', 'milestone', 'iteration', 'status',
                                                          'userPermissions')}
    issue.update(assignees=(node.get('assignees') or {}).get('nodes') or [],
                 labels=[label['title'] for label in (node.get('labels') or {}).get('nodes') or []],
                 milestone=(node.get('milestone') or {}).get('title'),
                 iteration=(node.get('iteration') or {}).get('title'), status=(node.get('status') or {}).get('name'),
                 canAdmin=bool((node.get('userPermissions') or {}).get('adminIssue')),
                 canComment=bool((node.get('userPermissions') or {}).get('createNote')), listId=list_id)
    return issue


def flat_list(node):
    page = node.get('issues') or {}
    info = page.get('pageInfo') or {}
    result = {k: v for k, v in node.items() if k not in ('issues', 'label', 'milestone', 'iteration')}
    result.update(label=(node.get('label') or {}).get('title'), milestone=(node.get('milestone') or {}).get('title'),
                  iteration=(node.get('iteration') or {}).get('title'),
                  issues=[flat_issue(n, node['id']) for n in page.get('nodes') or []],
                  more=bool(info.get('hasNextPage')), cursor=info.get('endCursor'))
    return result


MOVABLE = ('label', 'assignee', 'milestone', 'iteration', 'status')  # List.movable_types, with GitLab EE's


def scope(label):
    """The scope of a scoped label ('a::b::c' -> 'a::b'), or None."""
    return label.rsplit('::', 1)[0] if '::' in label else None


def move_effects(lists, issue, source, target):
    """What GitLab changes when the issue moves from one list to another, after Boards::Issues::MoveService and its EE
    part: shown before a move and applied by the demo. On a real board GitLab decides; this only explains."""
    kind, origin = target['listType'], source['listType']
    labels, people = list(issue['labels']), [a['username'] for a in issue['assignees']]
    if kind in MOVABLE:  # a movable list only takes away the label of the list the card leaves, and only between label lists
        remove = [source['label']] if origin == 'label' == kind and source.get('label') else []
    else:  # into Open or Closed: every label that has a list on this board goes
        remove = [l['label'] for l in lists if l['listType'] == 'label' and l.get('label')]
    add = [target['label']] if kind == 'label' and target.get('label') else []
    # Scoped labels (Premium): a label replaces any other with the same scope.
    replaced = [l for l in labels for a in add if scope(a) and scope(l) == scope(a) and l != a]
    effects = {'add': [l for l in add if l not in labels], 'remove': [l for l in labels if l in remove + replaced and l not in add],
               'state': 'reopen' if origin == 'closed' and kind != 'closed' else 'close' if kind == 'closed' else None}
    user = (target.get('assignee') or {}).get('username')
    gone = (source.get('assignee') or {}).get('username') if origin == 'assignee' and kind in ('assignee', 'backlog') else None
    effects['assign'] = [user] if user and user not in people else []
    effects['unassign'] = [gone] if gone and gone in people and gone != user else []
    if kind == 'milestone' and target.get('milestone') != issue.get('milestone'):
        effects['milestone'] = target.get('milestone')
    elif kind == 'backlog' and origin == 'milestone' and issue.get('milestone'):
        effects['milestone'] = None
    if kind == 'iteration' and target.get('iteration') != issue.get('iteration'):
        effects['iteration'] = target.get('iteration')
    elif kind == 'backlog' and origin == 'iteration' and issue.get('iteration'):
        effects['iteration'] = None
    if kind == 'status':
        effects['status'] = target['title']
    elif origin == 'status' and kind in ('backlog', 'closed'):
        effects['status'] = 'default'
    return effects


def describe_move(effects):
    words = []
    if effects['state']:
        words.append({'close': 'closes it', 'reopen': 'reopens it'}[effects['state']])
    words += [f'adds ~{l}' for l in effects['add']] + [f'removes ~{l}' for l in effects['remove']]
    words += [f'assigns @{u}' for u in effects['assign']] + [f'unassigns @{u}' for u in effects['unassign']]
    for key in ('milestone', 'iteration'):
        if key in effects:
            words.append(f'sets {key} {effects[key]}' if effects[key] else f'clears the {key}')
    if 'status' in effects:
        words.append(f'sets status {effects["status"]}' if effects['status'] != 'default' else 'resets the status')
    return ', '.join(words) or 'reorders only'


def issue_links(closing, related):
    """Each MR once, saying how it is linked: closing MRs also appear among the related ones."""
    found = {}
    for mr in related + closing:
        found.setdefault((mr['project']['fullPath'], mr['iid']), {**mr, 'why': 'Mentions this issue'})
    for mr in closing:
        found[(mr['project']['fullPath'], mr['iid'])]['why'] = 'Closes this issue when merged'
    return list(found.values())


def mentions(key, mr):
    """Where an MR names a story ID as a whole token, ignoring case: 'title, branch', or ''. DEMO-1 is not DEMO-10."""
    token = re.compile(rf'(?<![A-Za-z0-9]){re.escape(key)}(?![A-Za-z0-9])', re.IGNORECASE)
    return ', '.join(name for name, text in (('title', mr.get('title')), ('branch', mr.get('sourceBranch')),
                                             ('description', mr.get('description'))) if token.search(text or ''))


def named(prefixes, mr):
    """{story ID: where} for every story of these YouTrack project prefixes an MR names, matched as mentions() does."""
    found = {}
    for prefix in prefixes:
        token = re.compile(rf'(?<![A-Za-z0-9]){re.escape(prefix)}-([1-9][0-9]*)(?![A-Za-z0-9])', re.IGNORECASE)
        for name, text in (('title', mr.get('title')), ('branch', mr.get('sourceBranch')),
                           ('description', mr.get('description'))):
            for digits in token.findall(text or ''):
                found.setdefault(f'{prefix}-{digits}', {})[name] = None
    return {key: ', '.join(where) for key, where in found.items()}


def linked_issue(issue, why):
    """An issue from GitLab's REST API, shaped as the board's cards are where the app reads them."""
    reference = (issue.get('references') or {}).get('full') or ''
    return {'id': f'gid://gitlab/Issue/{int(issue["id"])}', 'iid': str(issue['iid']), 'title': issue.get('title') or '',
            'reference': reference, 'ref': reference or f'#{issue["iid"]}', 'webUrl': issue.get('web_url') or '',
            'state': issue.get('state'), 'why': why}


def check_comment(text):
    """The comment to send, or GlabError: GitLab runs a line starting with / as a quick action (/close, /assign…)."""
    text = text.strip()
    if not text:
        raise GlabError('A comment cannot be empty.')
    if re.search(r'^\s*/', text, re.MULTILINE):
        raise GlabError('A line of this comment starts with “/”, which GitLab runs as a quick action (/close, '
                        '/assign…). Quick actions are not sent from the board; reword it or use GitLab.')
    return text


class Client:
    def __init__(self, host, glab=None):
        self.glab = glab or Glab(host)
        self.host = self.glab.host
        self.base_url = 'https://' + self.host
        self.ee = True  # GitLab EE fields (scope, weight, epics…) until the server says it has none

    async def ask(self, make):
        """Run make(ee) as GraphQL; on a GitLab without EE fields, ask again with the core fields only."""
        try:
            return await self.glab.graphql(make(self.ee))
        except GlabError as exc:
            if not (self.ee and "doesn't exist on type" in str(exc)):
                raise
            self.ee = False
            return await self.glab.graphql(make(False))

    @staticmethod
    def namespace(place):
        return f'{"group" if place["kind"] == "group" else "project"}(fullPath: {literal(place["path"])})'

    async def me(self):
        data = await self.glab.graphql('query { currentUser { username name } }')
        return data.get('currentUser') or {}

    async def place(self, path, kind=''):
        """A project or group with its boards. A plain namespace URL can be either, so GitLab is asked for both."""
        boards = 'boards(first: 100) { nodes { id name } }'  # shortcut: the first 100 boards; more need paging
        parts = []
        if kind in ('', 'project'):
            parts.append(f'p: project(fullPath: {literal(path)}) {{ fullPath webUrl group {{ fullPath }} {boards} }}')
        if kind in ('', 'group'):
            parts.append(f'g: group(fullPath: {literal(path)}) {{ fullPath webUrl parent {{ fullPath }} {boards} }}')
        data = await self.glab.graphql('query { ' + ' '.join(parts) + ' }')
        found, kind = (data.get('p'), 'project') if data.get('p') else (data.get('g'), 'group')
        if not found:
            error = GlabError(f'GitLab has no project or group {path} that you can see.')
            error.status = 404
            raise error
        parent = (found.get('group') or found.get('parent') or {}).get('fullPath')
        return {'kind': kind, 'path': found['fullPath'], 'webUrl': found['webUrl'], 'parent': parent,
                'boards': [{'id': b['id'], 'name': b['name'], 'number': int(number(b['id']))}
                           for b in (found.get('boards') or {}).get('nodes') or []]}

    async def recent_boards(self, place):
        """Numbers of the boards you visited last on GitLab's web pages, newest first (GitLab keeps 4 per person). GitLab
        has no board activity time: Board.updatedAt changes only with the board's own settings."""
        data = await self.glab.graphql(f'query {{ ns: {self.namespace(place)} {{ recentIssueBoards(first: 4) '
                                       '{ nodes { id } } } }')
        return [int(number(b['id'])) for b in ((data.get('ns') or {}).get('recentIssueBoards') or {}).get('nodes') or []]

    async def board(self, place, board_number):
        """The board's settings, including its scope (Premium), which GitLab's page adds to the filters itself."""
        board_id = literal(f'gid://gitlab/Board/{int(board_number)}')
        data = await self.ask(lambda ee: f'query {{ ns: {self.namespace(place)} {{ board(id: {board_id}) '
                                         f'{{ {BOARD_EE if ee else BOARD} }} }} }}')
        board = (data.get('ns') or {}).get('board')
        if not board:
            error = GlabError(f'Board {board_number} is not in {place["path"]}, or you cannot see it.')
            error.status = 404
            raise error
        return board

    def lists_document(self, place, board, found, ee, epics):
        filters = literal(board_input(found))
        issues = f'issues(first: {FIRST}, filters: {filters}) {{ pageInfo {{ hasNextPage endCursor }} ' \
                 f'nodes {{ {ISSUE_EE if ee else ISSUE} }} }}'
        lanes = f'epics(first: 50, issueFilters: {filters}) {{ nodes {{ id iid title }} }}' if epics else ''
        return (f'query {{ ns: {self.namespace(place)} {{ board(id: {literal(board["id"])}) {{ '
                f'lists(issueFilters: {filters}) {{ nodes {{ {LIST_EE if ee else LIST} {issues} }} }} {lanes} }} }} }}')

    async def lists(self, place, board, found, epics=False):
        """Every list of the board with up to FIRST issues each, in GitLab's order, and the epic lanes if asked.
        Returns {'lists', 'epics', 'lanes_error'}: epics is None when lanes are off or unavailable."""
        lanes_error = None
        try:
            data = await self.ask(lambda ee: self.lists_document(place, board, found, ee, epics and ee))
        except GlabError as exc:
            if not epics:
                raise
            data, lanes_error = await self.ask(lambda ee: self.lists_document(place, board, found, ee, False)), str(exc)
        if epics and not self.ee:
            lanes_error = 'Epic swimlanes need GitLab Premium or Ultimate.'
        found_board = (data.get('ns') or {}).get('board')
        if not found_board:
            raise GlabError(f'Board {board.get("name")} is no longer available.')
        epic_nodes = (found_board.get('epics') or {}).get('nodes') if epics and not lanes_error else None
        nodes = (found_board.get('lists') or {}).get('nodes') or []
        # GitLab pins Open and Closed at the edges; only the middle lists use their saved position.
        nodes = sorted(nodes, key=lambda n: ({'backlog': 0, 'closed': 2}.get(n['listType'], 1), n.get('position') or 0))
        return {'lists': [flat_list(n) for n in nodes],
                'epics': epic_nodes, 'lanes_error': lanes_error}

    async def list_page(self, place, board, list_id, found, after=None, first=50):
        """One page of one list, from a cursor: (list with its issues, cursor, whether more follow)."""
        filters = literal(board_input(found))
        cursor = f', after: {literal(after)}' if after else ''
        data = await self.ask(lambda ee: (
            f'query {{ ns: {self.namespace(place)} {{ board(id: {literal(board["id"])}) {{ '
            f'lists(id: {literal(list_id)}, issueFilters: {filters}) {{ nodes {{ {LIST_EE if ee else LIST} '
            f'issues(first: {int(first)}, filters: {filters}{cursor}) {{ pageInfo {{ hasNextPage endCursor }} '
            f'nodes {{ {ISSUE_EE if ee else ISSUE} }} }} }} }} }} }} }}'))
        nodes = (((data.get('ns') or {}).get('board') or {}).get('lists') or {}).get('nodes') or []
        if not nodes:
            raise GlabError('This list is no longer on the board.')
        return flat_list(nodes[0])

    async def move(self, board, issue, from_list, to_list):
        """issueMoveList, as a drag on GitLab's board: GitLab changes labels, state, assignee, milestone, iteration or
        status for the two lists involved and leaves everything else alone."""
        data = await self.glab.graphql('mutation { issueMoveList(input: ' + literal({
            'projectPath': project_path(issue), 'iid': str(issue['iid']), 'boardId': board['id'],
            'fromListId': number(from_list['id']), 'toListId': number(to_list['id'])}) + ') { errors issue { id } } }')
        errors = (data.get('issueMoveList') or {}).get('errors') or []
        if errors:
            raise GlabError('GitLab did not move it: ' + '; '.join(map(str, errors)))

    async def members(self, issue):
        """People who are members of the issue's project, directly, through groups or by invitation:
        (active members with at least the Guest role, whether the list was cut short)."""
        path = f'projects/{quote(project_path(issue), safe="")}/members/all'
        found, more = await self.glab.pages(path, PEOPLE_PAGES)
        return [m for m in found if isinstance(m, dict) and m.get('username') and m.get('state', 'active') == 'active'
                and (m.get('access_level') or 0) >= 10], more

    async def assign(self, issue, usernames):
        """Make these the issue's only assignees (an empty list unassigns), and check that GitLab did."""
        data = await self.glab.graphql('mutation { issueSetAssignees(input: ' + literal({
            'projectPath': project_path(issue), 'iid': str(issue['iid']), 'assigneeUsernames': list(usernames),
            'operationMode': Enum('REPLACE')}) + ') { errors issue { assignees { nodes { username } } } } }')
        result = data.get('issueSetAssignees') or {}
        if result.get('errors'):
            raise GlabError('GitLab did not assign it: ' + '; '.join(map(str, result['errors'])))
        kept = {a['username'] for a in (((result.get('issue') or {}).get('assignees') or {}).get('nodes') or [])}
        if kept != set(usernames):
            raise GlabError(f'GitLab kept the assignees {", ".join(sorted(kept)) or "empty"}: the person may not '
                            'have access to the project, or this tier allows one assignee.')

    async def notes(self, issue):
        """The issue's comments the account may see, newest first, without system notes (GitLab's activity)."""
        path = f'projects/{quote(project_path(issue), safe="")}/issues/{int(issue["iid"])}/notes'
        found, _ = await self.glab.pages(path, NOTE_PAGES, sort='desc', order_by='created_at',
                                         activity_filter='only_comments')
        return [n for n in found if isinstance(n, dict) and not n.get('system')]

    async def comment(self, issue, text):
        path = f'projects/{quote(project_path(issue), safe="")}/issues/{int(issue["iid"])}/notes'
        await self.glab.post(path, {'body': check_comment(text)})

    async def issue_merge_requests(self, issue):
        """The MRs GitLab's issue page lists under Development, in every state, as far as the account may read them:
        those that close the issue when merged, and those that mention it (in its description or comments, or by a
        cross-reference from an MR, a commit or another project). Returns {'mrs', 'more'}; more says what was cut.
        GitLab resolves these for one issue per request, so they are read for the selected card only."""
        work_item = literal(f'gid://gitlab/WorkItem/{int(number(issue["id"]))}')  # an issue is a work item, same id
        data = await self.ask(lambda ee: (
            f'query {{ workItem(id: {work_item}) {{ widgets {{ ... on WorkItemWidgetDevelopment {{ '
            f'closingMergeRequests(first: {FIRST}) {{ pageInfo {{ hasNextPage }} nodes {{ mergeRequest {{ {MR_EE if ee else MR} }} }} }} '
            f'relatedMergeRequests(first: {FIRST}) {{ pageInfo {{ hasNextPage }} nodes {{ {MR_EE if ee else MR} }} }} }} }} }} }}'))
        if not data.get('workItem'):
            raise GlabError(f'GitLab did not return {issue.get("reference")}: it may be gone, or hidden from you.')
        widget = next((w for w in data['workItem'].get('widgets') or [] if w and 'closingMergeRequests' in w), {})
        closing, related = widget.get('closingMergeRequests') or {}, widget.get('relatedMergeRequests') or {}
        # shortcut: the first FIRST of each; no issue checked had more than 3. Page by cursor if that changes.
        more = any((part.get('pageInfo') or {}).get('hasNextPage') for part in (closing, related)) and \
            f'GitLab links more; the first {FIRST} closing and {FIRST} mentioning are shown'
        return {'mrs': issue_links([n['mergeRequest'] for n in closing.get('nodes') or [] if n.get('mergeRequest')],
                                   related.get('nodes') or []), 'more': more}

    async def project_merge_requests(self, path, key):
        """The project's open MRs that name a story ID in their title, source branch or description, as YouTrack's
        GitLab integration links them; MRs from forks count, since they target the project. Read 100 at a time, light,
        then the status of the matches only. Returns {'mrs', 'more'}; more says what was not searched."""
        found, after, more = {}, None, False
        for _ in range(OPEN_PAGES):
            cursor = f', after: {literal(after)}' if after else ''
            data = await self.glab.graphql(
                f'query {{ project(fullPath: {literal(path)}) {{ mergeRequests(state: opened, first: {FIRST}{cursor}) '
                f'{{ pageInfo {{ hasNextPage endCursor }} nodes {{ iid title sourceBranch description }} }} }} }}')
            if not data.get('project'):
                raise GlabError(f'GitLab has no project {path} that you can see.')
            page = data['project'].get('mergeRequests') or {}
            found.update((mr['iid'], where) for mr in page.get('nodes') or [] if (where := mentions(key, mr)))
            info = page.get('pageInfo') or {}
            if not info.get('hasNextPage'):
                break
            after = info.get('endCursor')
        else:  # shortcut: projects with more open MRs are rare; search on the server if that changes
            more = f'only the first {FIRST * OPEN_PAGES} open merge requests of {path} were searched'
        if not found:
            return {'mrs': [], 'more': more}
        nodes, ids = [], list(found)
        for offset in range(0, len(ids), MR_BATCH):
            batch = ids[offset:offset + MR_BATCH]
            data = await self.ask(lambda ee: (
                f'query {{ project(fullPath: {literal(path)}) {{ mergeRequests(iids: {literal(batch)}, '
                f'first: {len(batch)}) {{ nodes {{ {MR_EE if ee else MR} }} }} }} }}'))
            nodes.extend(((data.get('project') or {}).get('mergeRequests') or {}).get('nodes') or [])
        return {'mrs': [{**mr, 'why': f'Names {key} in its {found[mr["iid"]]}'} for mr in nodes], 'more': more}

    async def open_merge_requests(self, place):
        """Every open MR of a project, or of a group and its subgroups, most recently updated first, as GitLab sorts
        them: MR_BATCH at a time, up to ALL_PAGES pages. Returns {'mrs', 'more'}; more says what was not read."""
        found, after, more = {}, None, False
        sub = ', includeSubgroups: true' if place['kind'] == 'group' else ''
        for _ in range(ALL_PAGES):
            cursor = f', after: {literal(after)}' if after else ''
            data = await self.ask(lambda ee: (
                f'query {{ ns: {self.namespace(place)} {{ mergeRequests(state: opened, sort: UPDATED_DESC{sub}, '
                f'first: {MR_BATCH}{cursor}) {{ pageInfo {{ hasNextPage endCursor }} '
                f'nodes {{ {MR_EE if ee else MR} description }} }} }} }}'))
            if not data.get('ns'):
                raise GlabError(f'GitLab has no {place["kind"]} {place["path"]} that you can see.')
            page = data['ns'].get('mergeRequests') or {}
            for mr in page.get('nodes') or []:  # an MR updated between two pages comes again: keep its first place
                found.setdefault((mr['project']['fullPath'], mr['iid']), mr)
            info = page.get('pageInfo') or {}
            if not info.get('hasNextPage'):
                break
            after = info.get('endCursor')
        else:  # shortcut: the newest 500; page further on demand if a project needs more
            more = f'only the {MR_BATCH * ALL_PAGES} most recently updated are shown'
        return {'mrs': list(found.values()), 'more': more}

    async def merge_request_issues(self, mr):
        """The issues GitLab links to an MR, as far as you may see them: those it closes when merged, then those it
        mentions (title, description, commits or comments). Read for one MR, when asked."""
        base = f'projects/{quote(mr["project"]["fullPath"], safe="")}/merge_requests/{int(mr["iid"])}'
        closing, _ = await self.glab.pages(base + '/closes_issues', 1)  # shortcut: the first 100 of each
        related, _ = await self.glab.pages(base + '/related_issues', 1)
        found = {}
        for issue, why in [(i, 'closes it when merged') for i in closing] + [(i, 'mentions it') for i in related]:
            if isinstance(issue, dict) and issue.get('id') and issue.get('iid'):
                found.setdefault(issue['id'], linked_issue(issue, why))
        return list(found.values())

    async def close(self):
        pass
