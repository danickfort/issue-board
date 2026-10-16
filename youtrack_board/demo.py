import random
import time
from copy import deepcopy

from .client import YouTrackError, assignable_field, uses_sprints

PROJECTS = {key: {'id': f'0-{n}', 'shortName': key, 'name': name}
            for n, (key, name) in enumerate([('DEMO', 'Demo app'), ('WEB', 'Website'), ('OPS', 'Operations'),
                                                   ('MOB', 'Mobile app for the members of the Société des Arts')], 1)}
PEOPLE = {login: {'id': f'1-{n}', 'login': login, 'fullName': name, 'banned': login == 'kim'}
          for n, (login, name) in enumerate([('alex', 'Alex Morgan'), ('jordan', 'Jordan Lee'), ('sam', 'Sam Rivera'),
                                             ('priya', 'Priya Shah'), ('kim', 'Kim Park')], 1)}
# Who each project's Assignee field accepts; OPS requires an assignee.
BUNDLES = {'DEMO': ['alex', 'jordan', 'sam', 'kim'], 'WEB': ['alex', 'priya', 'sam'], 'OPS': ['jordan', 'priya'],
           'MOB': ['alex', 'sam']}
# Product team spans two projects, and its In Progress column also holds WEB's Review state.
# Support kanban and Services desk have sprints disabled. Services desk has a swimlane per project, in the board's
# own order; WEB has none there, so its cards land in the uncategorized lane. Website launch has Epic swimlanes,
# which this app does not draw.
BOARDS = {
    'product': ('Product team', ['DEMO', 'WEB'], [['Open'], ['In Progress', 'Review'], ['Done']]),
    'ops': ('Support kanban', ['OPS'], [['Open'], ['In Progress'], ['Done']]),
    'services': ('Services desk', ['MOB', 'OPS', 'DEMO', 'WEB'], [['Open'], ['In Progress', 'Review'], ['Done']]),
    'web': ('Website launch', ['WEB'], [['Open'], ['In Progress'], ['Review'], ['Done']]),
}
NO_SPRINTS = {'ops', 'services'}
SWIMLANES = {
    'services': {'$type': 'AttributeBasedSwimlaneSettings', 'enabled': True,
                 'field': {'id': 'project', 'name': 'project', 'presentation': 'project'},
                 'values': [{'name': key, 'presentation': PROJECTS[key]['name']} for key in ('MOB', 'OPS', 'DEMO')]},
    'web': {'$type': 'IssueBasedSwimlaneSettings', 'enabled': True,
            'field': {'id': '111-1', 'name': 'Type', 'presentation': 'Type'}, 'values': [{'name': 'Epic'}]},
}
ISSUES = [
    ('DEMO', 'Keyboard navigation for the board', 'Open', 'alex'), ('DEMO', 'Write the onboarding guide', 'Open', 'jordan'),
    ('DEMO', 'Connect to YouTrack', 'In Progress', 'alex'), ('DEMO', 'Handle workflow errors', 'In Progress', 'sam'),
    ('DEMO', 'Choose a sprint', 'Done', 'alex'),
    ('WEB', 'New landing page copy', 'Open', None), ('WEB', 'Pricing page layout', 'Review', 'priya'),
    ('WEB', 'Accessibility pass', 'In Progress', 'alex'),
    ('OPS', 'Rotate the staging certificate', 'Open', 'jordan'), ('OPS', 'Customer cannot log in', 'In Progress', 'priya'),
    ('OPS', 'Nightly backup check', 'Done', 'jordan'),
    ('MOB', 'Offline mode for event tickets', 'In Progress', 'sam'), ('MOB', 'Push notification opt-in', 'Open', None),
]
# A board's backlog is a saved search. Product team's spans DEMO and WEB and is long enough to page through; Services
# desk's matches nothing right now; Website launch has none.
BACKLOGS = {
    'product': {'id': '7-1', 'name': 'Product backlog', 'query': 'project: DEMO, WEB #Unresolved has: -{Board Product team}'},
    'ops': {'id': '7-2', 'name': 'Support triage', 'query': 'project: OPS tag: support #Unresolved has: -{Board Support kanban}'},
    'services': {'id': '7-3', 'name': 'Services escalations', 'query': 'project: MOB, OPS, DEMO Priority: Critical #Unresolved'},
}
RANKED = [('DEMO', 'Show the backlog of a board', 'Open', 'alex'), ('WEB', 'Cookie banner wording', 'Open', None),
          ('DEMO', 'Remember the last sprint per board', 'Submitted', 'jordan')]
ACTIONS = ['Polish', 'Speed up', 'Document', 'Add tests for', 'Simplify', 'Localize']
AREAS = ['the login form', 'search results', 'the sprint report', 'CSV export', 'email notifications', 'the mobile menu',
         'the settings page', 'image uploads', 'the onboarding tour', 'error messages']
TRIAGE = [('OPS', 'Disk usage alert on the build server', 'Open', 'jordan'), ('OPS', 'Renew the support phone line', 'Open', None),
          ('OPS', 'Customer asks for an invoice copy', 'Submitted', None), ('OPS', 'Slow VPN from the Lausanne office', 'Open', 'priya')]
# WEB's State follows a workflow (state machine): each state allows only these transitions, by name, to a target state.
WORKFLOW = {'Open': {'start': 'In Progress'}, 'In Progress': {'send to review': 'Review', 'stop': 'Open'},
            'Review': {'request changes': 'In Progress', 'approve': 'Done'}, 'Done': {'reopen': 'Open'}}
# Descriptions are YouTrack Markdown; issues without one here get the default text.
DESCRIPTIONS = {'1': '''## Acceptance
Arrow keys move between cards, columns and swimlanes.
Enter expands the details; Escape returns.

* [x] Columns from the board settings
* [x] **Swimlanes** per project, as on *Services desk*
* [ ] Drag and drop, see DEMO-2

> Keep it usable at 80×24.

| Key | Shows |
| --- | --- |
| `s` | The Markdown source |
| `o` | The issue in a browser, with its images |

```sh
uv run youtrack-board --demo
```

![](keyboard-map.png){width=70%} attached by @alex.morgan; background in [the guide](https://example.com/guide).'''}
DESCRIPTIONS['101'] = '''Open the board's **backlog** with `B`: the saved search the board uses, in its order.

* Search narrows the issues already loaded
* `n` loads the next page'''
# Issue id -> (author, hours ago, edited, visible only to this group, text), oldest first as YouTrack stores them.
COMMENTS = {
    '3': [('jordan', 50, False, None, 'Token auth works against the staging instance.\nNext step: let people pick a sprint.'),
          ('sam', 26, False, 'Developers', 'Staging moves to a new host on Friday. Only developers can see this comment.'),
          ('alex', 3, True, None, '**Markdown** is formatted: `code`, lists and links such as [the guide](https://example.com/guide).\n'
                                  'Rich markup like [b]tags[/b] stays literal; press s for the source.\n\n'
                                  '* [x] Read comments\n* [ ] Reply from the terminal\n\n'
                                  '```\nuv run youtrack-board --demo\n```\n\n' + 'A long comment wraps and scrolls. ' * 12)],
    '7': [('priya', 5, False, None, 'Mobile layout is ready for review.')],
    '101': [('jordan', 30, False, None, 'Should it follow the order we rank it in on the board? Yes, keep that order.')],
}


def backlog_items():
    """Each backlog saved search's issues, in the order they were created."""
    generated = [(project, f'{action} {area}', 'Open', [None, 'alex', None, 'sam', 'priya'][n % 5])
                 for project in ('DEMO', 'WEB') for n, (action, area) in enumerate((a, b) for a in ACTIONS for b in AREAS)]
    return {'7-1': RANKED + generated, '7-2': TRIAGE, '7-3': []}


def user_value(login):
    return {'id': PEOPLE[login]['id'], 'name': PEOPLE[login]['fullName'], 'login': login} if login else None


def demo_comment(login, hours, edited, group, text):
    created = int((time.time() - hours * 3600) * 1000)
    return {'id': f'4-{created}', 'text': text, 'created': created, 'updated': created + 60000 if edited else None,
            'author': {'login': login, 'fullName': PEOPLE[login]['fullName']},
            'visibility': {'$type': 'LimitedVisibility', 'permittedGroups': [{'name': group}], 'permittedUsers': []}
            if group else {'$type': 'UnlimitedVisibility'}}


def demo_state():
    """Visits a returning user would have, so the recent-first ordering is visible in the demo."""
    now = time.time()
    return {'visited': {'product': now - 300, 'ops': now - 86400}}


class DemoClient:
    base_url = 'https://example.youtrack.cloud'

    def __init__(self):
        counters = dict.fromkeys(PROJECTS, 0)

        def issue(n, project, title, state, login, description):
            counters[project] += 1
            return {'id': str(n), 'idReadable': f'{project}-{counters[project]}', 'summary': title, 'project': PROJECTS[project],
                    'description': DESCRIPTIONS.get(str(n), description),
                    'customFields': [{'id': 'state', 'name': 'State', 'value': {'name': state},
                                      '$type': 'StateMachineIssueCustomField' if project == 'WEB' else 'StateIssueCustomField'},
                                     {'id': 'assignee', 'name': 'Assignee', '$type': 'SingleUserIssueCustomField',
                                      'value': user_value(login)}]}

        self.data = [issue(n, *item, 'Demo issue. Press a to assign it, m to move it, c to comment, C to read comments, '
                                      'or o to open in a browser.') for n, item in enumerate(ISSUES, 1)]
        ids, self.backlogs = iter(range(101, 1000)), {}  # backlog issues are not on any board
        for saved, items in backlog_items().items():
            made = [issue(next(ids), *item, 'Backlog issue. Press C to read its comments or o to open it in a browser.')
                    for item in items]
            # Ranked by hand on the board: the first few on top, the rest neither by ID nor by name.
            self.backlogs[saved] = made[:3] + random.Random(4).sample(made[3:], len(made[3:]))
        self.threads = {issue: [demo_comment(*c) for c in thread] for issue, thread in COMMENTS.items()}

    async def boards(self):
        return [{'id': key, 'name': name, 'projects': [PROJECTS[p] for p in projects]}
                for key, (name, projects, _) in BOARDS.items()]

    async def me(self):
        return {'id': PEOPLE['alex']['id'], 'login': 'alex', 'fullName': 'Alex Morgan'}

    async def board(self, board_id):
        name, projects, columns = BOARDS[board_id]
        # Like YouTrack, a board with sprints disabled still names the underlying sprint as current.
        return {'id': board_id, 'name': name, 'currentSprint': {'id': f'{board_id}-1'},
                'projects': [PROJECTS[p] for p in projects], 'sprintsSettings': {'disableSprints': board_id in NO_SPRINTS},
                'swimlaneSettings': SWIMLANES.get(board_id, {'$type': 'AttributeBasedSwimlaneSettings', 'enabled': False}),
                'orphansAtTheTop': False, 'hideOrphansSwimlane': False, 'backlog': deepcopy(BACKLOGS.get(board_id)),
                'columnSettings': {'field': {'name': 'State'}, 'columns': [
                    {'presentation': states[0], 'ordinal': i, 'fieldValues': [{'name': state} for state in states]}
                    for i, states in enumerate(columns)]}}

    async def sprints(self, board_id):
        return [{'id': f'{board_id}-1', 'name': 'Sprint 01', 'archived': False}]

    async def issues(self, board, sprint):
        if sprint and not uses_sprints(board):  # what YouTrack answers to a sprint search on such a board
            return []
        projects = {p['id'] for p in board['projects']}
        return deepcopy([i for i in self.data if i['project']['id'] in projects])

    async def backlog(self, saved, skip, top):
        items = self.backlogs[saved['id']]
        return deepcopy(items[skip:skip + top]), len(items) > skip + top

    async def assignees(self, issue):
        project = issue['project']['shortName']
        settings = {'canBeEmpty': project != 'OPS', 'emptyFieldText': 'Unassigned'}
        return settings, [deepcopy(PEOPLE[login]) for login in BUNDLES[project]]

    async def assign(self, issue, login):
        if not assignable_field(issue):
            raise YouTrackError('Assigning supports single-user Assignee fields only.')
        target = next(i for i in self.data if i['id'] == issue['id'])
        target['customFields'][1]['value'] = user_value(login)

    async def move(self, issue, field_name, value):
        target = next(i for i in self.data if i['id'] == issue['id'])
        target['customFields'][0]['value'] = {'name': value}

    async def transitions(self, issue, field_name):
        field = deepcopy(next(i for i in self.data if i['id'] == issue['id'])['customFields'][0])
        field['possibleEvents'] = [{'id': event, 'presentation': state, '$type': 'Event'}
                                   for event, state in WORKFLOW.get(field['value']['name'], {}).items()]
        return field

    async def transition(self, issue, field, event):
        target = next(i for i in self.data if i['id'] == issue['id'])['customFields']
        current = target[0]['value']['name']
        allowed = WORKFLOW.get(current, {})
        if event['id'] not in allowed:
            raise YouTrackError(f'YouTrack returned HTTP 400. No transition "{event["id"]}" from {current}.')
        if event['id'] == 'start' and not target[1]['value']:  # a guard: YouTrack ignores the transition
            return current
        target[0]['value'] = {'name': allowed[event['id']]}
        return allowed[event['id']]

    async def comments(self, issue):
        return sorted(deepcopy(self.threads.get(issue['id'], [])), key=lambda c: c['created'], reverse=True)

    async def comment(self, issue, text):
        self.threads.setdefault(issue['id'], []).append(demo_comment('alex', 0, False, None, text))

    async def close(self):
        pass
