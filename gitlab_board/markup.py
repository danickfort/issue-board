"""GitLab Flavored Markdown through the YouTrack app's safe renderer (youtrack_board/markup.py).

GitLab keeps a single newline inside the paragraph, so line breaks are not forced. A few GitLab-only blocks are
turned into what CommonMark can show: >>> multi-line quotes become quotes, alert markers become a bold title, and
[~] inapplicable tasks get their own mark. Everything else GitLab adds (references, math, diagrams, footnotes,
emoji codes) stays readable as typed. See docs/research/gitlab-board.md.
"""
import re

from youtrack_board.markup import CHECKBOX, markdown

TASKS = {**CHECKBOX, '[~] ': '⊘ '}
ALERT = re.compile(r'^(\s*>\s*)\[!(note|tip|important|caution|warning)\]\s*$', re.IGNORECASE | re.MULTILINE)


def glfm(text):
    """Text already free of terminal control characters, as a Rich renderable."""
    lines, fence, quoted = [], None, False
    for line in text.split('\n'):
        stripped = line.strip()
        if stripped.startswith(('```', '~~~')):
            fence = None if fence == stripped[:3] else fence or stripped[:3]
        elif stripped == '>>>' and fence is None:
            quoted = not quoted
            continue
        lines.append('> ' + line if quoted else line)
    text = ALERT.sub(lambda m: f'{m.group(1)}**{m.group(2).capitalize()}**\n{m.group(1).rstrip()}', '\n'.join(lines))
    return markdown(text, breaks=False, tasks=TASKS)
