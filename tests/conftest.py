import pytest


def forbidden():
    raise AssertionError('a test reached the real OS credential store')


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path_factory):
    """No test may read the real MCP configs or saved files, use the real keychain, or see real credentials. Nor the
    remotes of the checkout the tests run in, or the real SSH config: tests start in an empty folder."""
    home, elsewhere = tmp_path_factory.mktemp('home'), tmp_path_factory.mktemp('elsewhere')
    monkeypatch.chdir(elsewhere)
    (elsewhere / 'ssh_config').write_text('')
    monkeypatch.setattr('gitlab_board.remotes.SSH_CONFIG', str(elsewhere / 'ssh_config'))
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / 'config'))
    monkeypatch.setenv('XDG_STATE_HOME', str(home / 'state'))
    for name in ('YOUTRACK_URL', 'YOUTRACK_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr('keyring.get_keyring', forbidden)
    return home


@pytest.fixture(autouse=True)
def no_timed_refresh(monkeypatch):
    """A slow test must not be refreshed under its feet: tests call BoardApp.tick() themselves."""
    monkeypatch.setattr('youtrack_board.app.BoardApp.REFRESH', 3600)
