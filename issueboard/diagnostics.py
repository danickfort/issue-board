"""Opt-in, in-memory diagnostics shared by both boards. Never log credentials or payloads."""
from collections import deque
import logging
import re

from rich.text import Text
from textual.widgets import RichLog

LOGGER = logging.getLogger('board')
LOGGER.setLevel(logging.DEBUG)
LOGGER.propagate = False
LOGGER.addHandler(logging.NullHandler())
SECRET = re.compile(r'gl[a-z]{2,12}-[A-Za-z0-9_.\-]{16,}|(?i:bearer|private-token:?)\s+\S+')


class LogBuffer(logging.Handler):
    def __init__(self, secrets=()):
        super().__init__()
        self.lines = deque(maxlen=1000)
        self.secrets = tuple(secret for secret in secrets if secret)
        self.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s', datefmt='%H:%M:%S'))

    def emit(self, record):
        text = self.format(record)
        for secret in self.secrets:
            text = text.replace(secret, '[redacted]')
        text = SECRET.sub('[redacted]', text)
        # Render as literal text, without terminal controls, markup or clickable links.
        text = ''.join(c if c.isprintable() else ' ' for c in text)
        self.lines.append((record.levelno, text[:2000]))


class LogPanel(RichLog):
    DEFAULT_CSS = """
    LogPanel { dock: bottom; height: 30%; min-height: 4; max-height: 12;
               border: round $border; scrollbar-size: 1 1; margin-bottom: 1; }
    """

    def __init__(self):
        super().__init__(id='log-panel', max_lines=1000, wrap=True, markup=False, highlight=False)
        self.border_title = 'Logs · F12 hide/show · Tab to focus and scroll'

    def drain(self, buffer):
        while buffer.lines:
            level, text = buffer.lines.popleft()
            self.write(Text(text, style='red' if level >= logging.ERROR else 'yellow' if level >= logging.WARNING else ''))
