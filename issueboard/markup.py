"""Shared Markdown as a Rich renderable: Rich's Markdown, fed by markdown-it's CommonMark parser.

Token fixes keep images, checklists and raw HTML readable in a terminal. What a terminal cannot show (images, attachments, diagrams, math,
HTML) stays as readable text or source. Nothing is fetched and no link is followed or made clickable.
See docs/research/markdown-support.md.
"""
import re

from markdown_it import MarkdownIt
from rich.markdown import Markdown

# html=True only so raw HTML arrives as tokens that can be shown as source: Rich would silently drop it.
PARSER = MarkdownIt('commonmark', {'html': True}).enable(['strikethrough', 'table'])
CHECKBOX = {'[ ] ': '☐ ', '[x] ': '☑ ', '[X] ': '☑ '}
BREAK = re.compile(r'<br\s*/?>', re.IGNORECASE)
SIZE = re.compile(r'^\{\s*(?:width|height)=[^}]*\}')  # YouTrack's image size, as in ![](a.png){width=70%}


def fix_inline(children, first_in_item, breaks=True, tasks=CHECKBOX):
    kept = []
    for child in children:
        if child.type == 'softbreak' and breaks:  # YouTrack shows a single newline as a line break
            child.type = 'hardbreak'
        elif child.type == 'html_inline':
            child.type = 'hardbreak' if BREAK.fullmatch(child.content) else 'text'
        elif child.type in ('link_open', 'link_close') and child.markup in ('autolink', 'linkify'):
            continue  # <https://…> shows its address already; Rich would print it twice
        elif child.type == 'image':  # in place: Rich would move it to the start of the paragraph
            name = child.content or str(child.attrs.get('src', '')).rsplit('/', 1)[-1]
            child.type, child.content, child.children, child.markup = 'text', f'[image: {name}]', None, 'image'
        elif child.type == 'text' and kept and kept[-1].markup == 'image':
            child.content = SIZE.sub('', child.content)
        elif child.type == 'text' and first_in_item and not kept:
            prefix = child.content[:4]
            child.content = tasks[prefix] + child.content[4:] if prefix in tasks else child.content
        kept.append(child)
    return kept


def markdown(text, breaks=True, tasks=CHECKBOX):
    """Text that is already free of terminal control characters, as a renderable that wraps to any width.
    breaks: a single newline breaks the line, as YouTrack shows it. tasks: the checklist markers to draw."""
    tokens = PARSER.parse(text)
    for n, token in enumerate(tokens):
        if token.type == 'html_block':
            token.type = 'code_block'  # shown as its source, in a code block
        elif token.type == 'inline' and token.children:
            token.children = fix_inline(token.children, n >= 2 and tokens[n - 2].type == 'list_item_open', breaks, tasks)
    rendered = Markdown('', hyperlinks=False)  # links print their address: "text (url)"
    rendered.parsed = tokens
    return rendered
