import pytest

from gitlab_board.client import Client


@pytest.mark.parametrize('hidden', [None, 'backlog', 'closed'])
async def test_board_pins_open_first_and_closed_last_in_configured_list_order(hidden):
    nodes = [
        {'id': 'open', 'title': 'Open', 'listType': 'backlog', 'position': None},
        {'id': 'closed', 'title': 'Closed', 'listType': 'closed', 'position': None},
        {'id': 'review', 'title': 'Review', 'listType': 'status', 'position': 2},
        {'id': 'ready', 'title': 'Ready', 'listType': 'status', 'position': 0},
        {'id': 'label', 'title': 'Closed', 'listType': 'label', 'position': 1},
    ]
    nodes = [n for n in nodes if n['listType'] != hidden]

    class API:
        host = 'gitlab.example.com'

        async def graphql(self, document):
            return {'ns': {'board': {'lists': {'nodes': nodes}}}}

    result = await Client('gitlab.example.com', API()).lists(
        {'kind': 'project', 'path': 'group/project'}, {'id': 'gid://gitlab/Board/1'}, {})
    expected = ['open', 'ready', 'label', 'review', 'closed']
    expected = [id for id in expected if not (id == 'open' and hidden == 'backlog')
                and not (id == 'closed' and hidden == 'closed')]
    assert [lst['id'] for lst in result['lists']] == expected
