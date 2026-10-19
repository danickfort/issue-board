"""The shared UI and standalone GitLab demo work without the other tracker installed."""
import subprocess
import sys

import pytest


@pytest.mark.parametrize('gitlab', [False, True])
def test_shared_ui_and_gitlab_demo_do_not_require_youtrack(gitlab):
    script = '''
import importlib.abc
import sys

class Unavailable(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in BLOCKED:
            raise ModuleNotFoundError(fullname)

sys.meta_path.insert(0, Unavailable())
from issueboard import board, diagnostics, markup, prefs, widgets
'''
    if gitlab:
        script += '''
import asyncio
from gitlab_board.app import GitLabApp, Prefs
from gitlab_board.demo import DemoClient, demo_state
from gitlab_board.target import parse

async def verify():
    client, state = DemoClient(), demo_state()
    app = GitLabApp(client, Prefs(None, client.host, state), start=parse(state['project'], client.host))
    async with app.run_test() as pilot:
        for _ in range(3):
            await pilot.pause()
            await app.workers.wait_for_complete()
        assert app.board_data and app.query(widgets.Card)

asyncio.run(verify())
'''
    blocked = ['youtrack_board'] if gitlab else ['youtrack_board', 'gitlab_board']
    result = subprocess.run([sys.executable, '-c', f'BLOCKED = {blocked!r}\n' + script],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
