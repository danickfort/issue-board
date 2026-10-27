import pytest
from textual.widgets import OptionList, Static

from gitlab_board.client import Client
from gitlab_board.merge_requests import approvals, details, discussion_status, summary
from test_gitlab import FakeGlab, demo_app, settle
from test_merge_requests import mr, pick
from youtrack_board.app import BoardApp
from youtrack_board.demo import DemoClient, demo_state
from issueboard.story_merge_requests import demo_gitlab
from issueboard.prefs import Prefs


@pytest.mark.parametrize('count,total,resolved,expected', [
    (0, 0, 0, 'no resolvable threads'),
    (4, 0, 0, 'no resolvable threads'),
    (6, 3, 1, '2 unresolved · 1 resolved'),
    (3, 2, 2, '0 unresolved · 2 resolved'),
    (None, None, None, 'unknown'),
    (2, 3, None, 'unknown'),
])
def test_comment_totals_do_not_imply_threads_are_resolved(count, total, resolved, expected):
    item = mr(1, userNotesCount=count, resolvableDiscussionsCount=total, resolvedDiscussionsCount=resolved)
    assert discussion_status(item) == expected
    assert f'Threads: {expected}' in details(item).plain
    assert f'Comments: {count if count is not None else "unknown"}' in details(item).plain
    if count is not None:
        assert f'{count} comment' in summary(item)


def test_unavailable_approval_count_does_not_mean_approved():
    assert approvals(mr(1, approvalsRequired=2, approvalsLeft=None)) == 'unknown (2 required)'


async def test_linked_mr_query_reads_aggregate_user_comments_and_thread_counts():
    fake = FakeGlab({'workItem': {'widgets': [{'closingMergeRequests': {}, 'relatedMergeRequests': {}}]}})
    await Client(fake.host, fake).issue_merge_requests({'id': 'gid://gitlab/Issue/1'})
    query = fake.documents[0]
    assert all(field in query for field in ('userNotesCount', 'resolvableDiscussionsCount', 'resolvedDiscussionsCount'))
    assert 'discussions(' not in query and 'notes(' not in query


@pytest.mark.parametrize('service', ['gitlab', 'youtrack'])
@pytest.mark.parametrize('size', [(80, 24), (60, 20)])
async def test_both_boards_surface_unresolved_and_resolved_review_threads(service, size):
    if service == 'gitlab':
        app, _ = demo_app()
    else:
        client = DemoClient()
        app = BoardApp(client, Prefs(None, client.base_url, demo_state()), gitlab=demo_gitlab)
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        if service == 'gitlab':
            await pilot.press('right', 'right', 'down')
        else:
            await pick(app, pilot, 'DEMO-3')
        await pilot.press('g')
        await settle(app, pilot)
        screen = app.screen
        assert '6 comments' in str(screen.query_one(OptionList).get_option_at_index(0).prompt)
        assert 'threads: 2 unresolved · 1 resolved' in str(screen.query_one(OptionList).get_option_at_index(0).prompt)
        panel = screen.query_one('#mr-details', Static)
        assert 'Comments: 6 · Threads: 2 unresolved · 1 resolved' in str(panel.content)
        assert screen.query_one(OptionList).size.height >= 3
        assert screen.query_one('#mr-detail-scroll').region.bottom <= screen.query_one('#mr-dialog').region.bottom
