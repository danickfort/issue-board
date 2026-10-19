"""Atlassian Document Format (Jira's rich text) as Markdown, shown through the shared safe renderer
(issueboard/markup.py), or as that Markdown's literal text for the source view.

Paragraphs, headings, lists, task lists, quotes, panels, code, tables, links, mentions, emoji, dates, status lozenges
and smart links keep their meaning. Attachments and media show as their name, as nothing is fetched. A node this does
not know keeps its text, or names its type when it has none.
"""
import re
import time

from rich.text import Text

from issueboard.markup import markdown
from issueboard.widgets import plain

PUNCTUATION = re.compile(r'([\\`*_{}\[\]<>()#+\-.!|~])')
MARKS = {'strong': '**', 'em': '*', 'strike': '~~'}


def lit(value, escape):
    return PUNCTUATION.sub(r'\\\1', value) if escape else value


def inline(nodes, escape):
    return ''.join(text(node, escape) for node in nodes or [])


def text(node, escape):
    """An inline node as Markdown."""
    kind, attrs = node.get('type'), node.get('attrs') or {}
    if kind == 'text':
        value, marks = node.get('text') or '', node.get('marks') or []
        if any(m.get('type') == 'code' for m in marks):
            fence = '`' * (max(map(len, re.findall('`+', value)), default=0) + 1)
            value = f'{fence} {value} {fence}' if value.startswith('`') or value.endswith('`') else f'{fence}{value}{fence}'
        else:
            value = lit(value, escape)
        for mark in marks:
            wrap = MARKS.get(mark.get('type'))
            if wrap and value.strip():
                value = f'{wrap}{value}{wrap}'
            href = (mark.get('attrs') or {}).get('href') if mark.get('type') == 'link' else None
            if href:
                value = f'[{value}]({href.replace(")", "%29").replace(" ", "%20")})'
        return value
    if kind == 'hardBreak':
        return '\n'
    if kind == 'mention':
        return '@' + lit((attrs.get('text') or 'someone').lstrip('@'), escape)
    if kind == 'emoji':
        return lit(attrs.get('text') or attrs.get('shortName') or '', escape)
    if kind == 'date':
        try:
            return time.strftime('%Y-%m-%d', time.gmtime(int(attrs.get('timestamp')) / 1000))
        except (TypeError, ValueError):
            return '[date]'
    if kind == 'status':
        return f'[{lit(attrs.get("text") or "status", escape)}]'
    if kind in ('inlineCard', 'blockCard', 'embedCard'):
        return attrs.get('url') or '[link]'
    if kind in ('media', 'mediaInline'):
        return f'[attachment: {lit(attrs.get("alt") or attrs.get("filename") or attrs.get("id") or "file", escape)}]'
    if kind == 'placeholder':
        return lit(attrs.get('text') or '', escape)
    if node.get('content'):
        return inline(node['content'], escape)
    return f'[{kind or "unknown"}]'


def block(node, escape, prefix=''):
    """A block node as Markdown lines; `prefix` indents list item content and continues quotes."""
    kind, attrs, content = node.get('type'), node.get('attrs') or {}, node.get('content') or []
    if kind == 'paragraph':
        return [inline(content, escape)]
    if kind == 'heading':
        return ['#' * min(6, max(1, int(attrs.get('level') or 1))) + ' ' + inline(content, escape)]
    if kind in ('bulletList', 'orderedList', 'taskList', 'decisionList'):
        start, lines = int(attrs.get('order') or 1), []
        for n, item in enumerate(content):
            if item.get('type') in ('taskList', 'bulletList', 'orderedList'):  # a nested list directly in the list
                lines += ['  ' + line for line in block(item, escape)]
                continue
            marker = {'orderedList': f'{start + n}.', 'taskList': '- [x]' if (item.get('attrs') or {}).get('state') == 'DONE'
                      else '- [ ]', 'decisionList': '- ✓'}.get(kind, '-')
            body = blocks(item.get('content') or [], escape, gap=False) if item.get('type') == 'listItem' else \
                inline(item.get('content'), escape).split('\n')
            pad = ' ' * (len(marker.split(' ')[0]) + 1)
            lines += [(f'{marker} ' if i == 0 else pad) + line if line else '' for i, line in enumerate(body or [''])]
        return lines
    if kind == 'codeBlock':
        code = ''.join(c.get('text') or '' for c in content)
        fence = '`' * max(3, max(map(len, re.findall('`+', code)), default=0) + 1)
        return [fence + (attrs.get('language') or ''), *code.split('\n'), fence]
    if kind in ('blockquote', 'panel'):
        title = {'panel': f'**{(attrs.get("panelType") or "info").capitalize()}:**'}.get(kind)
        return ['> ' + line if line else '>' for line in [*([title, ''] if title else []), *blocks(content, escape)]]
    if kind in ('expand', 'nestedExpand'):
        return [f'**▸ {attrs.get("title") or "Details"}**', '', *blocks(content, escape)]
    if kind == 'rule':
        return ['---']
    if kind == 'table':
        rows = [[' '.join(blocks(cell.get('content') or [], escape)).replace('|', '\\|').replace('\n', ' ')
                 for cell in row.get('content') or []] for row in content]
        width = max(map(len, rows), default=0)
        rows = [row + [''] * (width - len(row)) for row in rows]
        return [f'| {" | ".join(row)} |' for row in rows[:1]] + [f'|{" --- |" * width}'] + \
            [f'| {" | ".join(row)} |' for row in rows[1:]] if rows else []
    if kind in ('mediaSingle', 'mediaGroup'):
        return [' '.join(text(m, escape) for m in content)]
    if kind in ('extension', 'bodiedExtension', 'inlineExtension'):
        return [f'[{attrs.get("extensionKey") or "extension"}]', *blocks(content, escape)]
    if content and all(c.get('type') in ('text', 'hardBreak', 'mention', 'emoji') for c in content):
        return [inline(content, escape)]
    return blocks(content, escape) if content else [text(node, escape)]


def blocks(nodes, escape, gap=True):
    """Block nodes as Markdown lines, a blank line between them unless they are the parts of one list item."""
    lines = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        if lines and gap:
            lines.append('')
        lines += [part for line in block(node, escape) for part in line.split('\n')]
    return lines


def adf_markdown(doc, escape=True):
    """A document as Markdown text. escape: protect literal text from being read as Markdown (for display)."""
    if isinstance(doc, str):  # API v2 text, or a field holding plain text
        return lit(doc, escape)
    if not isinstance(doc, dict):
        return ''
    return '\n'.join(blocks(doc.get('content') or [], escape)).strip()


def render(doc, empty, source=False):
    """A description or comment body: formatted, or the Markdown it was turned into, as literal text."""
    rendered = plain(adf_markdown(doc, escape=not source)).strip()
    return Text(empty, 'dim') if not rendered else Text(rendered) if source else markdown(rendered)
