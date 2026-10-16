# Board loading and project swimlanes

Status: implemented. Sprint-disabled membership and project swimlanes are covered by API/UI regressions and live read-only SWING/PP checks. Scope deviations are recorded below.

## Evidence from SWING - Support

Read-only checks on 2026-10-16 against board `145-65`:

- `sprintsSettings.disableSprints` is true, although `currentSprint` points at the underlying `First sprint` (`146-97`). A currentSprint object alone does not mean sprint queries are appropriate.
- The current app query `{Board SWING - Support}: {First sprint}` returns 0 issues.
- `has: {Board SWING - Support}` returns 36 issues, matching the underlying sprint's direct issue collection count at the time checked.
- Those tickets span 8 projects; the board config includes 12 projects. Do not confuse configured project count with ticket-bearing project count.
- `swimlaneSettings` is `AttributeBasedSwimlaneSettings`, enabled, with predefined field `project`. Its configured values/order define project rows.
- Configured lanes: MDB, DEFIVELO, SAG, CIAO, TERVIN, CCHE, HN, ALSWC, MIDAT, GEO, BCUL, PP.
- Status columns include grouped values, such as In Progress / On Hold. Preserve those groupings and the configured column order.
- The browser redirects to Liip SSO. API structure is verified; exact webpage appearance, empty-lane treatment and collapse state have not been visually verified.

The official [search reference](https://www.jetbrains.com/help/youtrack/cloud/search-and-command-attributes.html#board-name) distinguishes sprint-based board queries from membership searches for boards with sprints disabled. See also [sprint settings](https://www.jetbrains.com/help/youtrack/devportal/api-entity-SprintsSettings.html) and [attribute-based swimlanes](https://www.jetbrains.com/help/youtrack/devportal/api-entity-AttributeBasedSwimlaneSettings.html).

## 1. Fix membership loading first

Scope: `client.py`, board loading/refresh paths in `app.py`, `demo.py`, regression tests and README.

1. Request sprint settings with board metadata. Decide the loading mode from `disableSprints`, never from board name or the presence of currentSprint alone.
2. With sprints disabled, paginate `has: {Board <name>}` and load immediately without waiting for a sprint selector event. Hide the irrelevant sprint control. Use the existing safe literal construction for the entire Board attribute.
3. With sprints enabled, retain the corrected `{Board <name>}: {<sprint>}` query and the current sprint-selection behavior. Do not use an empty-result fallback: legitimate empty sprints must remain empty, not show every issue from the board.
4. Keep API membership loading separate from personal/project/text visibility filters. Report loaded and visible counts plus filter reasons. Provide a quick clear-filters route when all cards are hidden.
5. Make the difference between genuine empty, filtered-empty and failed load visible. Preserve selection and discard stale responses when boards change.

Acceptance checks:

- Live read-only SWING load returns the same issue IDs as its membership query, without persisting test preferences. Snapshot count was 36; tests use captured shape/synthetic fixture data rather than hardcoding live counts forever.
- PP (`145-973`) still loads only the selected sprint. Its snapshot count was 31.
- Sprints-disabled boards with/without an underlying currentSprint, empty sprint boards, paging beyond 100, filtered-empty and failed requests have focused regression tests.

## 2. Render configured project swimlanes

Scope: grouping in `client.py`, `Column`/board rendering/navigation/focus in `app.py`, local view preferences only as needed, demo, tests and docs.

1. Request swimlane settings, project identities, column order and relevant orphan-lane settings. Detect the project-based variant explicitly. Leave boards with swimlanes disabled on the current flat-column layout.
2. Resolve configured lane values to stable project identities using the returned project metadata; do not treat a swimlane-value ID as a project ID. Preserve API lane order, not alphabetic order. Missing/unmatched data must remain visible in an explicit fallback row where the board configuration permits; do not silently drop cards.
3. Render one shared set of status headers and vertically stacked project rows beneath them. Each row has the same column widths and boundaries, a project label and count, with its cards in the correct status cells. Use shared horizontal scrolling so rows stay aligned, plus vertical scrolling between lanes.
4. Use compact, content-sized lanes; avoid a full-height panel for every project. Keep headers/project context visible while navigating. Honor the board's empty/orphan settings where exposed; confirm webpage behavior before claiming exact visual parity.
5. Preserve left/right navigation between status cells and up/down through cards/lanes, including empty cells. Moving a card keeps it selected in its destination cell. Assignment, workflow moves, comments and details continue to act on the focused card.
6. In All projects mode, show all applicable project lanes. An explicitly selected project remains a transparent personal filter; label the reduced scope and offer All projects without changing the remote board. Do not force grouping by project on boards configured with another kind of swimlane.
7. Preserve mine/text filters, compact cards and hidden status columns. Lane counts reflect visible cards; loaded totals remain available. No new generic layout/settings framework.

Acceptance checks:

- Demo board with multiple project lanes, grouped status values, empty cells and a long project name.
- Fixture reproducing SWING config: expected lane order, consistent column widths, each eligible card reachable, no cards silently lost through grouping.
- Focus/selection survive filtering, refresh, board changes and workflow moves across columns.
- Actual TUI captures and keyboard verification at 60x20, 80x24 and 120x40; no duplicate rendering or stale board results.
- Read-only live verification of Swing membership and project distribution. Visual comparison with the webpage remains pending an authenticated browser session.

## Delivery order and limits

Implemented membership loading first, then project swimlanes. Empty project lanes are hidden for terminal space; orphan cards remain visible even when the web board hides its orphan lane, with an explicit label. Other swimlane types remain flat with a status explanation. Exact webpage parity remains unverified because the browser requires SSO.

Do not expand this pass to arbitrary custom-field or issue-based swimlanes, drag-and-drop, manual ordering, WIP enforcement, shared board editing, or Markdown rendering. Unsupported swimlane kinds should be described honestly while retaining visible issue access.

The user subsequently approved Markdown research and implementation. The supported subset, fallbacks and verification are in [Markdown support](../research/markdown-support.md).
