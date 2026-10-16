"""Local, per-server view state: when boards were last opened here, the project scope, per-board view options, and
the GitLab repository of each project for its merge requests.

Nothing here is sent to YouTrack and no credentials are stored.
"""
import contextlib
import json
import os
import tempfile
import time

KEEP_VISITS = 50


def write_json(path, data):
    """Replace the file atomically, so readers never see half of it. mkstemp makes it readable by this user only."""
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=folder, prefix='.tmp-', suffix='.json')
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as file:
            json.dump(data, file, indent=1)
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def default_path(app='youtrack-board'):
    base = os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state')
    return os.path.join(base, app, 'state.json')


def _dict(value):
    return value if isinstance(value, dict) else {}


def clean_view(view):
    view = _dict(view)
    hidden = view.get('hidden') if isinstance(view.get('hidden'), list) else []
    return {'mine': view.get('mine') is True, 'compact': view.get('compact') is True,
            'hidden': [title for title in hidden if isinstance(title, str)],
            'auto': view.get('auto') is not False}  # auto-refresh is on until turned off for the board


def clean(section, view=clean_view):
    """Keep only well-formed values, so a hand-edited or older file cannot break the app."""
    section = _dict(section)
    project = section.get('project')
    return {'visited': {k: v for k, v in _dict(section.get('visited')).items() if type(v) in (int, float)},
            'project': project if isinstance(project, str) else None,
            'views': {k: view(v) for k, v in _dict(section.get('views')).items()},
            # project id -> the GitLab project URL whose merge requests the board shows for its stories
            'repos': {k: v for k, v in _dict(section.get('repos')).items() if isinstance(v, str)}}


class Prefs:
    VIEW = staticmethod(clean_view)  # the per-board view options this app keeps

    def __init__(self, path, server, section=None):
        """Without a path, state starts from `section` and stays in memory (the demo and tests use this)."""
        self.path, self.server, self.warning = path, server, None
        self.data = self.clean(self.read().get(server) if path else section)

    def clean(self, section):
        return clean(section, self.VIEW)

    def read(self):
        try:
            with open(self.path, encoding='utf-8') as file:
                data = json.load(file)
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            pass
        # Keep the unreadable file for inspection instead of overwriting it on the next save.
        try:
            os.replace(self.path, self.path + '.corrupt')
            self.warning = f'Saved view state was unreadable and was moved to {self.path}.corrupt.'
        except OSError:
            self.warning = f'Saved view state at {self.path} is unreadable.'
        return {}

    def save(self):
        """Replace only this server's section, atomically, so other servers' state and concurrent readers stay intact."""
        if not self.path:
            return
        data = self.read()
        data[self.server] = self.data
        write_json(self.path, data)

    def visit(self, board_id):
        visited = self.data['visited']
        visited[board_id] = time.time()
        for old in sorted(visited, key=visited.get)[:-KEEP_VISITS]:
            del visited[old]

    def view(self, board_id):
        return self.VIEW(self.data['views'].get(board_id))

    def set_view(self, board_id, **changes):
        self.data['views'][board_id] = {**self.view(board_id), **changes}
