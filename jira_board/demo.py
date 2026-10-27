"""In-memory Jira Cloud for --demo and tests: a scrum board with several statuses per column and a long backlog, a
kanban board with an empty column and a sub-filter, and a scrum board without an active sprint."""
import time
from copy import deepcopy

from .client import JiraError, compact

STATUSES = {'1': ('To Do', 'new'), '2': ('Selected', 'new'), '3': ('In Progress', 'indeterminate'),
            '4': ('In Review', 'indeterminate'), '5': ('Blocked', 'indeterminate'), '6': ('Done', 'done'),
            '7': ("Won't Do", 'done'), '8': ('Ready', 'new'), '9': ('Draft', 'new')}
PEOPLE = {key: {'accountId': f'acc-{key}', 'displayName': name, 'active': key != 'kim', 'accountType': 'atlassian'}
          for key, name in [('alex', 'Alex Morgan'), ('jordan', 'Jordan Lee'), ('sam', 'Sam Rivera'),
                            ('priya', 'Priya Shah'), ('kim', 'Kim Park')]}
ASSIGNABLE = {'WEB': ['alex', 'jordan', 'sam', 'kim'], 'DES': ['alex', 'priya'], 'MOB': ['sam', 'priya', 'alex']}
PROJECTS = {'WEB': 'Website', 'DES': 'Design system', 'MOB': 'Mobile app'}
# Board id: (name, type, project, columns of (name, status ids), sub-filter)
BOARDS = {
    '1': ('Web team', 'scrum', 'WEB', [('To Do', ['1', '2']), ('In Progress', ['3', '5']), ('In Review', ['4']),
                                         ('Done', ['6', '7'])], None),
    '2': ('Design', 'kanban', 'DES', [('Backlog', []), ('Draft', ['9']), ('Ready', ['8']), ('In Progress', ['3']),
                                       ('Done', ['6'])], 'fixVersion in unreleasedVersions() OR fixVersion is EMPTY'),
    '3': ('Mobile', 'scrum', 'MOB', [('To Do', ['1']), ('In Progress', ['3']), ('Done', ['6'])], None),
}
SPRINTS = {'1': [(13, 'Sprint 13', 'closed'), (14, 'Sprint 14', 'active'), (15, 'Sprint 15', 'future')],
           '2': None, '3': [(2, 'Mobile 2', 'closed'), (3, 'Mobile 3', 'future')]}
# (project, summary, status, assignee, type, priority, sprint or board for kanban, released: hidden by the sub-filter)
ISSUES = [
    ('WEB', 'Keyboard navigation for the board', '1', 'alex', 'Story', 'High', 14),
    ('WEB', 'Choose the sprint to show', '2', 'jordan', 'Story', 'Medium', 14),
    ('WEB', 'Connect with an API token', '3', 'alex', 'Task', 'High', 14),
    ('WEB', 'Rate limits slow the board down', '5', 'sam', 'Bug', 'Highest', 14),
    ('WEB', 'Render Atlassian documents', '4', None, 'Story', 'Medium', 14),
    ('WEB', 'Old export format', '7', 'jordan', 'Task', 'Low', 14),
    ('WEB', 'Ship the beta', '6', 'alex', 'Story', 'Medium', 14),
    ('WEB', 'A status no column maps', '9', 'sam', 'Task', 'Low', 14),
    ('WEB', 'Sign-in page', '6', 'jordan', 'Story', 'Medium', 13),
    ('DES', 'Colour tokens for dark mode', '9', 'priya', 'Task', 'Medium', 'kanban'),
    ('DES', 'Button states', '8', None, 'Story', 'High', 'kanban'),
    ('DES', 'Icon set refresh', '3', 'alex', 'Story', 'Medium', 'kanban'),
    ('DES', 'Typography scale', '6', 'priya', 'Task', 'Low', 'kanban'),
    ('DES', 'Released last year: logo files', '6', 'priya', 'Task', 'Low', 'released'),
    ('MOB', 'Offline tickets', '1', 'sam', 'Story', 'High', 3),
    ('MOB', 'Push opt-in', '1', None, 'Story', 'Medium', 3),
]
ACTIONS = ['Polish', 'Speed up', 'Document', 'Add tests for', 'Simplify', 'Localize']
AREAS = ['the login form', 'search results', 'the sprint report', 'CSV export', 'email notifications',
         'the mobile menu', 'the settings page', 'image uploads', 'the onboarding tour', 'error messages']
RESOLUTION = {'required': True, 'name': 'Resolution', 'schema': {'type': 'resolution', 'system': 'resolution'},
              'allowedValues': [{'id': '10000', 'name': 'Done'}, {'id': '10001', 'name': "Won't Do"},
                                {'id': '10002', 'name': 'Duplicate'}], 'hasDefaultValue': False}
WORKLOG = {'required': True, 'name': 'Time spent', 'schema': {'type': 'array', 'system': 'worklog'}}
# From status: [(transition id, name, to status, required fields)]
WORKFLOW = {
    '1': [('11', 'Select for development', '2', {}), ('21', 'Start progress', '3', {})],
    '2': [('21', 'Start progress', '3', {}), ('12', 'Deselect', '1', {})],
    '3': [('31', 'Send to review', '4', {}), ('32', 'Block', '5', {}),
          ('41', 'Done', '6', {'resolution': RESOLUTION}), ('42', 'Log work and close', '6', {'worklog': WORKLOG})],
    '4': [('41', 'Done', '6', {'resolution': RESOLUTION}), ('33', 'Request changes', '3', {})],
    '5': [('34', 'Unblock', '3', {})],
    '6': [('51', 'Reopen', '1', {})], '7': [('51', 'Reopen', '1', {})],
    '8': [('21', 'Start progress', '3', {})], '9': [('52', 'Ready for development', '8', {})],
}


def para(*inline):
    return {'type': 'paragraph', 'content': list(inline)}


def txt(value, *marks):
    return {'type': 'text', 'text': value, **({'marks': [{'type': m} if isinstance(m, str) else m for m in marks]}
                                               if marks else {})}


DESCRIPTION = {'type': 'doc', 'version': 1, 'content': [
    {'type': 'heading', 'attrs': {'level': 2}, 'content': [txt('Acceptance')]},
    para(txt('Arrow keys move between cards and columns; '), txt('Enter', 'code'), txt(' expands the details.')),
    {'type': 'taskList', 'content': [
        {'type': 'taskItem', 'attrs': {'state': 'DONE'}, 'content': [txt('Columns from the board configuration')]},
        {'type': 'taskItem', 'attrs': {'state': 'TODO'}, 'content': [txt('Several statuses in one column, '),
                                                                    txt('as Jira maps them', 'em')]}]},
    {'type': 'panel', 'attrs': {'panelType': 'warning'}, 'content': [para(txt('Keep it usable at 80×24.', 'strong'))]},
    {'type': 'table', 'content': [
        {'type': 'tableRow', 'content': [{'type': 'tableHeader', 'content': [para(txt('Key'))]},
                                         {'type': 'tableHeader', 'content': [para(txt('Shows'))]}]},
        {'type': 'tableRow', 'content': [{'type': 'tableCell', 'content': [para(txt('s', 'code'))]},
                                         {'type': 'tableCell', 'content': [para(txt('The source as text'))]}]},
        {'type': 'tableRow', 'content': [{'type': 'tableCell', 'content': [para(txt('o', 'code'))]},
                                         {'type': 'tableCell', 'content': [para(txt('The issue in Jira, with its images'))]}]}]},
    {'type': 'codeBlock', 'attrs': {'language': 'sh'}, 'content': [txt('issueboard --demo jira')]},
    {'type': 'mediaSingle', 'content': [{'type': 'media', 'attrs': {'type': 'file', 'id': 'f1', 'alt': 'keyboard-map.png'}}]},
    para({'type': 'mention', 'attrs': {'id': 'acc-alex', 'text': '@Alex Morgan'}}, txt(' wrote '),
         txt('the guide', {'type': 'link', 'attrs': {'href': 'https://example.com/guide'}}), txt(', reviewed on '),
         {'type': 'date', 'attrs': {'timestamp': '1790000000000'}}, txt(' '), {'type': 'status', 'attrs': {'text': 'APPROVED'}},
         txt('. Rich markup like [b]tags[/b] and *stars* stay literal.')),
    {'type': 'someFutureNode', 'attrs': {}},
]}
# Issue key: [(author, hours ago, edited, visible only to role, text)], oldest first.
COMMENTS = {
    'WEB-3': [('jordan', 50, False, None, 'Basic auth with an API token works against the staging site.'),
              ('sam', 26, False, 'Developers', 'Only developers can see this comment.'),
              ('alex', 3, True, None, 'Pagination now follows nextPageToken until isLast.')],
    'DES-1': [('priya', 5, False, None, 'Dark tokens are ready for review.')],
}


def comment(author, hours, edited, role, text):
    created = time.strftime('%Y-%m-%dT%H:%M:%S.000+0000', time.gmtime(time.time() - hours * 3600))
    updated = time.strftime('%Y-%m-%dT%H:%M:%S.000+0000', time.gmtime(time.time() - hours * 3600 + 60)) if edited else created
    return {'id': f'c-{author}-{hours}', 'author': deepcopy(PEOPLE[author]), 'created': created, 'updated': updated,
            'body': {'type': 'doc', 'version': 1, 'content': [para(txt(text))]},
            **({'visibility': {'type': 'role', 'value': role}} if role else {})}


def demo_state():
    """A returning user's visits, so the board opened last opens again. WEB's GitLab repository is chosen already,
    in the GitLab board demo's offline GitLab; DES's is not, so g on a DES issue asks for one."""
    now = time.time()
    return {'visited': {'1': now - 300, '2': now - 86400}, 'repos': {'WEB': 'https://gitlab.example.com/demo/api'}}


class DemoClient:
    base_url = 'https://example.atlassian.net'

    def __init__(self):
        self.data, counters = [], dict.fromkeys(PROJECTS, 0)

        def issue(project, summary, status, person, kind, priority, where):
            counters[project] += 1
            n = len(self.data) + 1
            name, category = STATUSES[status]
            self.data.append({'id': str(10000 + n), 'key': f'{project}-{counters[project]}', 'where': where,
                              'description': DESCRIPTION if n == 1 else None,
                              'fields': {'summary': summary, 'status': {'id': status, 'name': name,
                                                                         'statusCategory': {'key': category}},
                                         'assignee': deepcopy(PEOPLE[person]) if person else None,
                                         'issuetype': {'name': kind}, 'priority': {'name': priority},
                                         'project': {'key': project}}})
        for item in ISSUES:
            issue(*item)
        for n, summary in enumerate(f'{a} {b}' for a in ACTIONS for b in AREAS):  # 60 backlog issues per scrum board
            issue('WEB', summary, '1', [None, 'alex', 'sam'][n % 3], 'Task', 'Medium', 'backlog')
            if n < 4:
                issue('MOB', summary, '1', None, 'Task', 'Low', 'backlog')
        self.threads = {key: [comment(*c) for c in thread] for key, thread in COMMENTS.items()}

    def find(self, issue):
        return next(i for i in self.data if i['key'] == issue['key'])

    async def me(self):
        return deepcopy(PEOPLE['alex'])

    async def boards(self, project=None):
        return [{'id': int(board_id), 'name': name, 'type': kind,
                 'location': {'projectKey': key, 'projectName': PROJECTS[key], 'displayName': PROJECTS[key]}}
                for board_id, (name, kind, key, _, _) in BOARDS.items() if not project or project == key]

    async def recent_projects(self):
        return [{'key': key, 'name': name} for key, name in PROJECTS.items()]

    async def board(self, board_id):
        if board_id not in BOARDS:
            raise JiraError('Jira returned HTTP 404. The board does not exist or you do not have permission to view it.')
        name, kind, key, cols, sub = BOARDS[board_id]
        sprints = SPRINTS[board_id]
        return {'id': int(board_id), 'name': name, 'type': kind, 'location': {'projectKey': key},
                'config': {'columnConfig': {'columns': [{'name': c, 'statuses': [{'id': s} for s in ids]} for c, ids in cols]},
                           **({'subQuery': {'query': sub}} if sub else {})},
                'sprints': None if sprints is None else [{'id': n, 'name': s, 'state': state} for n, s, state in sprints]}

    async def issues(self, board, sprint=None):
        project = BOARDS[str(board['id'])][2]
        wanted = sprint['id'] if sprint else 'kanban'
        return [compact(i) for i in self.data if i['fields']['project']['key'] == project and i['where'] == wanted]

    async def backlog(self, board_id, token, top):
        if BOARDS[str(board_id)][1] == 'kanban':
            error = JiraError("Jira returned HTTP 400. The board does not have a backlog.")
            error.status = 400
            raise error
        items = [compact(i) for i in self.data if i['fields']['project']['key'] == BOARDS[str(board_id)][2]
                 and i['where'] == 'backlog']
        start = int(token or 0)
        return items[start:start + top], str(start + top) if start + top < len(items) else None

    async def description(self, issue):
        return deepcopy(self.find(issue)['description'])

    async def comments(self, issue):
        return sorted(deepcopy(self.threads.get(issue['key'], [])), key=lambda c: c['created'], reverse=True)

    async def comment(self, issue, text):
        self.threads.setdefault(issue['key'], []).append(comment('alex', 0, False, None, text.strip()))

    async def assignable(self, issue, me=None):
        return [deepcopy(PEOPLE[p]) for p in ASSIGNABLE[issue['key'].split('-')[0]] if PEOPLE[p]['active']], False

    async def assign(self, issue, account_id):
        person = next((p for p in PEOPLE.values() if p['accountId'] == account_id), None)
        self.find(issue)['fields']['assignee'] = deepcopy(person)

    async def transitions(self, issue):
        status = self.find(issue)['fields']['status']['id']
        return [{'id': tid, 'name': name, 'to': {'id': to, 'name': STATUSES[to][0]}, 'fields': deepcopy(fields)}
                for tid, name, to, fields in WORKFLOW.get(status, [])]

    async def transition(self, issue, transition_id, fields=None):
        target = self.find(issue)['fields']
        found = next((t for t in WORKFLOW.get(target['status']['id'], []) if t[0] == transition_id), None)
        if not found:
            raise JiraError(f'Jira returned HTTP 400. Transition id {transition_id} is not valid for this issue.')
        missing = [f['name'] for k, f in found[3].items() if k not in (fields or {})]
        if missing:
            raise JiraError(f'Jira returned HTTP 400. {", ".join(missing)} is required.')
        name, category = STATUSES[found[2]]
        target['status'] = {'id': found[2], 'name': name, 'statusCategory': {'key': category}}

    async def close(self):
        pass
