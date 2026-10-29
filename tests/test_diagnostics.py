import httpx
import pytest

from gitlab_board.glab import GlabError
from gitlab_board.app import GitLabApp, Prefs as GitLabPrefs
from gitlab_board.demo import DemoClient as GitLabDemo, demo_state
from gitlab_board.target import parse
from test_board import settle
from test_gitlab import fake_glab
from issueboard.widgets import CommentDialog
from youtrack_board.app import BoardApp
from youtrack_board.client import Client, YouTrackError
from youtrack_board.demo import DemoClient
from issueboard.diagnostics import LOGGER, LogBuffer, LogPanel


@pytest.fixture
def logs():
    buffer = LogBuffer(['opaque-test-secret'])
    LOGGER.addHandler(buffer)
    yield buffer
    LOGGER.removeHandler(buffer)


def lines(buffer):
    return '\n'.join(text for _, text in buffer.lines)


def test_log_buffer_is_bounded_and_redacts_secrets_and_controls(logs):
    LOGGER.warning('opaque-test-secret Bearer hidden [bold]literal[/bold]\x1b\n')
    text = lines(logs)
    assert 'opaque-test-secret' not in text and 'hidden' not in text and '[redacted]' in text
    assert '\x1b' not in text and '\n' not in text and '[bold]literal[/bold]' in text
    for n in range(1100):
        LOGGER.info('event %s', n)
    assert len(logs.lines) == 1000 and 'event 1099' in logs.lines[-1][1]


@pytest.mark.parametrize('gitlab', [False, True])
@pytest.mark.parametrize('size', [(80, 24), (60, 20)])
async def test_verbose_panel_logs_hidden_activity_and_toggles_without_typing_conflicts(gitlab, size):
    if gitlab:
        client = GitLabDemo()
        state = demo_state()
        app = GitLabApp(client, GitLabPrefs(None, client.host, state), start=parse(state['project'], client.host), verbose=True)
    else:
        app = BoardApp(DemoClient(), verbose=True)
    buffer = app.log_buffer
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        panel = app.query_one(LogPanel)
        app.status('A request failed', error=True)
        app.notify('Retrying soon', severity='warning')
        app.drain_logs()
        text = '\n'.join(line.text for line in panel.lines)
        assert 'ERROR A request failed' in text and 'WARNING Retrying soon' in text
        assert panel.display and panel.region.width <= size[0] and panel.region.height < size[1]
        await pilot.press('f12')
        assert not panel.display
        LOGGER.info('activity while hidden')
        app.drain_logs()
        await pilot.press('f12')
        assert panel.display and 'activity while hidden' in '\n'.join(line.text for line in panel.lines)
        await pilot.press('enter')
        await settle(app, pilot)
        await pilot.press('f12', 'f12')
        assert panel.display and panel.region.height > 0
        await pilot.press('escape')
        await pilot.press('slash', 'l', 'o', 'g')
        assert app.query_one('#filter').value == 'log'
        await pilot.press('escape')
        # F12 must also work while a dialog has the keyboard.
        app.push_screen(CommentDialog('Test'))
        await pilot.pause()
        await pilot.press('f12')
        assert not panel.display
        await pilot.press('f12', 'escape')
        assert panel.display
        for n in range(1100):
            LOGGER.info('entry %s', n)
        app.drain_logs()
        assert len(panel.lines) <= 1000
    assert buffer not in LOGGER.handlers


async def test_normal_mode_does_not_collect_logs():
    app = BoardApp(DemoClient())
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        assert not app.query(LogPanel) and app.log_buffer is None
        await pilot.press('f12')
        assert not app.query(LogPanel)


async def test_log_timer_can_tick_after_panel_is_removed_during_shutdown():
    app = BoardApp(DemoClient(), verbose=True)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await app.query_one(LogPanel).remove()
        LOGGER.info('shutdown in progress')
        app.drain_logs()


async def test_youtrack_requests_show_endpoint_status_and_timing_without_payloads(logs):
    statuses = iter([200, 403, 200])
    def respond(request):
        status = next(statuses)
        return httpx.Response(status, json={'error_description': 'opaque-test-secret'} if status == 403 else {})
    client = Client('https://example.youtrack.cloud', 'opaque-test-secret', transport=httpx.MockTransport(respond))
    await client.request('GET', 'issues', params={'query': 'private search'})
    with pytest.raises(YouTrackError):
        await client.request('POST', 'issues/1/comments', json={'text': 'private body'})
    text = lines(logs)
    assert '→ GET https://example.youtrack.cloud/api/issues' in text
    assert 'HTTP 200' in text and 'HTTP 403' in text and 'ms)' in text
    assert all(secret not in text for secret in ('opaque-test-secret', 'private search', 'private body', 'Authorization'))
    await client.close()
    offline = Client('https://example.youtrack.cloud', 'opaque-test-secret',
                     transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError('secret'))))
    with pytest.raises(YouTrackError):
        await offline.request('GET', 'users/me')
    assert 'ConnectError' in lines(logs) and 'secret' not in lines(logs)
    await offline.close()


async def test_glab_logs_requests_and_failures_without_stdin_queries_or_stderr(logs, fake_glab, monkeypatch):
    runner, _ = fake_glab
    monkeypatch.setenv('FAKE_OUT', '{}')
    await runner.get('projects/team%2Fapp/issues', search='private-search')
    await runner.post('projects/team%2Fapp/issues/1/notes', {'body': 'private body'})
    monkeypatch.setenv('FAKE_OUT', '{"data": {"ok": true}}')
    await runner.graphql('mutation { privateBody }')
    monkeypatch.setenv('FAKE_EXIT', '1')
    monkeypatch.setenv('FAKE_ERR', 'Bearer opaque-test-secret (HTTP 403)')
    with pytest.raises(GlabError):
        await runner.get('user')
    text = lines(logs)
    assert 'GET https://gitlab.example.com/api/v4/projects/team%2Fapp/issues' in text
    assert 'POST https://gitlab.example.com/api/graphql mutation' in text
    assert 'HTTP 403' in text and 'OK (' in text
    assert all(secret not in text for secret in ('opaque-test-secret', 'private-search', 'private body', 'privateBody', 'Bearer'))
