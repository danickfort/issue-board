# Auto-refresh

Status: implemented. The board on screen re-reads its issues every 5 seconds, per-board toggle `R`, on by default. Verified on 2026-10-16 against the demo in a real terminal, headless tests, and, GET only, track.liip.ch.

## Sources

| Question | Source | Finding |
| --- | --- | --- |
| How to run something every few seconds | Textual [`set_interval`](https://textual.textualize.io/api/message_pump/) | "Call a function at periodic intervals." Returns a timer. The page does not say when timers stop; in the Textual 8.2.8 source, `Timer._tick` does nothing once the app is exiting. |
| What if a tick is late | Textual [`Timer`](https://textual.textualize.io/api/timer/) | `skip` defaults to true: "Enable skipping of scheduled events that couldn't be sent in time." A slow tick never queues a burst. |
| Overlapping and stale answers | Textual [workers guide](https://textual.textualize.io/guide/workers/) | "The `exclusive` flag tells Textual to cancel all previous workers before starting the new one", because network answers can arrive out of order. Workers "are tied to the DOM node" and are cleaned up when it goes. |
| Redrawing without flicker | Textual [`App.batch_update`](https://textual.textualize.io/api/app/) | "A context manager to suspend all repaints until the end of the batch." |
| Can YouTrack push changes to a client | YouTrack [Webhook Triggers app](https://www.jetbrains.com/help/youtrack/server/webhook-triggers.html) and [workflow HTTP requests](https://www.jetbrains.com/help/youtrack/devportal/JS-Workflow-REST-API.html) | Both send `POST`s from YouTrack to "external services", which "expose webhook URLs". An administrator installs the app. A terminal app has no reachable URL, so neither fits. The [REST API](https://www.jetbrains.com/help/youtrack/devportal/youtrack-rest-api.html) documents no subscription or streaming endpoint. |
| A cheaper change feed | [Pagination](https://www.jetbrains.com/help/youtrack/devportal/api-concept-pagination.html), [issue activities page](https://www.jetbrains.com/help/youtrack/devportal/operations-api-issues-issueID-activitiesPage.html) | Activities change between requests, so they need cursors. Per-issue activity needs at least one category per request. Turning activities back into board columns is more code and more requests than re-reading the board. |
| What the board read returns | [`GET /api/issues`](https://www.jetbrains.com/help/youtrack/devportal/resource-api-issues.html) | `query`, `fields`, `$top`, `$skip`. Without a sort clause the order is `updated desc`. Each issue has an `updated` timestamp. |
| Request limits | [Secure your instance](https://www.jetbrains.com/help/youtrack/cloud/secure-your-installation.html) | Throttling is documented for logins, per IP address. No per-client REST quota is published. |

A delta search (`updated:` since the last read) was also rejected. It cannot see an issue that left the board or sprint, because that issue no longer matches the board query. So a full read is needed anyway.

## Decision

Poll with the read the board already does: the same `GET /api/issues?query={Board …}: {Sprint …}` (or `has: {Board …}`) that `r` sends. Nothing new is added: no dependency, no endpoint and no streaming. A Textual interval timer ticks every 5 s and starts one exclusive worker in the board's `load` group.

| Hazard | Handling |
| --- | --- |
| Overlapping requests | A tick does nothing while any board, sprint, issue or refresh load is unfinished. |
| A stale answer replaces the current board | Any other load cancels the refresh (same exclusive worker group). The answer is also dropped unless the same board and sprint are still shown. |
| Redrawing under a dialog, the backlog, the filter or expanded details | Ticks wait while a modal screen is up (pickers, comments, view, backlog), the filter has focus, details are maximized, or a save runs. An answer that arrives after one of these opened is dropped; the next tick catches up. |
| Flicker, or rebuilding for nothing | Equal issue data means no redraw. A redraw happens inside `batch_update`, so no half-built frame is painted. |
| Losing the place | The selected card stays selected and follows a moved card. Focus stays on the same cell, including an empty one. If the focused card left, the cursor stays in its column. The board's scroll offset is kept: without the batch, emptying the board reset it. Scope, only-mine, text filter, hidden columns and source mode are local state and untouched. |
| Failures | The board keeps its data. The tick interval doubles to at most 12× (10, 20, 40, 60 s), the `↻` indicator turns red and shows it, and a single warning notification names the error per outage. Any successful read (a refresh or `r`) restores 5 s. |
| Lifecycle | The timer and workers belong to the app, so they stop on quit. The demo exits cleanly with code 0 in a real terminal. |

Default: on, every 5 s, saved per board as `auto` in the board's view (`state.json`), like only-mine and compact. The point of the feature is a board that stays current without thought, as YouTrack's own board does. The cost is small (below), and it stops whenever the board is covered. `R` turns it off for a board; the choice is remembered on this computer.

## Live evidence

GET only, enforced by a transport that raises before sending anything else, with in-memory preferences. The credentials came from the app's own helper and were not printed.

| Board | Data | One refresh | Unchanged board |
| --- | --- | --- | --- |
| SWING - Support (`145-65`) | 36 issues, 8 project lanes, sprints disabled | 2 GETs to `/api/issues` (the page, then the empty page that ends paging), 0.24–0.28 s | Not rebuilt three times in a row, selection kept; two raw reads compare equal |
| Parler Parent - Sprints (`145-973`) | Sprint 5, 31 issues | 2 GETs, 0.18–0.19 s | Same |

So a visible, unattended board costs 24 small GETs a minute. The trailing empty page comes from `Client.pages`, which stops only on an empty page. Stopping on a short page would halve this, but it would trust that YouTrack never returns fewer than `$top` mid-collection. That was left unchanged.

## Not covered

- The app cannot tell whether the terminal window is visible. A board left open in a background window keeps refreshing. Pausing on terminal focus loss was rejected because a board on a second monitor is a main use.
- In a flat (non-swimlane) column taller than the screen, the selected card stays visible after a redraw but may sit a few rows higher or lower. The new column scrolls to its card itself, after any restore the app could do.
- A redraw rewrites the status line, so a red save error can be replaced by the counts if the board changes within seconds of it.
- The check that drops an answer for another board or sprint sits behind worker cancellation, which catches every switch the tests can produce. It guards the gap while a sprint switch redraws before its load starts.
