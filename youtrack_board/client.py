import asyncio
import json
import logging
import time

from urllib.parse import quote, urlsplit

import httpx

from .diagnostics import LOGGER

ISSUE_FIELDS = "id,idReadable,summary,description,project(id,shortName),customFields(id,name,$type,value(id,name,login))"
BOARD_FIELDS = ("id,name,currentSprint(id),columnSettings(field(name),columns(presentation,ordinal,fieldValues(name))),"
                "projects(id,shortName,name),sprintsSettings(disableSprints),orphansAtTheTop,hideOrphansSwimlane,"
                "swimlaneSettings($type,enabled,field(id,name,presentation),values(name,presentation)),backlog(id,name,query)")
COMMENT_FIELDS = ("id,text,created,updated,deleted,author(login,fullName),"
                  "visibility($type,permittedGroups(name),permittedUsers(login,fullName))")


class YouTrackError(Exception):
    status = None  # the HTTP status when YouTrack answered with an error


def check_url(base_url):
    try:
        url = urlsplit(base_url)
        httpx.URL(base_url)
    except (ValueError, httpx.InvalidURL):
        raise YouTrackError('That is not a valid URL.') from None
    if url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1', '::1')):
        raise YouTrackError('Use an HTTPS YouTrack URL (HTTP is allowed only for localhost).')
    if not url.hostname or url.username or url.password or url.query or url.fragment:
        raise YouTrackError('Use a base URL without credentials, query or fragment.')


def search_literal(value):
    if any(char in value for char in '{}\\\r\n'):
        raise YouTrackError("Board/sprint names containing braces, backslashes or newlines are unsupported.")
    return '{' + value + '}'


def custom_field(issue, name):
    return next((f for f in issue['customFields'] if f['name'] == name), None)


def field_values(issue, name):
    field = custom_field(issue, name)
    value = field.get('value') if field else None
    return value if isinstance(value, list) else [value] if value else []


def movable_field(issue, name):
    field = custom_field(issue, name)
    return field if field and field.get('$type') in ('StateIssueCustomField', 'SingleEnumIssueCustomField') else None


def field_path(issue, field):
    return f'issues/{quote(issue["id"], safe="")}/fields/{quote(field["id"], safe="")}'


def assignable_field(issue):
    field = custom_field(issue, 'Assignee')
    return field if field and field.get('$type') == 'SingleUserIssueCustomField' else None


def group_issues(board, issues):
    settings = board.get('columnSettings') or {}
    field = (settings.get('field') or {}).get('name')
    columns = sorted(settings.get('columns') or [], key=lambda c: c.get('ordinal', 0))
    groups = [(c.get('presentation') or ', '.join(v['name'] for v in c['fieldValues']),
               {v['name'] for v in c['fieldValues']}, []) for c in columns]
    unmatched = []
    for issue in issues:
        values = {v.get('name') for v in field_values(issue, field)}
        matches = [cards for _, names, cards in groups if names & values]
        for cards in matches:
            cards.append(issue)
        if not matches:
            unmatched.append(issue)
    result = [(name, cards) for name, _, cards in groups]
    if unmatched:
        result.append(('Other / no column', unmatched))
    return result


def uses_sprints(board):
    """Only the board's settings tell: a board with sprints disabled still has an underlying currentSprint."""
    return not ((board or {}).get('sprintsSettings') or {}).get('disableSprints')


def project_lanes(board, issues):
    """(swimlane value, issues) per project swimlane in the board's order, plus (None, issues) for issues of
    projects without a swimlane, placed as the board places them. None when the board has no project swimlanes."""
    settings = board.get('swimlaneSettings') or {}
    if not (settings.get('enabled') and settings.get('$type') == 'AttributeBasedSwimlaneSettings'
            and (settings.get('field') or {}).get('id') == 'project'):
        return None
    # A value's name is the project's short name; its id belongs to the swimlane, not to the project.
    lanes = {value.get('name'): (value, []) for value in settings.get('values') or []}
    orphans = []
    for issue in issues:
        lanes.get((issue.get('project') or {}).get('shortName'), (None, orphans))[1].append(issue)
    rows = list(lanes.values())
    if orphans:
        rows.insert(0 if board.get('orphansAtTheTop') else len(rows), (None, orphans))
    return rows


class Client:
    def __init__(self, base_url, token, *, transport=None):
        check_url(base_url)
        if not token.strip():
            raise YouTrackError('A permanent YouTrack token is required.')
        self.base_url = base_url.rstrip('/')
        self.http = httpx.AsyncClient(base_url=self.base_url + '/api/',
                                     headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'},
                                     timeout=30, transport=transport, follow_redirects=False)

    async def request(self, method, path, **kwargs):
        endpoint = self.base_url + '/api/' + path.split('?')[0]
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
            raise YouTrackError('Cannot reach YouTrack. Check the URL and connection.') from exc
        LOGGER.log(logging.INFO if response.is_success else logging.ERROR, '← %s %s HTTP %s (%.0f ms)', method, endpoint,
                   response.status_code, (time.monotonic() - started) * 1000)
        if not response.is_success:
            message = f'YouTrack returned HTTP {response.status_code}.'
            try:
                detail = response.json().get('error_description', '')
                if detail:
                    message += ' ' + str(detail)[:300]
            except (ValueError, AttributeError):
                pass
            error = YouTrackError(message.replace(self.http.headers['Authorization'], '[redacted]')
                                  .replace(self.http.headers['Authorization'][7:], '[redacted]'))
            error.status = response.status_code
            raise error
        try:
            return response.json() if response.content else None
        except json.JSONDecodeError as exc:
            LOGGER.error('Invalid JSON response from %s', endpoint)
            raise YouTrackError('Expected JSON from YouTrack; check the base URL.') from exc

    async def pages(self, path, fields, **params):
        items = []
        while True:
            page = await self.request('GET', path, params={**params, 'fields': fields, '$top': 100, '$skip': len(items)})
            if not isinstance(page, list):
                raise YouTrackError('Expected a list from YouTrack; check the API URL.')
            if not page:
                return items
            items.extend(page)

    async def boards(self):
        return await self.pages('agiles', 'id,name,projects(id,shortName,name)')

    async def me(self):
        return await self.request('GET', 'users/me', params={'fields': 'id,login,fullName'})

    async def board(self, board_id):
        return await self.request('GET', f'agiles/{quote(board_id, safe="")}', params={'fields': BOARD_FIELDS})

    async def sprints(self, board_id):
        return await self.pages(f'agiles/{quote(board_id, safe="")}/sprints', 'id,name,archived,isDefault')

    async def issues(self, board, sprint):
        """The sprint's issues, or with sprint None (a board with sprints disabled) every issue on the board."""
        board_term = search_literal("Board " + board["name"])
        query = f'{board_term}: {search_literal(sprint["name"])}' if sprint else f'has: {board_term}'
        return await self.pages('issues', ISSUE_FIELDS, query=query)

    async def backlog(self, saved, skip, top):
        """Up to `top` issues of a board's backlog saved search from `skip`, and whether more follow. They come in the
        order YouTrack keeps for that search, including a manual order set on the board, which a query search drops."""
        if not (saved.get('query') or '').strip():  # such a search matches every issue the account can see
            raise YouTrackError(f'The backlog saved search "{saved.get("name")}" has no query, so it would list every issue '
                                'you can see. It was not loaded; give it a query in YouTrack.')
        page = await self.request('GET', f'savedQueries/{quote(saved["id"], safe="")}/issues',
                                  params={'fields': ISSUE_FIELDS, '$top': top + 1, '$skip': skip})
        if not isinstance(page, list):
            raise YouTrackError('Expected a list from YouTrack; check the API URL.')
        return page[:top], len(page) > top

    async def move(self, issue, field_name, value):
        field = movable_field(issue, field_name)
        if not field:
            raise YouTrackError('Moving supports single-value State and Enum board fields only.')
        await self.request('POST', f'issues/{quote(issue["id"], safe="")}', json={
            'customFields': [{'id': field['id'], '$type': field['$type'], 'value': {'name': value}}]})

    async def transitions(self, issue, field_name):
        """A state-machine field as YouTrack has it now, with the workflow transitions (possibleEvents) from its value."""
        return await self.request('GET', field_path(issue, custom_field(issue, field_name)),
                                  params={'fields': 'id,name,$type,value(name),possibleEvents(id,presentation)'})

    async def transition(self, issue, field, event):
        """Apply one of the field's possibleEvents and return the value YouTrack ended up with."""
        data = await self.request('POST', field_path(issue, field), params={'fields': 'value(name)'},
                                  json={'id': field['id'], '$type': 'StateMachineIssueCustomField', 'event': event})
        return ((data or {}).get('value') or {}).get('name')

    async def assignees(self, issue):
        """The issue project's Assignee settings, and every user its bundle holds directly or through groups."""
        data = await self.request('GET', f'issues/{quote(issue["id"], safe="")}', params={
            'fields': 'customFields(name,projectCustomField(canBeEmpty,emptyFieldText,bundle(id)))'})
        field = custom_field(data, 'Assignee') if isinstance(data, dict) and isinstance(data.get('customFields'), list) else None
        settings = (field or {}).get('projectCustomField') or {}
        bundle = (settings.get('bundle') or {}).get('id')
        if not bundle:
            raise YouTrackError(f'YouTrack did not return an Assignee user list for {issue["idReadable"]}.')
        users = await self.pages(f'admin/customFieldSettings/bundles/user/{quote(bundle, safe="")}/aggregatedUsers',
                                 'id,login,fullName,banned')
        return settings, users

    async def assign(self, issue, login):
        """Set the single-user Assignee field to a login, or clear it when login is empty."""
        field = assignable_field(issue)
        if not field:
            raise YouTrackError('Assigning supports single-user Assignee fields only.')
        await self.request('POST', f'issues/{quote(issue["id"], safe="")}', json={
            'customFields': [{'id': field['id'], '$type': field['$type'], 'value': {'login': login} if login else None}]})

    async def comments(self, issue):
        """Every comment the account may see, newest first. YouTrack itself leaves out comments hidden from it."""
        found = await self.pages(f'issues/{quote(issue["id"], safe="")}/comments', COMMENT_FIELDS)
        return sorted((c for c in found if isinstance(c, dict) and not c.get('deleted')),
                      key=lambda c: c.get('created') or 0, reverse=True)

    async def comment(self, issue, text):
        if not text.strip():
            raise YouTrackError('A comment cannot be empty.')
        await self.request('POST', f'issues/{quote(issue["id"], safe="")}/comments', json={'text': text.strip()})

    async def close(self):
        await self.http.aclose()
