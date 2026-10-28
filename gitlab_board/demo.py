"""An offline GitLab for --demo and the tests: fictional projects, boards and people, kept in memory.

It answers like the GitLab client and follows GitLab's board rules: which open or closed issues each list holds
(Boards::Issues::ListService), the board's filters, and what a move changes (move_effects). It never runs glab.
"""
import random
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from .client import check_comment, issue_links, linked_issue, mentions, move_effects, number, project_path
from .glab import GlabError

HOST = 'gitlab.example.com'
PEOPLE = {u: {'username': u, 'name': n} for u, n in [('alex', 'Alex Morgan'), ('jordan', 'Jordan Lee'), ('sam', 'Sam Rivera'),
                                                    ('priya', 'Priya Shah'), ('kim', 'Kim Park')]}
# Members of each project and their role (10 Guest, 15 Planner, 30 Developer); kim's account is blocked.
MEMBERS = {'demo/portal': {'alex': 30, 'jordan': 30, 'sam': 15, 'kim': 30, 'priya': 5},
           'demo/api': {'alex': 30, 'priya': 30, 'sam': 10}}
EPICS = {1: 'Online permit requests', 2: 'Accessibility'}
# A task's parent issue; other issues have their epic as parent. Links as (source, type, target), seen from the source.
PARENTS = {'portal#2': 'portal#3'}
LINKED = [('portal#3', 'blocks', 'portal#4'), ('portal#4', 'relates_to', 'portal#5')]
REVERSE = {'blocks': 'is_blocked_by', 'relates_to': 'relates_to'}


def label_list(n, title):
    return {'id': f'gid://gitlab/List/{n}', 'title': title, 'listType': 'label', 'label': title}


def typed_list(n, kind, title, **extra):
    return {'id': f'gid://gitlab/List/{n}', 'title': title, 'listType': kind, 'label': None, **extra}


BOARDS = {
    1: ('project', 'demo/portal', 'Development', {}, [
        typed_list(11, 'backlog', 'Open'), label_list(12, 'To Do'), label_list(13, 'Doing'),
        label_list(14, 'Review'), typed_list(15, 'closed', 'Closed')]),
    2: ('project', 'demo/portal', 'Permits – Sprint 10', {'milestone': {'id': 'gid://gitlab/Milestone/10', 'title': 'Sprint 10'},
                                                         'labels': {'nodes': [{'title': 'Permits'}]}}, [
        typed_list(21, 'backlog', 'Open'), typed_list(22, 'status', 'Ready'), typed_list(23, 'status', 'In progress', maxIssueCount=2),
        typed_list(24, 'status', 'Dev review'), typed_list(25, 'status', 'Customer review'), typed_list(26, 'closed', 'Closed')]),
    3: ('group', 'demo', 'Team planning', {}, [
        typed_list(31, 'backlog', 'Open'), typed_list(32, 'assignee', 'Alex Morgan', assignee=PEOPLE['alex']),
        typed_list(33, 'milestone', 'Sprint 11', milestone='Sprint 11'), typed_list(34, 'closed', 'Closed')]),
}
DESCRIPTION = '''## Acceptance
Arrow keys move between cards, lists and epic swimlanes.
This line continues the paragraph, as on GitLab.

- [x] Lists from the board settings
- [ ] **Epic swimlanes** with `e`
- [~] Drag and drop, see #4

> [!note]
> Keep it usable at 80×24.

| Key | Shows |
| --- | --- |
| `s` | The Markdown source |
| `o` | The issue in a browser, with its images |

```mermaid
graph LR; Open --> Doing --> Review --> Closed
```

![Board sketch](/uploads/0a1b2c/board.png){width=60%} by @alex, tracked in &1 with ~"workflow::doing".'''
# (project, title, open, labels, assignees, milestone, epic, status, confidential)
ISSUES = [
    ('portal', 'Keyboard navigation for the board', True, ['Doing', 'frontend', 'Permits'], ['alex'], 'Sprint 10', 2, 'In progress', False),
    ('portal', 'Write the onboarding guide', True, ['To Do', 'docs'], ['jordan'], 'Sprint 10', None, 'Ready', False),
    ('portal', 'Connect through glab', True, ['Doing', 'Review', 'backend', 'Permits'], ['alex'], 'Sprint 10', 1, 'Dev review', False),
    ('portal', 'Explain permission errors', True, ['To Do', 'backend', 'Permits'], ['sam'], 'Sprint 10', 1, 'Ready', False),
    ('portal', 'Choose a board', False, ['frontend', 'Permits'], ['alex'], 'Sprint 10', None, 'Done', False),
    ('portal', 'Upload plans as PDF', True, ['Permits'], [], 'Sprint 10', 1, None, False),
    ('portal', 'Rotate the signing key', True, ['Review', 'backend'], ['jordan'], 'Sprint 11', None, None, True),
    ('portal', 'Mobile menu covers the map', True, ['bug', 'frontend'], [], None, 2, None, False),
    ('portal', 'Date format in exports', False, ['bug'], ['sam'], 'Sprint 9', None, 'Done', False),
    ('api', 'Rate limit the public API', True, ['Doing', 'backend'], ['priya'], 'Sprint 11', None, 'In progress', False),
    ('api', 'Document the webhook payloads', True, ['docs'], ['alex'], None, None, None, False),
    ('api', 'Answer 404 for archived permits', True, ['bug', 'Permits'], ['sam'], 'Sprint 10', 1, 'Customer review', False),
]
ACTIONS = ['Polish', 'Speed up', 'Document', 'Add tests for', 'Simplify', 'Translate', 'Cache', 'Log errors in',
           'Fix focus in', 'Review', 'Restyle', 'Measure']
AREAS = ['the login form', 'search results', 'the sprint report', 'CSV export', 'email notifications', 'the map view',
         'the settings page', 'plan uploads', 'the onboarding tour', 'error messages']
# Issue number -> (author, hours ago, edited, internal, text), oldest first.
NOTES = {
    3: [('jordan', 50, False, False, 'Token-less: glab keeps the token, the board only runs `glab api`.\nNext: board scope.'),
        ('sam', 26, False, True, 'Internal note: staging moves on Friday. Only project members see this.'),
        ('alex', 3, True, False, '**Markdown** is formatted: `code`, lists and links such as [the docs](https://docs.example.com).\n'
                                 'Rich markup like [b]tags[/b] stays literal; press s for the source.\n\n'
                                 '- [x] Read comments\n- [ ] Reply from the terminal\n\n' + 'A long comment wraps. ' * 12)],
    1: [('priya', 5, False, False, 'Focus order looks right with a screen reader.')],
}
# Merge requests: (project, title, source branch, description, state, draft, pipeline, merge status, conflicts,
# fork, issues it closes, issues it mentions). Titles, branches and descriptions also name YouTrack demo stories
# (DEMO-3), for the YouTrack board's demo; DEMO-10 must not count as DEMO-1.
MERGE_REQUESTS = [
    ('portal', 'Run requests through glab (DEMO-3)', 'DEMO-3-glab', '', 'opened', False, 'SUCCESS', 'NOT_APPROVED', False,
     None, ['portal#3'], []),
    ('portal', 'Draft: Explain sign-in errors', 'sign-in-errors', "Shows glab's own message. Part of DEMO-3.", 'opened', True,
     'FAILED', 'DRAFT_STATUS', False, 'sam/portal', [], ['portal#3']),
    ('api', 'Rate limit glab calls', '3-rate-limit', '', 'opened', False, 'RUNNING', 'CI_STILL_RUNNING', False, None,
     ['api#1'], ['portal#3']),
    ('portal', 'First glab spike', 'spike-glab', 'For DEMO-3.', 'merged', False, 'SUCCESS', 'NOT_OPEN', False, None, [],
     ['portal#3']),
    ('portal', 'Arrow keys across lanes', 'demo-1-lanes', '', 'opened', False, None, 'CHECKING', True, None, ['portal#1'], []),
    ('portal', 'Export dates as ISO 8601', 'DEMO-10-export-dates', '', 'opened', False, 'SUCCESS', 'UNCHECKED', False, None,
     [], ['portal#9']),
    ('api', 'Answer 404 for archived permits', 'archived-404', '', 'opened', False, 'SUCCESS', 'MERGEABLE', False, None,
     ['api#3'], []),
    ('api', 'Prices endpoint for the pricing page', 'web-2-prices', 'Needed by WEB-2.', 'opened', False, 'PENDING',
     'CI_MUST_PASS', False, None, [], []),
]


def stamp(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec='seconds')


def demo_state():
    """Visits a returning user would have, so the recent-first ordering and resume are visible."""
    now = time.time()
    return {'visited': {'demo/portal/-/boards/1': now - 300, 'demo/portal/-/boards/2': now - 86400,
                        'groups/demo/-/boards/3': now - 3 * 86400}, 'project': 'demo/portal'}


class DemoClient:
    host = HOST
    base_url = 'https://' + HOST

    def __init__(self):
        counters, self.issues, self.threads = {}, [], {}
        generated = [('portal', f'{a} {b}', True, [], [], None, None, None, False) for a in ACTIONS for b in AREAS]
        order = random.Random(7).sample(range(len(generated)), len(generated))  # ranked by hand, not by number
        for n, item in enumerate(ISSUES + [generated[i] for i in order]):
            project, title, opened, labels, people, milestone, epic, status, confidential = item
            counters[project] = iid = counters.get(project, 0) + 1
            path = f'demo/{project}'
            self.issues.append({
                'id': f'gid://gitlab/Issue/{n + 1}', 'iid': str(iid), 'title': title, 'reference': f'{path}#{iid}',
                'webUrl': f'{self.base_url}/{path}/-/issues/{iid}', 'state': 'opened' if opened else 'closed',
                'confidential': confidential, 'closedAt': None if opened else stamp(n), 'dueDate': None,
                'type': 'TASK' if f'{project}#{iid}' in PARENTS else 'ISSUE',
                'assignees': [deepcopy(PEOPLE[u]) for u in people], 'labels': labels, 'milestone': milestone,
                'author': deepcopy(PEOPLE['alex' if n % 2 else 'jordan']), 'weight': n % 5 or None, 'healthStatus': None,
                'iteration': None, 'status': status, 'epic': {'id': f'gid://gitlab/Epic/{epic}', 'iid': str(epic),
                                                              'title': EPICS[epic]} if epic else None,
                'description': DESCRIPTION if n == 0 else 'Demo issue. Press a to assign it, m to move it, c to comment, '
                                                          'C to read comments, or o to open it in a browser.',
                'canAdmin': project == 'portal' or n % 2 == 0, 'canComment': True, 'position': n})
        for iid, thread in NOTES.items():
            self.threads[f'demo/portal#{iid}'] = [self.note(*note) for note in thread]
        self.merge_requests, counters = [], {}
        for n, item in enumerate(MERGE_REQUESTS):
            project, title, branch, text, state, draft, pipeline, status, conflicts, fork, closes, mentions = item
            counters[project] = iid = counters.get(project, 0) + 1
            path = f'demo/{project}'
            self.merge_requests.append(({
                'iid': str(iid), 'title': title, 'webUrl': f'{self.base_url}/{path}/-/merge_requests/{iid}', 'state': state,
                'draft': draft, 'sourceBranch': branch, 'targetBranch': 'main', 'description': text, 'conflicts': conflicts,
                'detailedMergeStatus': status, 'updatedAt': stamp(n + 1), 'project': {'fullPath': path},
                'sourceProject': {'fullPath': fork or path}, 'approvalsRequired': 2 if status == 'NOT_APPROVED' else 0,
                'approvalsLeft': 1 if status == 'NOT_APPROVED' else 0,
                'userNotesCount': (6, 3, 0)[n % 3], 'resolvableDiscussionsCount': (3, 2, 0)[n % 3],
                'resolvedDiscussionsCount': (1, 2, 0)[n % 3],
                'autoMergeEnabled': n % 3 == 0, 'author': deepcopy(PEOPLE['alex']),
                'reviewers': {'nodes': [{**deepcopy(PEOPLE['jordan']), 'mergeRequestInteraction': {
                    'reviewState': ('REVIEW_STARTED', 'APPROVED', 'REQUESTED_CHANGES')[n % 3]}}],
                              'pageInfo': {'hasNextPage': False}},
                'assignees': {'nodes': [deepcopy(PEOPLE['sam'])] if n % 3 != 2 else [],
                              'pageInfo': {'hasNextPage': False}},
                'headPipeline': pipeline and {'status': pipeline, 'detailedStatus': {
                    'label': {'SUCCESS': 'passed'}.get(pipeline, pipeline.lower())}}}, closes, mentions))

    @staticmethod
    def note(author, hours, edited, internal, body):
        created = stamp(hours)
        return {'id': int(time.time() * 1000) - hours, 'body': body, 'author': deepcopy(PEOPLE[author]), 'created_at': created,
                'updated_at': stamp(hours - 1) if edited else created, 'system': False, 'internal': internal}

    async def me(self):
        return deepcopy(PEOPLE['alex'])

    async def place(self, path, kind=''):
        kinds = {'demo/portal': 'project', 'demo/api': 'project', 'demo': 'group'}
        if path not in kinds or kind not in ('', kinds[path]):
            error = GlabError(f'GitLab has no project or group {path} that you can see.')
            error.status = 404
            raise error
        boards = [{'id': f'gid://gitlab/Board/{n}', 'name': name, 'number': n}
                  for n, (k, p, name, _, _) in BOARDS.items() if p == path]
        return {'kind': kinds[path], 'path': path, 'webUrl': f'{self.base_url}/{"groups/" if kinds[path] == "group" else ""}{path}',
                'parent': 'demo' if path != 'demo' else None, 'boards': boards}

    async def recent_boards(self, place):
        return [2] if place['path'] == 'demo/portal' else []  # as if you had opened the sprint board on GitLab

    async def board(self, place, board_number):
        kind, path, name, scope, _ = BOARDS.get(board_number, (None, None, None, None, None))
        if path != place['path']:
            error = GlabError(f'Board {board_number} is not in {place["path"]}, or you cannot see it.')
            error.status = 404
            raise error
        return {'id': f'gid://gitlab/Board/{board_number}', 'name': name, 'hideBacklogList': False, 'hideClosedList': False,
                'webUrl': f'{self.base_url}/{"groups/" if kind == "group" else ""}{path}/-/boards/{board_number}',
                'milestone': None, 'iteration': None, 'iterationCadence': None, 'assignee': None, 'weight': None,
                'labels': {'nodes': []}, **deepcopy(scope)}

    def matches(self, issue, found):
        """The board filters this demo understands; any other is refused rather than ignored."""
        known = {'labelName', 'milestoneTitle', 'milestoneWildcardId', 'assigneeUsername', 'assigneeWildcardId',
                 'authorUsername', 'search', 'confidential', 'not', 'or'}
        if set(found) - known or set(found.get('not') or {}) - {'labelName', 'milestoneTitle', 'assigneeUsername'} \
                or set(found.get('or') or {}) - {'labelNames', 'assigneeUsernames'}:
            raise GlabError('The demo does not apply this filter; try it against GitLab.')

        def many(value):
            return value if isinstance(value, list) else [value]
        people = {a['username'] for a in issue['assignees']}
        no = found.get('not') or {}
        either = found.get('or') or {}
        wildcard = {'NONE': issue['milestone'] is None, 'ANY': issue['milestone'] is not None}
        return (all(label in issue['labels'] for label in many(found.get('labelName', [])))
                and found.get('milestoneTitle', issue['milestone']) == issue['milestone']
                and wildcard.get(found.get('milestoneWildcardId'), True)
                and set(many(found.get('assigneeUsername', []))) <= people
                and {'NONE': not people, 'ANY': bool(people)}.get(found.get('assigneeWildcardId'), True)
                and found.get('authorUsername', issue['author']['username']) == issue['author']['username']
                and found.get('search', '').casefold() in (issue['title'] + issue['description']).casefold()
                and found.get('confidential', issue['confidential']) == issue['confidential']
                and not any(label in issue['labels'] for label in many(no.get('labelName', [])))
                and no.get('milestoneTitle', object()) != issue['milestone']
                and not set(many(no.get('assigneeUsername', []))) & people
                and (not either.get('labelNames') or any(l in issue['labels'] for l in many(either['labelNames'])))
                and (not either.get('assigneeUsernames') or bool(people & set(many(either['assigneeUsernames'])))))

    def members_of(self, board_number, lst, found):
        """The issues of one list, as Boards::Issues::ListService picks and orders them."""
        kind, path, _, _, lists = BOARDS[board_number]
        issues = [i for i in self.issues if (project_path(i) == path if kind == 'project' else project_path(i).startswith(path + '/'))
                  and self.matches(i, found)]
        kind = lst['listType']
        if kind == 'closed':
            return sorted((i for i in issues if i['state'] == 'closed'), key=lambda i: i['closedAt'], reverse=True)
        if kind == 'status':
            return sorted((i for i in issues if i['status'] == lst['title']), key=lambda i: i['position'])
        issues = [i for i in issues if i['state'] == 'opened']
        if kind == 'label':
            issues = [i for i in issues if lst['label'] in i['labels']]
        elif kind == 'assignee':
            issues = [i for i in issues if lst['assignee']['username'] in {a['username'] for a in i['assignees']}]
        elif kind == 'milestone':
            issues = [i for i in issues if i['milestone'] == lst['milestone']]
        else:  # Open: nothing that another list of the board claims
            labels = {l['label'] for l in lists if l['listType'] == 'label'}
            people = {l['assignee']['username'] for l in lists if l['listType'] == 'assignee'}
            milestones = {l['milestone'] for l in lists if l['listType'] == 'milestone'}
            statuses = {l['title'] for l in lists if l['listType'] == 'status'}
            issues = [i for i in issues if not labels & set(i['labels']) and not people & {a['username'] for a in i['assignees']}
                      and i['milestone'] not in milestones and i['status'] not in statuses]
        return sorted(issues, key=lambda i: i['position'])

    def page(self, board_number, lst, found, offset, first):
        issues = self.members_of(board_number, lst, found)
        chunk = [dict(deepcopy(i), listId=lst['id']) for i in issues[offset:offset + first]]
        for issue in chunk:
            del issue['position']
        return {**deepcopy(lst), 'position': None, 'collapsed': False, 'issuesCount': len(issues),
                'maxIssueCount': lst.get('maxIssueCount', 0), 'limitMetric': None, 'issues': chunk,
                'more': len(issues) > offset + first, 'cursor': str(offset + first)}

    async def lists(self, place, board, found, epics=False):
        board_number = int(number(board['id']))
        lists = [self.page(board_number, lst, found, 0, 100) for lst in BOARDS[board_number][4]]
        shown = {i['epic']['id']: i['epic'] for lst in lists for i in lst['issues'] if i['epic']}
        lanes = [deepcopy(shown[k]) for k in sorted(shown, key=lambda k: int(number(k)))] if epics else None
        return {'lists': lists, 'epics': lanes, 'lanes_error': None}

    async def list_page(self, place, board, list_id, found, after=None, first=50):
        board_number = int(number(board['id']))
        lst = next(l for l in BOARDS[board_number][4] if l['id'] == list_id)
        return self.page(board_number, lst, found, int(after or 0), first)

    def find(self, issue):
        return next(i for i in self.issues if i['id'] == issue['id'])

    async def move(self, board, issue, from_list, to_list):
        target = self.find(issue)
        if not target['canAdmin']:
            raise GlabError('GitLab did not move it: you need at least the Planner role.')
        lists = BOARDS[int(number(board['id']))][4]
        effects = move_effects(lists, target, from_list, to_list)
        target['labels'] = [l for l in target['labels'] if l not in effects['remove']] + effects['add']
        target['assignees'] = [a for a in target['assignees'] if a['username'] not in effects['unassign']] + \
            [deepcopy(PEOPLE[u]) for u in effects['assign']]
        if effects['state']:
            target['state'] = 'closed' if effects['state'] == 'close' else 'opened'
            target['closedAt'] = stamp(0) if effects['state'] == 'close' else None
        for key in ('milestone', 'iteration'):
            if key in effects:
                target[key] = effects[key]
        if 'status' in effects:
            target['status'] = effects['status'] if effects['status'] != 'default' else \
                ('Done' if to_list['listType'] == 'closed' else None)

    async def members(self, issue):
        roles = MEMBERS[project_path(issue)]
        return [{**deepcopy(PEOPLE[u]), 'access_level': level, 'state': 'blocked' if u == 'kim' else 'active'}
                for u, level in roles.items() if level >= 10 and u != 'kim'], False

    async def assign(self, issue, usernames):
        target = self.find(issue)
        if not target['canAdmin']:
            raise GlabError('GitLab did not assign it: you need at least the Planner role.')
        if set(usernames) - set(MEMBERS[project_path(issue)]):
            raise GlabError('GitLab kept the assignees: the person is not a member of the project.')
        target['assignees'] = [deepcopy(PEOPLE[u]) for u in usernames]

    async def notes(self, issue):
        return sorted(deepcopy(self.threads.get(issue['reference'], [])), key=lambda n: n['created_at'], reverse=True)

    async def issue_merge_requests(self, issue):
        ref = issue['reference'].removeprefix('demo/')
        linked = [(mr, ref in closes) for mr, closes, mentions in self.merge_requests if ref in closes + mentions]
        return {'mrs': issue_links([deepcopy(mr) for mr, closes in linked if closes], [deepcopy(mr) for mr, _ in linked]),
                'more': False}

    async def project_merge_requests(self, path, key):
        return {'mrs': [{**deepcopy(mr), 'why': f'Names {key} in its {where}'} for mr, _, _ in self.merge_requests
                        if mr['project']['fullPath'] == path and mr['state'] == 'opened' and (where := mentions(key, mr))],
                'more': False}

    async def open_merge_requests(self, place):
        inside = (lambda p: p.startswith(place['path'] + '/')) if place['kind'] == 'group' else place['path'].__eq__
        found = [deepcopy(mr) for mr, _, _ in self.merge_requests
                 if mr['state'] == 'opened' and inside(mr['project']['fullPath'])]
        return {'mrs': sorted(found, key=lambda mr: mr['updatedAt'], reverse=True), 'more': False}

    async def merge_request_issues(self, mr):
        closes, mentions = next((c, m) for found, c, m in self.merge_requests
                                if (found['project']['fullPath'], found['iid']) == (mr['project']['fullPath'], mr['iid']))
        found = {}
        for ref, why in [(r, 'closes it when merged') for r in closes] + [(r, 'mentions it') for r in mentions]:
            issue = next(i for i in self.issues if i['reference'] == 'demo/' + ref)
            found.setdefault(issue['id'], linked_issue({
                'id': number(issue['id']), 'iid': issue['iid'], 'title': issue['title'], 'state': issue['state'],
                'references': {'full': issue['reference']}, 'web_url': issue['webUrl']}, why))
        return list(found.values())

    async def issue(self, reference):
        found = next((i for i in self.issues if i['reference'] == reference), None)
        if not found:
            error = GlabError(f'GitLab did not return {reference}: it may be gone, or hidden from you.')
            error.status = 404
            raise error
        return {**{k: v for k, v in deepcopy(found).items() if k != 'position'}, 'listId': None}

    @staticmethod
    def work_item(issue):
        return {'iid': issue['iid'], 'title': issue['title'], 'reference': issue['reference'], 'webUrl': issue['webUrl'],
                'state': 'OPEN' if issue['state'] == 'opened' else 'CLOSED',
                'workItemType': {'name': issue['type'].capitalize()}}

    async def relations(self, issue):
        found = self.find(issue)
        ref, by_ref = found['reference'].removeprefix('demo/'), {i['reference'].removeprefix('demo/'): i for i in self.issues}
        epic = found['epic'] and found['epic']['iid']
        parent = self.work_item(by_ref[PARENTS[ref]]) if ref in PARENTS else epic and {
            'iid': epic, 'title': EPICS[int(epic)], 'reference': f'demo&{epic}', 'state': 'OPEN',
            'webUrl': f'{self.base_url}/groups/demo/-/epics/{epic}', 'workItemType': {'name': 'Epic'}}
        return {'parent': parent or None,
                'linked': [{'linkType': kind if ref == source else REVERSE[kind],
                            'workItem': self.work_item(by_ref[target if ref == source else source])}
                           for source, kind, target in LINKED if ref in (source, target)]}

    async def comment(self, issue, text):
        check_comment(text)
        self.threads.setdefault(issue['reference'], []).append(self.note('alex', 0, False, False, text.strip()))

    async def close(self):
        pass
