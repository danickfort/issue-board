import pytest
from textual.widgets import Button, Input

from issueboard.widgets import IssueActions, IssueScreen, Picker
from test_board import details, settle
from test_gitlab import demo_app
from test_places import jira
from youtrack_board.app import BoardApp
from youtrack_board.demo import DemoClient


@pytest.mark.parametrize('make', [lambda: BoardApp(DemoClient()), jira, lambda: demo_app()[0]])
@pytest.mark.parametrize('size', [(80, 24), (60, 20)])
async def test_labeled_controls_fit_and_mouse_actions_target_the_current_issue(make, size):
    app = make()
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        for selector in ('#project', '#boards', '#filter', '#auto'):
            control = app.query_one(selector)
            assert control.region.right <= size[0] and control.region.width >= 7
        assert '(p)' in str(app.query_one('#project').label)
        assert '(b)' in str(app.query_one('#boards').label)
        assert app.query_one('#details').region.y + 1 < app.query_one('Footer').region.y
        actions = app.query_one(IssueActions)
        for button in actions.query(Button):
            assert button.region.right <= size[0] and button.region.height == 1
        await pilot.click('#issue-move')
        await settle(app, pilot)
        assert isinstance(app.screen, Picker)
        await pilot.press('escape')
        await pilot.click('#issue-more')
        await pilot.pause()
        assert isinstance(app.screen, Picker)
        assert 'merge_requests' in [choice[0] for choice in app.screen.choices]
        app.screen.query_one(Input).value = 'Markdown source'
        await pilot.pause()
        await pilot.press('enter')
        await pilot.pause()
        assert app.source


async def test_parent_button_and_read_only_actions_follow_the_visible_issue():
    app = BoardApp(DemoClient())
    async with app.run_test(size=(100, 32)) as pilot:
        await settle(app, pilot)
        await pilot.press('slash', *'workflow err', 'enter', 'enter')
        await settle(app, pilot)
        assert 'DEMO-3' in str(app.query_one('#issue-parent', Button).label)
        await pilot.click('#issue-parent')
        await settle(app, pilot)
        assert isinstance(app.screen, IssueScreen) and app.screen.issue['idReadable'] == 'DEMO-3'
        assert not app.screen.query('#issue-move, #issue-assign, #issue-comment')
        await pilot.click('#issue-more')
        await pilot.pause()
        assert [c[0] for c in app.screen.choices] == ['open', 'source', 'load']
        await pilot.press('escape')
        await pilot.click('#issue-parent')
        await settle(app, pilot)
        assert app.screen.issue['idReadable'] == 'DEMO-68'
        assert not app.screen.query_one('#issue-parent').display
        await pilot.press('escape', 'escape')
        assert app.selected['idReadable'] == 'DEMO-4'
        await pilot.press('escape')
        assert details(app).lstrip().startswith('DEMO-4')
        assert app.query_one('#details').region.y + 1 < app.query_one('Footer').region.y
