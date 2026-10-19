import pytest
from textual.widgets import OptionList

from gitlab_board.client import Client
from gitlab_board.merge_requests import details, participants, person, summary
from test_gitlab import FakeGlab, demo_app, settle
from test_merge_requests import mr, pick
from youtrack_board.app import BoardApp
from youtrack_board.demo import DemoClient, demo_gitlab, demo_state
from youtrack_board.prefs import Prefs


def test_auto_merge_is_an_indication_not_an_action_or_mergeability_claim():
    item = mr(1, autoMergeEnabled=True, detailedMergeStatus='CI_MUST_PASS')
    assert 'auto-merge on' in summary(item) and 'pipeline must succeed' in summary(item)
    assert 'auto-merge off' in details(mr(1, autoMergeEnabled=False)).plain
    assert 'auto-merge unknown' in details(mr(1)).plain
    assert 'auto-merge' not in summary(mr(1))


def test_author_reviewers_and_assignees_remain_distinct_and_plain():
    user = {'name': 'Sam [red]\x1b', 'username': 'sam'}
    item = mr(1, author={'name': 'Author', 'username': 'author'},
              reviewers={'nodes': [{**user, 'mergeRequestInteraction': {'reviewState': 'REQUESTED_CHANGES'}}]},
              assignees={'nodes': [{'name': 'Assigned', 'username': 'assigned'}]})
    shown = details(item).plain
    assert 'Author: Author (@author)' in shown
    assert 'Reviewers: Sam [red] (@sam) — changes requested' in shown
    assert 'Assignees: Assigned (@assigned)' in shown
    assert '\x1b' not in shown
    assert 'changes requested' in summary(item)


@pytest.mark.parametrize('state,word', [('UNREVIEWED', 'awaiting review'), ('REVIEW_STARTED', 'reviewing'),
                                      ('APPROVED', 'approved'), ('NEW_STATE', 'new state')])
def test_review_states_and_missing_connections_are_not_confused(state, word):
    item = mr(1, reviewers={'nodes': [{'username': 'sam', 'mergeRequestInteraction': {'reviewState': state}}]})
    assert word in participants(item, 'reviewers')
    assert participants(item, 'assignees') == 'unknown'
    assert participants(mr(1, assignees={'nodes': []}), 'assignees') == 'none'
    assert 'more in GitLab' in participants(mr(1, reviewers={'nodes': [], 'pageInfo': {'hasNextPage': True}}), 'reviewers')
    assert person(None) == 'unknown'


async def test_metadata_is_requested_with_the_linked_mrs_not_by_extra_calls():
    fake = FakeGlab({'workItem': {'widgets': [{'closingMergeRequests': {}, 'relatedMergeRequests': {}}]}})
    await Client(fake.host, fake).issue_merge_requests({'id': 'gid://gitlab/Issue/1'})
    assert len(fake.documents) == 1
    query = fake.documents[0]
    assert all(field in query for field in ('autoMergeEnabled', 'author {', 'reviewers(first: 20)',
                                           'assignees(first: 20)', 'mergeRequestInteraction { reviewState }'))


@pytest.mark.parametrize('service', ['gitlab', 'youtrack'])
@pytest.mark.parametrize('size', [(80, 24), (60, 20)])
async def test_mr_people_are_readable_in_both_apps_with_scrollable_details(service, size):
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
        content = str(screen.query_one('#mr-details').content)
        branch = screen.current()['sourceBranch']
        listing = screen.query_one(OptionList)
        prompt = listing.get_option_at_index(listing.highlighted).prompt.plain
        assert f'Branch: {branch}' in prompt
        assert content.splitlines()[1] == f'Branch: {branch} → {screen.current()["targetBranch"]}'
        assert 'auto-merge on' in content and 'Author: Alex Morgan (@alex)' in content
        assert 'Reviewers: Jordan Lee (@jordan) — reviewing' in content
        assert 'Assignees: Sam Rivera (@sam)' in content
        pane = screen.query_one('#mr-detail-scroll')
        assert pane.max_scroll_y > 0
        selected = screen.current()
        await pilot.press('pagedown')
        await pilot.pause()
        assert pane.scroll_y > 0 and screen.current() is selected
        await pilot.press('pageup')
        await pilot.pause()
        assert pane.scroll_y == 0
        assert screen.query_one('#mr-hint').region.bottom <= screen.query_one('#mr-dialog').region.bottom
