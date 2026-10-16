"""What a GitLab board URL asks for: the host, the project or group, the board, and its filters.

The query string is read the way GitLab's board page reads it (boards/index.js: queryToObject with gatherArrays):
keys ending in [] collect every value, and any other key that repeats keeps only its last value. The board's own scope
is then added the way the page adds it (ee/boards/boards_util.js transformBoardConfig), and the result becomes the
GraphQL BoardIssueInput the way the page builds it (boards_util.js filterVariables). Parameters the page would drop
silently are refused here instead, so the board never shows more than the URL meant without saying so.
See docs/research/gitlab-board.md.
"""
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

SEGMENT = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.\-]*|\.[A-Za-z0-9_][A-Za-z0-9_.\-]*')
MILESTONE_WILDCARDS = ('Any', 'None', 'Started', 'Upcoming')


class TargetError(ValueError):
    pass


@dataclass(frozen=True)
class Target:
    host: str
    path: str  # the project's or group's full path
    kind: str = ''  # 'project' or 'group'; empty when only GitLab can tell (a plain namespace URL)
    board: int | None = None
    params: dict = field(default_factory=dict)  # query values: a string, or a list for keys ending in []
    repeated: dict = field(default_factory=dict)  # every value of a key GitLab reduces to its last one

    @property
    def place(self):
        """The namespace part of a web path: 'group/project' or 'groups/group'."""
        return ('groups/' if self.kind == 'group' else '') + self.path

    @property
    def key(self):
        """The board's web path without the host, as saved in local state."""
        return f'{self.place}/-/boards/{self.board}' if self.board else self.place


def decode(text):
    """decodeUrlParameter in GitLab's url_utility.js: '+' is a space, then percent-decoding."""
    try:
        return unquote(text.replace('+', '%20'), errors='strict')
    except UnicodeDecodeError:
        raise TargetError('The URL has a malformed percent-encoded value.') from None


def query_params(query):
    """(params, repeated): queryToObject(query, {gatherArrays: true}) and the keys whose earlier values it dropped."""
    params, repeated = {}, {}
    for part in query.split('&'):
        pieces = part.split('=')
        if not part or len(pieces) < 2:  # like the page: a key without '=' is ignored
            continue
        key, value = decode(pieces[0]), decode(pieces[1])  # like JS split('='): text after a second '=' is dropped
        if key.endswith('[]'):
            values = params.get(key[:-2])
            params[key[:-2]] = (values if isinstance(values, list) else []) + [value]
        else:
            repeated.setdefault(key, []).append(value)
            params[key] = value
    return params, {k: v for k, v in repeated.items() if len(v) > 1}


def parse(text, host='gitlab.com'):
    """A Target from a board, project or group URL, or from a bare 'group/project' path on `host`."""
    text = text.strip()
    if '://' in text:
        url = urlsplit(text)
        if url.scheme not in ('https', 'http') or not url.hostname:
            raise TargetError('Use an https:// GitLab URL.')
        if url.username or url.password:
            raise TargetError('Remove the user name or password from the URL.')
        host = url.hostname + (f':{url.port}' if url.port else '')
        path, query = url.path, url.query  # the #fragment is the page's own router state (#/), not a filter
    else:
        path, _, query = text.partition('?')
    parts = [decode(p) for p in path.strip('/').split('/') if p]
    kind = ''
    if parts[:1] == ['groups']:
        kind, parts = 'group', parts[1:]
    board = None
    if '-' in parts:
        at = parts.index('-')
        route, parts = parts[at + 1:], parts[:at]
        if route[:1] != ['boards'] or len(route) > 2 or (len(route) == 2 and not route[1].isdigit()):
            raise TargetError('Not an issue board URL. Open a board (…/-/boards/<id>), or a project or group.')
        board = int(route[1]) if len(route) == 2 else None
        kind = kind or 'project'
    if not parts or not all(SEGMENT.fullmatch(p) and '..' not in p for p in parts):
        raise TargetError('The URL does not name a GitLab project or group.')
    params, repeated = query_params(query)
    return Target(host.lower(), '/'.join(parts), kind, board, params, repeated)


def gid(kind, value):
    return value if str(value).startswith('gid://') else f'gid://gitlab/{kind}/{value}'


def upper(value):
    return value.upper() if isinstance(value, str) else value


# FiltersInfo (boards_util.js, extended in ee/boards/boards_util.js): per filter, whether not[…] is supported, the
# key or[…] maps to, a key change that depends on the value, and a value transform.
INFO = {
    'assigneeUsername': dict(neg=True, union='assigneeUsernames', remap=lambda v: 'assigneeWildcardId' if v == 'Any' else None),
    'assigneeId': dict(remap=lambda v: 'assigneeWildcardId'),
    'assigneeWildcardId': dict(transform=upper),
    'authorUsername': dict(neg=True, union='authorUsernames'),
    'authorUsernames': dict(neg=True),
    'labelName': dict(neg=True, union='labelNames'),
    'milestoneTitle': dict(neg=True, remap=lambda v: 'milestoneWildcardId' if v in MILESTONE_WILDCARDS else None),
    'milestoneWildcardId': dict(neg=True, transform=upper),
    'myReactionEmoji': dict(neg=True),
    'releaseTag': dict(neg=True),
    'type': dict(remap=lambda v: 'workItemTypeIds'),
    'types': dict(remap=lambda v: 'workItemTypeIds'),
    'workItemTypeIds': dict(neg=True, transform=lambda v: [gid('WorkItems::Type', i) for i in v]
                            if isinstance(v, list) else gid('WorkItems::Type', v)),
    'confidential': dict(transform=lambda v: v == 'yes'),
    'search': dict(),
    'status': dict(transform=lambda v: {'name': v}),
    'epicId': dict(neg=True, transform=lambda v: gid('Epic', v),
                   remap=lambda v: 'epicWildcardId' if v in ('Any', 'None') else None),
    'epicWildcardId': dict(transform=upper),
    'iterationId': dict(neg=True, transform=lambda v: gid('Iteration', v),
                        remap=lambda v: 'iterationWildcardId' if isinstance(v, str)
                        and v.endswith(('Any', 'None', 'Current')) else None),
    'iterationWildcardId': dict(neg=True, transform=lambda v: v.split('/')[-1].upper()),
    'iterationCadenceId': dict(transform=lambda v: gid('Iterations::Cadence', v)),
    'weight': dict(neg=True),
    'healthStatus': dict(neg=True, remap=lambda v: 'healthStatusFilter',
                         transform=lambda v: upper(v) if v in ('Any', 'None') else v),
    'healthStatusFilter': dict(neg=True, transform=lambda v: upper(v) if v in ('Any', 'None') else v),
}
# FilterFields for issues (boards/constants.js plus ee/boards/constants.js): what a board query accepts.
FIELDS = {'assigneeUsername', 'assigneeUsernames', 'assigneeWildcardId', 'authorUsername', 'authorUsernames',
          'confidential', 'labelName', 'labelNames', 'milestoneTitle', 'milestoneWildcardId', 'myReactionEmoji',
          'releaseTag', 'search', 'weight', 'workItemTypeIds', 'status', 'epicId', 'epicWildcardId', 'iterationId',
          'iterationWildcardId', 'iterationCadenceId', 'healthStatusFilter'}
ENUMS = {'assigneeWildcardId', 'milestoneWildcardId', 'epicWildcardId', 'iterationWildcardId', 'healthStatusFilter'}


def camel(key):
    return re.sub(r'_([a-z0-9])', lambda m: m.group(1).upper(), key)


def scoped(params, board):
    """URL params with the board's scope (Premium) added as GitLab's page adds it: appended to the query string, so for
    single-value filters the scope's value wins, and its labels join the URL's. Returns (params, notes)."""
    params, notes = dict(params), []

    def put(key, value):
        if value is None or value == '':
            return
        value = str(value)
        if key in params and params[key] != value:
            shown = ', '.join(params[key]) if isinstance(params[key], list) else params[key]
            notes.append(f'The board’s scope sets {key} to “{value}” and replaces the URL’s “{shown}”, as on GitLab.')
        params[key] = value

    title = (board.get('milestone') or {}).get('title')
    put('milestone_title', title)
    iteration, cadence = (board.get('iteration') or {}).get('id'), (board.get('iterationCadence') or {}).get('id')
    number = str(iteration or '').rsplit('/', 1)[-1]
    if number in ('0', '-1', '-4'):
        put('iteration_id', {'0': 'None', '-1': 'Any', '-4': 'Current'}[number])
        if number != '0':
            put('iteration_cadence_id', str(cadence or '').rsplit('/', 1)[-1])
    elif iteration:
        put('iteration_id', number)
    weight = board.get('weight')
    if weight is not None and weight != -1:
        put('weight', 'None' if weight == -2 else weight)
    put('assignee_username', (board.get('assignee') or {}).get('username'))
    labels = params.get('label_name') or []
    labels = labels if isinstance(labels, list) else [labels]
    extra = [label['title'] for label in (board.get('labels') or {}).get('nodes') or [] if label['title'] not in labels]
    if extra:
        params['label_name'] = labels + extra
    return params, notes


def filters(params):
    """The BoardIssueInput for these params, as GitLab's board page builds it. Raises TargetError naming every
    parameter the page would ignore, because ignoring it would show issues the URL meant to hide."""
    result, refused = {}, []
    for raw, value in params.items():
        if raw == 'group_by':  # swimlanes, not a filter
            continue
        negated, unioned = raw.startswith('not[') and raw.endswith(']'), raw.startswith('or[') and raw.endswith(']')
        key = camel(raw[4:-1] if negated else raw[3:-1] if unioned else raw)
        info = INFO.get(key, {})
        if unioned:
            key = info.get('union', key)
            if key not in FIELDS or key not in {'assigneeUsernames', 'authorUsernames', 'labelNames'}:
                refused.append(raw)
                continue
            result.setdefault('or', {})[key] = value
            continue
        key = info['remap'](value) or key if 'remap' in info else key
        info = INFO.get(key, {})
        if key not in FIELDS or (negated and not info.get('neg')):
            refused.append(raw)
            continue
        if 'transform' in info:
            value = info['transform'](value)
        (result.setdefault('not', {}) if negated else result)[key] = value
    if refused:
        raise TargetError('This app does not apply these board URL filters: ' + ', '.join(refused)
                          + '. Remove them from the URL, or open the board in GitLab.')
    return result


def describe(found):
    """The server-side filters in a few words, for the toolbar."""
    words = {'labelName': 'label', 'milestoneTitle': 'milestone', 'milestoneWildcardId': 'milestone',
             'assigneeUsername': 'assignee', 'assigneeWildcardId': 'assignee', 'authorUsername': 'author',
             'iterationId': 'iteration', 'iterationWildcardId': 'iteration', 'epicId': 'epic',
             'epicWildcardId': 'epic', 'search': 'search', 'confidential': 'confidential', 'weight': 'weight',
             'releaseTag': 'release', 'myReactionEmoji': 'reaction', 'status': 'status', 'workItemTypeIds': 'type',
             'healthStatusFilter': 'health', 'iterationCadenceId': 'cadence', 'authorUsernames': 'author'}

    def text(key, value):
        value = value.get('name') if isinstance(value, dict) else value
        values = value if isinstance(value, list) else [value]
        return f'{words.get(key, key)} ' + ', '.join(str(v).rsplit('/', 1)[-1] if str(v).startswith('gid://') else str(v)
                                                     for v in values)

    parts = [text(k, v) for k, v in found.items() if k not in ('not', 'or')]
    parts += ['not ' + text(k, v) for k, v in (found.get('not') or {}).items()]
    parts += ['any of ' + text(k, v) for k, v in (found.get('or') or {}).items()]
    return ' · '.join(parts)
