# Backlog browsing

Status: implemented as a read-only, full-screen view (`B`). Verified on 2026-10-16 against the demo, mocked APIs and, GET only, track.liip.ch.

## What a YouTrack backlog is

The [backlog help](https://www.jetbrains.com/help/youtrack/cloud/work-with-backlog.html) defines it as a saved search chosen per board. The default query is `project: <projects> has: -{Board <name>} #Unresolved`, but any saved search can be used. Issues reordered by hand on the board "take priority over the sort order in the search query". So the backlog is neither "the board's projects" nor "unresolved issues not on the board". It is whatever that saved search returns, in its order.

## Where the app reads it

| Step | Request | Source |
| --- | --- | --- |
| Which saved search | `GET /api/agiles/{id}?fields=…,backlog(id,name,query)` | Not in the [Agile entity reference](https://www.jetbrains.com/help/youtrack/devportal/api-entity-Agile.html); returned by the live server. Unknown fields are ignored, so an older server just shows "No backlog". |
| Its issues, one page | `GET /api/savedQueries/{id}/issues?fields=…&$skip=n&$top=51` | [`SavedQuery.issues`](https://www.jetbrains.com/help/youtrack/devportal/api-entity-SavedQuery.html), "the collection of issues matching this saved search". The sub-resource path and paging work live but are not documented as an endpoint. |

Live evidence (read-only):

- 79 boards: 30 return a backlog saved search, 49 return `backlog: null`. None of the queries were empty or used `sort by:`. Four saved searches serve more than one board.
- Parler Parent - Sprints (`145-973`) uses `Parler Parent - Sprints Backlog` (`7-1414`), 40 issues. `savedQueries/7-1414/issues` starts PP-114, PP-93, PP-23, PP-83; `/api/issues?query=<same text>` starts PP-115, PP-114, PP-113, PP-21. The saved search keeps the manual rank; a query search does not. The app's 40 rows matched the saved-search order exactly.
- SWING - Support (`145-65`) uses `SWING - Support` (`7-94`), 12 issues from 7 projects. Both requests agree, as no manual order is set. The app showed the 12 rows in that order.
- `$skip`/`$top` page the collection consistently (pages of 4 and of 10 against pages of 50). An unknown saved search returns HTTP 404.
- Every request was a GET, enforced by a request hook. No preferences were written.

Not verified: whether a saved search that is not shared with the account appears as `backlog: null` or as a 403 on its issues (both are handled); whether a `sort by:` in a saved query is honored by the collection (expected, not observed); and the web page's own backlog rendering, which needs an SSO session.

## Guards

- No backlog: the app says so and asks nothing more.
- A saved search without a query would list every visible issue. The client refuses it before any request (`Client.backlog`).
- Page size 50, asking for 51 to learn whether more exist; no count request, no whole-backlog fetch.
- `r` re-reads the board setting before the first page, so a changed backlog in YouTrack is picked up.
- Workers belong to the backlog screen: closing it cancels them. `n` and `r` share one exclusive worker group, so a reload drops a page still on its way. Issues already shown are not repeated if the backlog shifts between pages.

## UX decision

| Option | 80×24 | 60×20 | Verdict |
| --- | --- | --- | --- |
| Sidebar next to the board | About 45 columns left for the board: one status column (estimated) | Board unusable | Rejected |
| Board/backlog split vertically | About 6 rows each, besides details (estimated) | About 4 rows each | Rejected |
| Full-screen view, Escape back | 12 one-line rows plus details | 9 rows plus details | Chosen |

The view is a full-size `ModalScreen` over the untouched board. Returning needs no restore logic: sprint, card, lane, scroll and filters were never changed. Being modal, it also blocks the board's key bindings, so `a`, `m`, `c`, `b`, `p`, `v`, `M`, `x` cannot act on a hidden card. Its comment reader opens without `c` (write) for the same reason.

Search is local to loaded pages and says so ("3 of 50 loaded match · n searches 50 more"). A server-side search would mean composing the user's text with the saved query, which drops the manual order and can break on `sort by:` clauses. The board's project scope, "only mine" and text filter are not applied: the saved search defines the scope.

Rows show ID (project prefix), summary, the board's column value and assignee, ellipsized to width; the details pane below reuses the board's Markdown rendering and `s` source toggle.

Out of scope: ranking, moving issues to a sprint or the board, editing the saved search, writing comments from the backlog.
