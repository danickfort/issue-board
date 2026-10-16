# Markdown in descriptions and comments

Status: implemented in `youtrack_board/markup.py`, used by the details pane and the comment reader (`C`). `s` switches both between formatted text and the source as typed.

## Question

YouTrack stores issue descriptions and comments as Markdown. The app used to show that source literally. Which subset can a terminal show well with what is already installed, and what happens to the rest?

## Sources

- YouTrack Markdown syntax for issues and comments: <https://www.jetbrains.com/help/youtrack/cloud/youtrack-markdown-syntax-issues.html>. CommonMark plus strikethrough, tables, checklists, mentions, issue and article links, image sizing, stack traces, Mermaid, LaTeX, and a filtered set of HTML tags (`<details>`, `<font>`, `<br>`, `<table>`, …).
- YouTrack `IssueComment` entity: <https://www.jetbrains.com/help/youtrack/devportal/api-entity-IssueComment.html>. `text` is the source; `textPreview` is YouTrack's own HTML rendering.
- CommonMark 0.31.2: <https://spec.commonmark.org/0.31.2/>.
- markdown-it-py security notes: <https://markdown-it-py.readthedocs.io/en/latest/security.html>. Links with `javascript:`, `vbscript:`, `file:` and most `data:` URLs are not turned into links.
- Rich Markdown: <https://rich.readthedocs.io/en/stable/markdown.html>, and the installed source `rich/markdown.py` (Rich 15.0.0), which is the authority for the details below.
- Textual Markdown widget: <https://textual.textualize.io/widgets/markdown/>, source `textual/widgets/_markdown.py` (Textual 8.2.8).

## What real issues contain

Read-only sample on 2026-10-16: the descriptions and comments of the 36 SWING - Support issues and the 31 Parler Parent Sprint 5 issues. Only construct counts and masked shapes were read.

- Common: lists, checklists written `* [x] …` / `* [ ] …` (often nested), `@first.last` mentions, issue IDs, bold, headings, quotes, `<https://…>` autolinks.
- Attachment images written `![](image.png){width=70%}`; a few attachment links `[file.pdf](file.pdf)`; a few file names with spaces.
- Some `<br/>`, a few tables, two code fences, no Mermaid or LaTeX in this sample.
- One description contained a zero-width space before bold text. It is kept; real terminals show it as nothing.
- A single newline inside a paragraph: in all 21 comments that had one, YouTrack's `textPreview` contained `<br>`. YouTrack shows single newlines as line breaks, unlike plain CommonMark.
- `usesMarkdown` (older YouTrack Wiki markup) is not returned on these issues or comments, so all text is treated as Markdown.

## Renderer choice

Both candidates are already installed and both use markdown-it-py.

| | Rich `Markdown` in a `Static` | Textual `Markdown` widget |
| --- | --- | --- |
| Structure | One renderable; the details pane and comment cards keep their widgets, focus and scrolling | A tree of block widgets per text, with its own link handling |
| Links | `hyperlinks=False` prints `text (url)`; nothing is clickable | Clicking calls `app.open_url` unless every `LinkClicked` is intercepted |
| Raw HTML | Dropped silently | Dropped silently |
| Parser | Fixed in the constructor; its parsed tokens can be replaced | `parser_factory` |

Rich was chosen: it needs no new widgets, cannot open anything, and its token list can be adjusted before rendering. The Textual widget would add focusable parts inside the details pane and a click-to-open path for links in remote content.

## Supported subset

`markdown(text)` parses with markdown-it-py's CommonMark preset plus `strikethrough` and `table`, adjusts the tokens, and hands them to Rich with `hyperlinks=False`.

| YouTrack construct | In the terminal |
| --- | --- |
| Headings, bold, italic, strikethrough, inline code | Rich styles (h1 centred, others left) |
| Paragraphs, single newlines, trailing `\` or two spaces | Line breaks where YouTrack has them |
| Bulleted and numbered lists, nesting | Bullets / numbers, indented |
| Checklists `- [ ]` / `- [x]` | `☐` / `☑` after the bullet |
| Block quotes | `▌` bar |
| Fenced and indented code, with a language | Code block; Pygments highlighting when the language is known, plain otherwise |
| Tables, with inline formatting and `<br>` in cells | Rich table; text wraps in cells |
| Links `[text](url)`, reference links | `text (url)`, not clickable |
| Autolinks `<https://…>`, `<a@b.c>` | The address once |
| `<br>`, `<br/>`, `<br />` | Line break |
| Horizontal rules | A rule |
| Backslash escapes | As CommonMark |

## Not rendered, and what is shown instead

| Construct | Shown as | Why |
| --- | --- | --- |
| Images and attachments `![](a.png){width=70%}` | `[image: a.png]`, in place; the size attribute is removed | Nothing is downloaded. `o` opens the issue in the browser |
| Attachment links `[file.pdf](file.pdf)` | `file.pdf (file.pdf)` | A relative URL is not resolved: YouTrack serves attachments to signed-in sessions, and building a URL could send the reader off with a token-less, broken or misleading link |
| Image file names with spaces | The source, e.g. `![](Screen shot.png){width=70%}` | Not valid CommonMark; it stays readable |
| `@login` mentions | The text `@login` | YouTrack replaces it with the user's full name only in its UI |
| Issue IDs and article IDs | The ID as text | No lookup per ID |
| Mermaid (```` ```mermaid ````), LaTeX (```` ```latex ````, `$$`) | Their source in a code block | Diagrams and math are not drawn or executed |
| HTML blocks (`<details>`, `<table>`, `<font>`, `<script>`, …) | Their source in a code block | Never interpreted. Rich would otherwise drop them silently |
| Inline HTML other than `<br>` | The tag as text | Same |
| `javascript:`, `vbscript:`, `file:`, `data:` links | The Markdown source of the link | markdown-it-py does not make them links |
| `stacktrace` code blocks | A plain code block | IDE links are a YouTrack feature |
| Emoji short codes, `<font color>` | As typed | Not supported by the renderer |
| YouTrack Wiki markup | Read as Markdown; `s` shows the source | `usesMarkdown` is not available |
| Query URLs as dynamic lists | The URL | YouTrack shows these in its visual editor only |

`s` (on the board and in the comment reader) shows the exact source of every description and comment, so nothing is lost when the formatting is wrong.

## Safety

- Terminal control characters are removed from all YouTrack text before parsing (the existing `plain()`), so escapes cannot restyle the terminal.
- Rich markup such as `[b]…[/b]` in remote text stays literal: Rich's Markdown renderer adds text as `Text`, never as markup.
- Author names, times, visibility, IDs, summaries and field values stay plain `Text`, outside the Markdown renderer.
- No OSC 8 hyperlinks are emitted, no URL is opened, no image or attachment is fetched, and no request carries the token anywhere except the configured YouTrack API.
- Code is only highlighted, by the installed Pygments.

## Verification

- `tests/test_board.py::test_markdown_renders_youtrack_text_readably_and_never_as_live_content`: headings, YouTrack line breaks, checklists, quotes, tables, diagram source, HTML shown as source, link display, an unsafe link left as text, image placeholders without size attributes, literal Rich markup.
- `test_details_render_markdown_and_s_shows_the_source` and `test_comments_read_newest_first_safely_and_return_to_the_card`: formatted text, plain metadata, control characters removed, `s` in both places, focus and selection unchanged.
- Rendered live descriptions and comments were inspected in headless captures. The browser rendering of the same issues was not compared, because the browser session redirects to SSO.
