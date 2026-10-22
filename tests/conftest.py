import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def forbidden():
    raise AssertionError('a test reached the real OS credential store or keychain')


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path_factory):
    """No test may read the real MCP configs or saved files, use the real keychain, or see real credentials. Nor the
    remotes of the checkout the tests run in, or the real SSH config: tests start in an empty folder."""
    home, elsewhere = tmp_path_factory.mktemp('home'), tmp_path_factory.mktemp('elsewhere')
    monkeypatch.chdir(elsewhere)
    (elsewhere / 'ssh_config').write_text('')
    monkeypatch.setattr('gitlab_board.remotes.SSH_CONFIG', str(elsewhere / 'ssh_config'))
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('USERPROFILE', str(home))  # expanduser uses this on Windows, not HOME
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / 'config'))
    monkeypatch.setenv('XDG_STATE_HOME', str(home / 'state'))
    for name in ('YOUTRACK_URL', 'YOUTRACK_TOKEN', 'JIRA_URL', 'JIRA_EMAIL', 'JIRA_API_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr('keyring.get_keyring', forbidden)
    monkeypatch.setattr('jira_board.credentials.keychain_item', lambda *_, **__: forbidden())
    return home


@pytest.fixture(autouse=True)
def no_timed_refresh(monkeypatch):
    """A slow test must not be refreshed under its feet: tests call tick() themselves."""
    monkeypatch.setattr('issueboard.board.BoardView.REFRESH', 3600)
    monkeypatch.setattr('jira_board.app.JiraApp.REFRESH', 3600)


@pytest.fixture
def executable_script(monkeypatch):
    """Real Python subprocesses on every OS; Windows has no executable shebang scripts.

    Only the registered fixture paths are routed through Python. Production glab
    invocation stays unchanged, and cancellation still kills the real child PID.
    """
    paths = []
    native_run, native_async, native_which = subprocess.run, asyncio.create_subprocess_exec, shutil.which
    # Windows venv python.exe is a redirector which creates another process.
    # Use the base interpreter so the PID we cancel is the fixture's actual PID.
    interpreter = getattr(sys, '_base_executable', sys.executable) if os.name == 'nt' else sys.executable

    def resolve(command):
        command = os.fspath(command)
        for path in paths:
            if command == str(path) or command == path.name and str(path.parent) in os.environ.get('PATH', '').split(os.pathsep):
                return str(path)
        return None

    def run(args, *positional, **kwargs):
        if isinstance(args, (list, tuple)) and args and (path := resolve(args[0])):
            args = [interpreter, path, *args[1:]]
        return native_run(args, *positional, **kwargs)

    async def launch(program, *args, **kwargs):
        if path := resolve(program):
            return await native_async(interpreter, path, *args, **kwargs)
        return await native_async(program, *args, **kwargs)

    def create(path, source):
        path = Path(path)
        path.write_text(source, encoding='utf-8')
        path.chmod(0o755)
        paths.append(path)
        if os.name == 'nt':
            monkeypatch.setattr(subprocess, 'run', run)
            monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
            monkeypatch.setattr(shutil, 'which', lambda command, *a, **kw: resolve(command) or native_which(command, *a, **kw))
        return path
    return create


@pytest.fixture
def process_exists():
    def exists(pid):
        if os.name == 'nt':
            # os.kill(pid, 0) terminates processes on Windows; query the handle instead.
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
            if not handle:
                return False
            try:
                return kernel.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
            finally:
                kernel.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    return exists
