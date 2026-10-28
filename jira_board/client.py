"""Jira Cloud REST client: boards, their configuration and sprints (Agile API), the issues on them (the enhanced
board endpoints, with cursor pagination), and the platform API for details, comments, assignment and transitions.

Only HTTPS, certificates verified, redirects never followed (credentials go to the configured site only). Reads are
retried at most twice when Jira rate-limits them; writes never are, as their outcome is then unknown.
"""
import asyncio
import base64
import json
import logging
import time
from urllib.parse import quote, urlsplit

import httpx

from issueboard.diagnostics import LOGGER

# What a card needs; the description is read only when the details expand.
CARD_FIELDS = 'summary,status,assignee,issuetype,priority,project'
RETRIES = 2  # rate-limited reads; a longer wait than MAX_WAIT is not waited for
MAX_WAIT = 10


class JiraError(Exception):
    status = None  # the HTTP status when Jira answered with an error


def check_url(url):
    """A Jira Cloud site address: https://example.atlassian.net, without a path, credentials, query or fragment."""
    try:
        parts = urlsplit(url)
        httpx.URL(url)
    except (ValueError, httpx.InvalidURL):
        raise JiraError('That is not a valid URL.') from None
    if parts.scheme != 'https':
        raise JiraError('Use the HTTPS address of the Jira site, e.g. https://example.atlassian.net.')
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ('', '/'):
        raise JiraError('Use the site address alone, e.g. https://example.atlassian.net, without a path, credentials, '
                        'query or fragment.')


def compact(issue):
    """The fields a card, the filters and grouping use, in a small stable shape (polls compare whole lists)."""
    fields = issue.get('fields') or {}
    status, assignee = fields.get('status') or {}, fields.get('assignee')
    return {'id': str(issue.get('id')), 'key': issue.get('key') or '', 'summary': fields.get('summary') or '',
            'status': {'id': str(status.get('id') or ''), 'name': status.get('name') or '',
                       'category': (status.get('statusCategory') or {}).get('key')},
            'assignee': {'accountId': assignee.get('accountId'), 'displayName': assignee.get('displayName') or ''}
            if isinstance(assignee, dict) else None,
            'type': (fields.get('issuetype') or {}).get('name') or '',
            'priority': (fields.get('priority') or {}).get('name') or '',
            'project': (fields.get('project') or {}).get('key') or ''}


def columns(config):
    """(column name, its status IDs) in board order. A column may hold many statuses, or none."""
    return [(c.get('name') or '', {str(s.get('id')) for s in c.get('statuses') or []})
            for c in ((config or {}).get('columnConfig') or {}).get('columns') or []]


UNMAPPED = 'Not on a board column'


def group_issues(config, issues):
    """(column, issues) in board order, keeping the server's rank order inside each column. Issues whose status no
    column holds (a sprint can contain them) are kept in a last column instead of being dropped."""
    found = [(name, ids, []) for name, ids in columns(config)]
    rest = []
    for issue in issues:
        target = next((cards for _, ids, cards in found if issue['status']['id'] in ids), rest)
        target.append(issue)
    return [(name, cards) for name, _, cards in found] + ([(UNMAPPED, rest)] if rest else [])


def required_fields(transition):
    """(fields Jira lets one choose from a list, names of other required fields) for a transition. The second ones
    need Jira's own form."""
    choosable, other = {}, []
    for field_id, field in (transition.get('fields') or {}).items():
        if not field.get('required'):
            continue
        if field.get('allowedValues') and (field.get('schema') or {}).get('type') != 'array':
            choosable[field_id] = field
        else:
            other.append(field.get('name') or field_id)
    return choosable, other


def adf_paragraphs(text):
    """Plain text as an Atlassian Document Format document: one paragraph per blank-line block, hard line breaks."""
    blocks = [b for b in text.strip().replace('\r\n', '\n').split('\n\n') if b.strip()]
    content = []
    for block in blocks:
        lines, inline = block.split('\n'), []
        for n, line in enumerate(lines):
            if n:
                inline.append({'type': 'hardBreak'})
            if line:
                inline.append({'type': 'text', 'text': line})
        content.append({'type': 'paragraph', 'content': inline})
    return {'type': 'doc', 'version': 1, 'content': content}


class Client:
    def __init__(self, base_url, email, token, *, transport=None):
        check_url(base_url)
        if not email.strip() or not token.strip():
            raise JiraError('An Atlassian account email and API token are required.')
        self.base_url = base_url.rstrip('/')
        basic = base64.b64encode(f'{email.strip()}:{token.strip()}'.encode()).decode()
        self.secrets = (token.strip(), basic)
        self.http = httpx.AsyncClient(base_url=self.base_url,
                                      headers={'Authorization': f'Basic {basic}', 'Accept': 'application/json'},
                                      timeout=30, transport=transport, follow_redirects=False)
        self.sleep = asyncio.sleep

    def redact(self, text):
        for secret in self.secrets:
            text = text.replace(secret, '[redacted]')
        return text

    async def request(self, method, path, **kwargs):
        endpoint = self.base_url + path
        for attempt in range(RETRIES + 1):
            started = time.monotonic()
            LOGGER.info('→ %s %s', method, endpoint)
            try:
                response = await self.http.request(method, path, **kwargs)
            except asyncio.CancelledError:
                LOGGER.debug('Cancelled %s %s', method, endpoint)
                raise
            except httpx.RequestError as exc:
                LOGGER.error('← %s %s failed: %s (%.0f ms)', method, endpoint, type(exc).__name__,
                             (time.monotonic() - started) * 1000)
                raise JiraError(f'Cannot reach Jira at {urlsplit(self.base_url).hostname}. Check the site URL and '
                                'your connection.') from exc
            LOGGER.log(logging.INFO if response.is_success else logging.ERROR, '← %s %s HTTP %s (%.0f ms)', method,
                       endpoint, response.status_code, (time.monotonic() - started) * 1000)
            wait = self.retry_after(response) if method == 'GET' and attempt < RETRIES else None
            if wait is None:
                break
            LOGGER.warning('Jira asked to slow down; retrying in %g s', wait)
            await self.sleep(wait)
        if not response.is_success:
            raise self.error(response)
        try:
            return response.json() if response.content else None
        except json.JSONDecodeError as exc:
            LOGGER.error('Invalid JSON response from %s', endpoint)
            raise JiraError('Expected JSON from Jira; check the site URL.') from exc

    @staticmethod
    def retry_after(response):
        """Seconds to wait before reading again, when Jira rate-limits and says how long is short enough."""
        if response.status_code not in (429, 503):
            return None
        try:
            wait = float(response.headers.get('Retry-After', 2))
        except ValueError:
            return None
        return max(0.0, wait) if wait <= MAX_WAIT else None

    def error(self, response):
        code = response.status_code
        if response.is_redirect:
            target = urlsplit(response.headers.get('Location', '')).hostname or 'another address'
            message = (f'Jira redirected to {target}; the request was not followed, so your credentials were not sent '
                       'there. Check the site URL.')
        elif code == 401:
            message = ('Jira rejected the email or API token (HTTP 401). Tokens "with scopes" do not work on the site '
                       'address; create a classic API token.')
        elif code == 429:
            message = 'Jira is rate-limiting requests (HTTP 429). Wait a minute, then press r.'
        else:
            message = f'Jira returned HTTP {code}.'
        try:
            data = response.json()
            details = [*(data.get('errorMessages') or []), *(f'{k}: {v}' for k, v in (data.get('errors') or {}).items())]
            if details and code != 401:
                message += ' ' + ' '.join(map(str, details))[:300]
        except (ValueError, AttributeError):
            pass
        error = JiraError(self.redact(message))
        error.status = code
        return error

    async def offset_pages(self, path, key, **params):
        """Every item of an offset-paginated list (startAt, maxResults, isLast or total)."""
        items = []
        while True:
            page = await self.request('GET', path, params={**params, 'startAt': len(items)})
            values = (page or {}).get(key) if isinstance(page, dict) else None
            if not isinstance(values, list):
                raise JiraError('Jira answered with an unexpected list; check the site URL.')
            items.extend(values)
            if not values or page.get('isLast') or len(items) >= page.get('total', float('inf')):
                return items

    async def token_page(self, path, token=None, **params):
        """One page of an enhanced (cursor-paginated) issue list: (compact issues, the next page's token or None)."""
        page = await self.request('GET', path, params={**params, **({'nextPageToken': token} if token else {})})
        if not isinstance(page, dict) or not isinstance(page.get('issues'), list):
            raise JiraError('Jira answered with an unexpected issue list; check the site URL.')
        following = None if page.get('isLast') else page.get('nextPageToken')
        return [compact(i) for i in page['issues'] if isinstance(i, dict)], following

    async def token_pages(self, path, **params):
        issues, token = [], None
        while True:
            page, following = await self.token_page(path, token, **params)
            issues.extend(page)
            if not following:
                return issues
            if following == token:
                raise JiraError('Jira repeated a page of issues; press r to retry.')
            token = following

    async def me(self):
        return await self.request('GET', '/rest/api/3/myself')

    async def boards(self, project=None):
        """Boards the account can see, of one project (key) or all, in Jira's order."""
        return await self.offset_pages('/rest/agile/1.0/board', 'values', maxResults=50,
                                       **({'projectKeyOrId': project} if project else {}))

    async def recent_projects(self):
        found = await self.request('GET', '/rest/api/3/project/recent', params={'maxResults': 20})
        return found if isinstance(found, list) else []

    async def board(self, board_id):
        """The board, its column configuration (columnConfig, subQuery) and sprints, or 'sprints': None without them."""
        path = f'/rest/agile/1.0/board/{quote(str(board_id), safe="")}'
        board = await self.request('GET', path)
        board['config'] = await self.request('GET', path + '/configuration')
        board['sprints'] = None
        if board.get('type') != 'kanban':
            try:
                board['sprints'] = await self.offset_pages(path + '/sprint', 'values', maxResults=50)
            except JiraError as exc:
                if exc.status != 400:  # "The board does not support sprints", e.g. a team-managed kanban board
                    raise
        return board

    async def issues(self, board, sprint=None):
        """Every issue of a sprint, or of a board without sprints, in rank order. A kanban board's sub-filter (such
        as hiding released work) is passed on as the JQL it is."""
        path = f'/rest/software/1.0/board/{quote(str(board["id"]), safe="")}'
        if sprint:
            return await self.token_pages(f'{path}/sprint/{quote(str(sprint["id"]), safe="")}/issue',
                                          fields=CARD_FIELDS, maxResults=100)
        sub = (((board.get('config') or {}).get('subQuery') or {}).get('query') or '').strip()
        return await self.token_pages(path + '/issue', fields=CARD_FIELDS, maxResults=100, **({'jql': sub} if sub else {}))

    async def backlog(self, board_id, token, top):
        """One page of the board's backlog, in rank order, and the next page's token."""
        return await self.token_page(f'/rest/software/1.0/board/{quote(str(board_id), safe="")}/backlog', token,
                                     fields=CARD_FIELDS, maxResults=top)

    def issue_path(self, issue):
        return f'/rest/api/3/issue/{quote(issue["key"], safe="")}'

    async def issue(self, key):
        """One issue by key, as a card has it."""
        found = await self.request('GET', self.issue_path({'key': key}), params={'fields': CARD_FIELDS})
        if not isinstance(found, dict) or not found.get('key'):
            raise JiraError('Jira answered with an unexpected issue; check the site URL.')
        return compact(found)

    async def links(self, issue):
        """The issue's parent (a sub-task's issue, an issue's epic…) and its issue links: Jira's fields of those names."""
        found = await self.request('GET', self.issue_path(issue), params={'fields': 'parent,issuelinks'})
        fields = found.get('fields') if isinstance(found, dict) else None
        return fields if isinstance(fields, dict) else {}

    async def description(self, issue):
        data = await self.request('GET', self.issue_path(issue), params={'fields': 'description'})
        return ((data or {}).get('fields') or {}).get('description')

    async def comments(self, issue):
        """Every comment the account may see, newest first."""
        found = await self.offset_pages(self.issue_path(issue) + '/comment', 'comments', maxResults=100,
                                        orderBy='-created')
        return sorted((c for c in found if isinstance(c, dict)), key=lambda c: c.get('created') or '', reverse=True)

    async def comment(self, issue, text):
        if not text.strip():
            raise JiraError('A comment cannot be empty.')
        await self.request('POST', self.issue_path(issue) + '/comment', json={'body': adf_paragraphs(text)})

    async def assignable(self, issue, me=None, pages=10):
        """(active people who can be assigned the issue, whether the list was cut short). Jira may return short pages
        before the end, so only an empty page ends the list; a cut-short list still includes `me` if eligible."""
        users, path = [], '/rest/api/3/user/assignable/search'
        for n in range(pages):
            page = await self.request('GET', path, params={'issueKey': issue['key'], 'startAt': n * 100, 'maxResults': 100})
            if not isinstance(page, list):
                raise JiraError('Jira answered with an unexpected user list.')
            if not page:
                break
            users.extend(u for u in page if isinstance(u, dict) and u.get('accountId') and u.get('active', True))
        else:
            if me and all(u['accountId'] != me for u in users):
                found = await self.request('GET', path, params={'issueKey': issue['key'], 'accountId': me})
                users.extend(u for u in found or [] if isinstance(u, dict) and u.get('accountId') == me)
            return list({u['accountId']: u for u in users}.values()), True
        return list({u['accountId']: u for u in users}.values()), False

    async def assign(self, issue, account_id):
        """Assign to an account, or unassign with None."""
        await self.request('PUT', self.issue_path(issue) + '/assignee', json={'accountId': account_id})

    async def transitions(self, issue):
        data = await self.request('GET', self.issue_path(issue) + '/transitions', params={'expand': 'transitions.fields'})
        return [t for t in (data or {}).get('transitions') or [] if isinstance(t, dict) and t.get('id')]

    async def transition(self, issue, transition_id, fields=None):
        """Apply a workflow transition by ID, with values for its required fields ({field id: allowed value id})."""
        body = {'transition': {'id': transition_id}}
        if fields:
            body['fields'] = {k: {'id': v} for k, v in fields.items()}
        await self.request('POST', self.issue_path(issue) + '/transitions', json=body)

    async def close(self):
        await self.http.aclose()
