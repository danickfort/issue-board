# GitLab issue board in the terminal: findings and design

Status: released separately as [GitLab Board 0.1.0](https://github.com/krabilousse/gitlab-board/releases/tag/v0.1.0), installable from the private `krabilousse/gitlab` Homebrew tap. Researched and checked on 2026-10-16 against GitLab.com 19.6.0-pre (Enterprise Edition), glab 1.115.0, GitLab's source at `master` (commit `bf8b7997dc96fe5912d8224b4a16d61e674fdf1a`), and the example board, read-only. User guide: [docs/gitlab-board.md](../gitlab-board.md).

## Summary

- **The example URL means milestone *Sprint 10* and label *PolCo*.** GitLab's board page reads a repeated key that has no `[]` by keeping the **last** value. The first value is not combined with the second, by AND or by OR. The duplicate comes from GitLab itself: a scoped board appends its own scope to the query string it is opened with. The app applies the same rule and shows a warning naming the dropped value.
- **GitLab decides list membership; the app does not reimplement it.** The app reads each list's issues with the same GraphQL query the web board uses (`board.lists { issues(filters) }`), so Open, Closed, label, status, assignee, milestone and iteration lists hold exactly what GitLab puts in them, in GitLab's order. One request reads the whole board, at a query complexity of 113 of the 250 allowed.
- **A board's scope (Premium) is not applied by the server.** The web page adds the scope as filters on the client. Without the same step, the example board would list 1,828 issues instead of 23. The app adds the scope exactly as the page does.
- **Moves use GitLab's own mutation (`issueMoveList`),** so GitLab changes labels, open/closed state, assignee, milestone, iteration or status for the two lists involved and leaves other labels alone. Before a move, the picker shows what GitLab will change. This matters most for moves to Closed, which also removes every label that has a list on the board.
- **The example board uses Status lists** (Premium/Ultimate, work item statuses), not label lists. Issues are a work item type; issue references link to `/-/work_items/<iid>`.
- **Swimlanes in GitLab are epic swimlanes only** (Premium/Ultimate), a per-user display choice (`group_by=epic`). The app offers them per board with `e`. The example board's epics exist, but none matches its filters, so all issues land in "Issues with no epic assigned", as they would on the web.
- **Everything goes through `glab api`** with glab's own sign-in. GraphQL documents go to glab on stdin, so no issue text or filter value appears in a process listing. REST reads are explicit GETs.
- **Not possible or not done:** observing the web page for this board without your browser session (an anonymous visit returns *Not Found*); reordering within a list; adding a second assignee; epic boards; creating issues; editing labels directly; WIP and list settings. Details are in the parity table below.

## How to read the evidence

Each claim below says where it comes from:

- **Docs**: GitLab's or glab's published documentation.
- **Source**: GitLab's source at the commit above, the code the web board runs.
- **Observed**: live, read-only requests to GitLab.com on 2026-10-16, or glab's actual behavior.
- **Inferred**: a conclusion from the above, not checked directly.
- **Tier**: needs GitLab Premium or Ultimate.

## Sources

Documentation (fetched 2026-10-16):

- Issue boards: <https://docs.gitlab.com/user/project/issue_board/>
- Scoped labels: <https://docs.gitlab.com/user/project/labels/#scoped-labels>
- Work item status: <https://docs.gitlab.com/user/work_items/status/>
- Multiple assignees: <https://docs.gitlab.com/user/project/issues/multiple_assignees_for_issues/>
- GitLab Flavored Markdown: <https://docs.gitlab.com/user/markdown/>
- Notes API: <https://docs.gitlab.com/api/notes/>
- Members API: <https://docs.gitlab.com/api/members/>
- GraphQL API and limits: <https://docs.gitlab.com/api/graphql/>, reference <https://docs.gitlab.com/api/graphql/reference/>
- GitLab.com rate limits: <https://docs.gitlab.com/user/gitlab_com/rate_limits/>
- Quick actions: <https://docs.gitlab.com/user/project/quick_actions/>
- `glab api`: <https://docs.gitlab.com/cli/api/>, and `glab api --help`, `glab config --help` for 1.115.0

GitLab source, `gitlab-org/gitlab` (paths under <https://gitlab.com/gitlab-org/gitlab/-/blob/bf8b7997dc96fe5912d8224b4a16d61e674fdf1a/>; read through `glab api` GETs):

| File | What it settles |
| --- | --- |
| `app/assets/javascripts/boards/index.js` | The board reads its URL with `queryToObject(window.location.search, { gatherArrays: true })` |
| `app/assets/javascripts/lib/utils/url_utility.js` | `queryToObject`: keys ending in `[]` collect values; any other key is overwritten, so the last value wins. `+` is a space |
| `ee/app/assets/javascripts/boards/components/board_filtered_search.vue` | On load, a scoped board appends its scope to `window.location.search` and reparses it |
| `ee/app/assets/javascripts/boards/boards_util.js` | `transformBoardConfig`: which scope values are appended (milestone, iteration and cadence, weight, assignee, labels not already present); EE filter rules |
| `app/assets/javascripts/boards/boards_util.js` | `FiltersInfo` and `filterVariables`: URL keys to GraphQL filters, `not[…]`, `or[…]`, wildcards; unsupported keys dropped silently |
| `app/assets/javascripts/boards/constants.js`, `ee/…/constants.js` | Filter fields a board accepts; list types including `status`; `group_by=epic` |
| `app/assets/javascripts/boards/components/board_app.vue` | Epic swimlanes on from `group_by=epic`, else from a per-board `localStorage` key |
| `ee/app/assets/javascripts/boards/components/epics_swimlanes.vue` | Epic lanes first, "Issues with no epic assigned" last |
| `app/assets/javascripts/boards/graphql/lists_issues.query.graphql`, `issue_move_list.mutation.graphql`, `ee/…/issue.fragment.graphql` | The queries and mutation the web board sends |
| `app/services/boards/base_items_list_service.rb`, `app/services/boards/issues/list_service.rb`, `ee/app/services/ee/boards/issues/list_service.rb` | Which issues each list holds, and in what order |
| `app/services/boards/base_item_move_service.rb`, `ee/app/services/ee/boards/issues/move_service.rb`, `ee/app/models/ee/list.rb` | What a move between two lists changes |
| `app/graphql/mutations/boards/issues/issue_move_list.rb` | `issueMoveList` arguments and checks |

## Architecture

**Layout.** A sibling package, `gitlab_board/`, next to `youtrack_board/`:

| File | Role |
| --- | --- |
| `glab.py` | Runs `glab api`: argv, stdin, environment, timeout, cancellation, error messages, REST paging |
| `target.py` | Board URL parsing and filters, following GitLab's page (pure functions) |
| `client.py` | GraphQL documents and REST paths; flattening answers; move-effect preview |
| `markup.py` | GitLab Markdown through the shared renderer |
| `app.py` | The Textual app: `GitLabApp(BoardView)` and its screens |
| `demo.py` | Offline GitLab for `--demo` and tests |

**Architecture review: share the terminal UI, keep tracker semantics separate.** The original `GitLabApp` inherited the complete YouTrack `BoardApp`. This reused navigation and refresh, but also loaded YouTrack credentials and inherited sprint state, custom-field assumptions and repository-linking logic that GitLab did not use. Comment dialogs repeated the same loading, retry and source-toggle flow. Extracting the existing shared behavior is worthwhile; normalizing both trackers into one issue schema is not.

Both tracker apps now inherit `issueboard.board.BoardView`. It owns column and swimlane rendering, scrolling, focus preservation, the refresh timer with pausing and backoff, details, and the write lifecycle. `issueboard.widgets` owns dialogs, comment loading and paged-list navigation. `issueboard.prefs`, `issueboard.markup` and `issueboard.diagnostics` own state persistence, safe Markdown rendering and credential-redacted logs. These modules import neither tracker. GitLab imports no YouTrack modules; YouTrack still loads GitLab's merge-request functionality on demand because that is an actual feature.

Each tracker keeps its own client, authentication, startup, filters, issue and comment formatting, paging, permissions and mutations. YouTrack keeps sprint selection and repository mappings; GitLab keeps scoped lists, cursor paging and epic swimlanes. The existing methods (`read`, `groups`, `lanes`, `details`, `ref`, `read_comments`, `comment_view`) remain the seam. `refresh_context` distinguishes a YouTrack board/sprint from a GitLab board when rejecting stale refresh results. Preferences retain their existing locations and formats.

**Why GraphQL for the board.** The REST Boards API lists boards and lists but has no endpoint for "the issues in this list". Rebuilding membership from `GET /issues` would mean reimplementing `Boards::Issues::ListService`: the Open list excludes every issue another list claims, the Closed list sorts by close date, status lists include closed issues, and the type and scope rules come on top. GraphQL `BoardList.issues(filters)` runs that service on the server, and it is what the web board itself calls (Source). It is documented in the GraphQL reference.

**Requests.**

| Purpose | Request | Notes |
| --- | --- | --- |
| Who am I | GraphQL `currentUser { username name }` | Once per session |
| A project or group and its boards | GraphQL `project(fullPath)` and `group(fullPath)` in one query | A plain `/<path>` URL can be either |
| A board's settings and scope | GraphQL `board(id) { … milestone iteration iterationCadence assignee weight labels }` | Scope fields are EE |
| The board (each refresh) | GraphQL `board { lists(issueFilters) { … issues(first: 100, filters) } [epics(first: 50)] }` | One request; complexity 113 of 250 (Observed) |
| One list, a page at a time (`B`) | GraphQL `lists(id) { issues(first: 50, after) }` | Cursor paging |
| Move | GraphQL mutation `issueMoveList(projectPath, iid, boardId, fromListId, toListId)` | As a drag on the web board |
| Assign | GraphQL mutation `issueSetAssignees(…, operationMode: REPLACE)` | The answer is checked against the request |
| Members to assign | REST `GET /projects/:path/members/all`, 100 per page, at most 5 pages | Active members with at least the Guest role |
| Comments | REST `GET /projects/:path/issues/:iid/notes?sort=desc&order_by=created_at&activity_filter=only_comments`, at most 20 pages | System notes are also dropped locally |
| Write a comment | REST `POST /projects/:path/issues/:iid/notes`, JSON body on stdin | No GraphQL, so the 10,000-character query limit never applies to the comment |
| An issue's merge requests (`g`) | GraphQL `workItem(id) { widgets { … on WorkItemWidgetDevelopment { closingMergeRequests relatedMergeRequests } } }`, 100 each | As GitLab's issue page; one issue per request. |

**How glab is run.**

- **Always `--hostname <host>`,** and the working directory is `/`. glab picks the host from the Git checkout it runs in, and `-F` fields fill placeholders such as `:repo` from it (Docs). Outside a checkout, a placeholder fails loudly instead of being filled.
- **GraphQL documents go on stdin** with `-F query=@-`. Observed: glab reads the file as is, with no placeholder expansion. Values are written into the document as GraphQL literals: strings JSON-escaped, which is valid GraphQL string syntax, and enums and input names checked against `[A-Za-z_][A-Za-z0-9_]*`. Variables were rejected for two reasons. `-F` expands placeholders in values (Observed: `-F 's={"a":"x:repo"}'` fails with "not a git repository"). And `-f` can only send strings, while board filters are objects and lists.
- **`--input` does not work for GraphQL** in glab 1.115.0 (Observed: "Unexpected end of document"). For REST, glab sends no `Content-Type` with `--input`, and GitLab answers HTTP 415. The glab documentation says JSON is the default (Observed against Docs). The app sets the header.
- **REST reads pass `--method GET` explicitly** and refuse any path outside `[A-Za-z0-9_.~%/?&=-]`, so no `:placeholder` can appear. Reads never use `-f` or `-F`, which would turn a GET into a POST (Docs).
- **The environment** sets `GLAB_CHECK_UPDATE=false`, `GLAB_NO_PROMPT=true` (`NO_PROMPT` is deprecated, Observed), `GLAB_SEND_TELEMETRY=false`, `GLAB_SHOW_WHATS_NEW=false`, `GLAB_NOTIFY_SKILL_UPDATES=false` and `NO_COLOR=1`. It removes `GLAB_DEBUG_HTTP` and `DEBUG`, which print request headers.
- **Each call has a 30-second limit,** like GitLab's GraphQL timeout. A cancelled or timed-out call kills glab and waits for it, so no process is left behind.
- **Errors** show the GraphQL or REST message, then glab's last stderr line, with the HTTP status. HTTP 401, or no token, becomes `glab auth login --hostname <host>` guidance. Text that looks like a GitLab token (`gl…-…`, `Bearer …`) is redacted.

**Before the board opens**, `glab` must be on `PATH`, and `glab auth status --hostname <host>` must succeed. Observed: it exits 0 for a signed-in host, and 1 for an expired token or an unknown host. Otherwise the app exits with the install or login command to run.

**A GitLab without EE fields** (Community Edition on a self-managed server) answers "Field 'weight' doesn't exist on type 'Issue'". The client then asks again with core fields only and remembers it. Epic swimlanes report that they need Premium.

## How GitLab boards work, and what the app does

### Boards, lists and tiers

| Topic | GitLab | Evidence | App |
| --- | --- | --- | --- |
| Project and group boards | Several per project in all tiers; several per group needs Premium | Docs | Both, from a URL or the place picker; references are `project#iid` on group boards |
| Open, Closed | Open: open issues no other list claims. Closed: all closed issues, newest closed first | Docs, Source (`ListService#order`) | Shown as GitLab returns them |
| Column order | Open is always leftmost, Closed always rightmost; other lists have a saved position | [Docs](https://docs.gitlab.com/user/project/issue_board/#issue-board-terminology) | Normalized after reading the API, whose raw order can put Closed second |
| Label lists | Open issues with the label. An issue with two list labels appears in **both** lists | Docs, Source (`with_list_label`) | Shown in both; a move starts from the list the card is in |
| Assignee, milestone, iteration lists | Open issues with that user, milestone or iteration; the Open list excludes them | Tier, Source (EE `ListService`) | Shown and movable |
| Status lists | Issues with that work item status, **open or closed** (no state filter) | Tier, Source (`set_state` skips status lists) | Shown and movable; the example board uses them |
| Hidden Open or Closed list | Left out of `board.lists` | Observed (board 7502524 hides Open; the API returns no `backlog` list) | Nothing to do |
| Order | `relative_position` within a positioning root; Closed by `closed_at` | Docs, Source | Kept as returned |
| Issue types | The types a namespace marks `filterable_board_view` | Source | Whatever GitLab returns |
| WIP limits | Per list, by count or weight; not on Open or Closed | Tier, Docs | Count limit shown as `· max N`, with `⚠` when exceeded; weight limits not shown |
| Collapsed lists | A per-user web preference | Source | Ignored; hide lists locally with `v` |

### Filters, URL and scope

1. **Reading the query string.** The page uses `queryToObject(search, { gatherArrays: true })`. A pair without `=` is skipped. `+` becomes a space before percent-decoding. A key ending in `[]` collects every value. Any other key keeps its last value, and only text up to a second `=` survives (JS `split('=')`). Source. `target.query_params` reproduces this and records the values it dropped.
2. **Board scope (Tier).** The server does not apply a board's milestone, labels, assignee, weight or iteration to `board.lists` or `issues`. Observed: the example board without filters returns 254 Open and 1,547 Closed issues. The web page appends the scope to the URL and reads it again (`transformBoardConfig`, Source). A scope value replaces a different URL value for the same key, because it comes last. Scope labels join the URL's. A weight of `-1` (any) adds nothing; `-2` means `None`. Iteration ids `0`, `-1` and `-4` mean `None`, `Any` and `Current`, together with the cadence. `target.scoped` does the same, and says so when it replaces a URL value.
3. **To GraphQL.** `FiltersInfo` and `filterVariables` map URL keys to `BoardIssueInput`. This covers `not[…]` where allowed and `or[…]` for labels, authors and assignees. Milestone `Any`, `None`, `Started` and `Upcoming` become `milestoneWildcardId`, and the same kind of rule applies to assignee, epic, iteration and health. `confidential=yes` becomes `true`. `type[]` and epic or iteration ids become global ids. Source. `target.filters` follows it, key by key.
4. **Unsupported keys.** The page drops them silently: custom fields without that feature, `not[search]`, `or[milestone_title]`, unknown keys. The app instead refuses the URL and names each key, because showing a wider board without saying so is worse.

`group_by=epic` turns on epic swimlanes for that board and is not a filter. Choosing another board drops the URL filters, as the web page does when it switches boards (`updateHistory` to the bare board URL, Source).

### Moves

From `BaseItemMoveService` plus its EE part (Source), when an issue moves from list A to list B:

| Move | Labels | State and other fields |
| --- | --- | --- |
| Label list → label list | Adds B's label, removes A's; other labels stay | — |
| Any list → Closed | Removes **every label that has a list on this board** | Closes |
| Any list → Open | Removes every board list label | Removes A's assignee if A is an assignee list; clears the milestone or iteration if A is a milestone or iteration list |
| Closed → any list | Adds B's label | Reopens |
| Between different movable types (label, assignee, milestone, iteration, status) | Adds B's label if B is a label list; removes nothing | Assigns B's user (keeps others); sets B's milestone, iteration or status |
| Scoped labels (Tier) | Adding `a::b` replaces any other `a::…`; for nested scopes, the scope is everything before the last `::` | Docs |

The move picker shows these effects for every target (`client.move_effects`). The demo applies them to its in-memory issues. On a real board GitLab applies them, and the board reloads. Moving and assigning need `admin_issue` (Source: `move_single_issuable`), which GitLab grants from the Planner role up (Docs, multiple assignees page). Each issue's `userPermissions.adminIssue` and `createNote` are read with the board, so moves, assignments and comments that GitLab would refuse are explained before anything is sent. `issueMoveList` returns `errors`, for example when an assignee list's user cannot see the issue (Source); the app shows them.

Not implemented: the position inside the target list. `moveBeforeId`, `moveAfterId` and `positionInList` exist (Source). Without them the issue keeps its relative position.

### Swimlanes

GitLab has only epic swimlanes (Tier, Docs). The web page turns them on with `group_by=epic` or a per-board `localStorage` flag, lists the board's epics with `board.epics(issueFilters)`, and puts "Issues with no epic assigned" last (Source). The app asks for `board.epics` in the same request as the lists. It groups loaded cards by `Issue.epic`, which the web board's fragment also reads. `Issue.epic` is marked "Replaced by WorkItem type. Deprecated in GitLab 17.5", but the web board still uses it (Source). If `board.epics` fails, for example without the license, the board is read again without it, and the status line says why. There are no assignee, label or project swimlanes in GitLab to reproduce.

### Work items

- `issueMoveList`, `BoardList.issues` and the `Issue` type still serve boards. The web board's issue fragment adds `workItemType` (GitLab 18.10+). Issue references render as links to `/-/work_items/<iid>` (Observed in a Markdown render).
- Status lists and `Issue.status` are newer; `Issue.status` is marked experimental (Source schema). Statuses in the Done and Canceled categories close the work item (Docs). A move into a status list sets that status; a move out of one into Open or Closed resets it to the default (Source).
- Epics are work items. Epic boards are a separate board type (`group.epicBoards`) and are not supported.

### The example URL

`https://gitlab.com/geocity/geocity/-/boards/11511337?milestone_title=Sprint+9&label_name[]=PolCo&milestone_title=Sprint+10#/`

| Part | Meaning |
| --- | --- |
| `gitlab.com`, `geocity/geocity` | Host and project (a project board: there is no `/groups/` prefix) |
| `boards/11511337` | Board *Police des constructions - Sprint 10* |
| `milestone_title` twice | Last wins: **Sprint 10**. *Sprint 9* is dropped, not combined |
| `label_name[]=PolCo` | Label filter, as a list |
| `#/` | The page's router state; ignored |

**Why the URL looks like this (Inferred from Source).** The board's scope is milestone *Sprint 10* and label *PolCo* (Observed). When a scoped board opens, the page appends each scope value the URL does not already have. A URL that already said `milestone_title=Sprint+9&label_name[]=PolCo`, for example a link copied while the board, or an earlier one, was scoped to Sprint 9, becomes exactly this URL: `PolCo` is present, so it is not repeated, and `Sprint 10` differs, so it is appended.

**Why AND or OR would both be wrong.** `BoardIssueInput.milestoneTitle` is a single `String`. `UnionedIssueFilterInput` has no milestone field, so OR cannot even be expressed in a board query. An issue has one milestone, so AND would always be empty. Observed: the board's list counts with each reading are below. They are counts only; no issue content was copied.

| Filters | Open | Closed | Ready | In progress | Dev review | Customer review |
| --- | --- | --- | --- | --- | --- | --- |
| None (the server's own answer, without the scope) | 254 | 1,547 | 26 | 1 | 0 | 0 |
| Sprint 10 + PolCo (what GitLab's page uses, and the app) | 0 | 22 | 0 | 1 | 0 | 0 |
| Sprint 9 + PolCo (the dropped value) | 0 | 21 | 0 | 0 | 0 | 0 |

The app opened the board read-only through its real TUI at 80×24. It showed the six lists with those counts (23 issues) and the line "Board filters: milestone Sprint 10 · label PolCo ⚠ The URL gives milestone_title 2 times (Sprint 9, Sprint 10); GitLab's board uses only the last, 'Sprint 10'". Epic lanes showed only "Issues with no epic assigned", 23 issues, which matches `board.epics` returning no epic for those filters.

**Web page not observed.** An anonymous browser visit to the URL, with a fresh profile and no session or token, returned GitLab's *Not Found* page, although the project is public and its issues API answers anonymously. The board's web behavior therefore comes from the source above, not from watching the page. Doing that would need your signed-in browser, and putting a token into a browser was out of scope.

## Feature parity

Same: as in the YouTrack app. Different: the GitLab concept differs and the app follows GitLab. Tier: needs GitLab Premium or Ultimate. API gap: no supported API. Not implemented: possible, but left out.

| Feature | YouTrack app | GitLab app | Class |
| --- | --- | --- | --- |
| Open a board | Board picker, MRU | Board URL (with filters), project or group URL or path, or `b` for this place's boards, most recent first | Same |
| Project scope | Picker narrows boards and cards to a project | `p` picks a project or group (recent ones, the parent group, or a typed URL or path); on group boards, the text filter matches `project#iid` references | Different |
| Resume, MRU | Saved per server | Saved per host: board paths, last place, view options | Same |
| Columns | Configured State values, grouped | GitLab lists of every type, with server-side membership; an issue can sit in several | Different |
| Swimlanes | Project lanes from board settings | Epic lanes, per board with `e` or `group_by=epic`; no-epic lane last | Tier |
| Scrolling and layout | Horizontal and vertical, shared headers, 80×24 and 60×20 | Same code | Same |
| Card and details | ID, summary, assignee; fields and Markdown | `#iid` (or `project#iid`), title, assignees, labels, confidential mark; state, lists, status, labels, milestone, iteration, epic, weight, due date, GitLab Markdown | Same |
| Compact, only mine, hidden columns, text filter | Yes | Yes; the text filter also matches labels | Same |
| Markdown source and formatted | `s` | `s`, with GitLab rules (below) | Same |
| Open in browser | `o` | `o`: the issue's `webUrl`, only if it is https on the board's host; no token involved | Same |
| Assign | Single-user Assignee from the project bundle, groups included | Active project members (direct, inherited, invited groups), at least Guest, first 500; replaces all assignees; result checked | Different |
| Second assignee | n/a | Not offered; `issueSetAssignees` `APPEND` and `REMOVE` exist | Tier, not implemented |
| Workflow transitions | State machine events | n/a: GitLab has no workflow engine. Status lists come closest, and `issueMoveList` handles them | Different |
| Move | Pick a State value | Pick a list; effects shown first; GitLab applies them | Different |
| Reorder within a list | Not supported | Not implemented (`positionInList`, `moveBeforeId` exist) | Not implemented |
| Write a comment | Single line | Single line, REST; lines starting with `/` refused (quick actions) | Same |
| Read comments | Newest first, all pages, visibility | Newest first, up to 2,000, system notes hidden, internal notes marked, threads flattened | Same |
| Linked merge requests (`g`) | Open MRs in the project's chosen repository that name the story ID | Open MRs GitLab links to the issue (closing or mentioning, any project), with pipeline and merge status; read-only | Different |
| Internal notes | n/a | Read and marked; writing one is not offered | Not implemented |
| Permission errors | From the server | Checked first from `userPermissions`, then from the server | Same |
| Control and markup stripping | Plain metadata | Same, and notifications never read GitLab text as markup | Same |
| Backlog | Saved search, ranked, pages of 50 | `B`: the focused list in full (Open is GitLab's backlog list type), board order, pages of 50 by cursor, search, details, read-only comments | Different |
| Auto-refresh | 5 s, per board, pauses, backoff, drops stale answers | Same code; one GraphQL request (~2 s on the example board) | Same |
| Credentials | Env, keychain, MCP config, setup | glab's sign-in only; install and login guidance; `--auth-status`. Never reads glab's config or keyring | Different |
| Demo | Offline, in memory | Offline, in memory, never runs glab | Same |
| URL filters | n/a | GitLab's page rules; unsupported keys refused by name | New |
| Board scope | n/a | Applied like the page; replacements explained | Tier |
| WIP limits | Not shown | Count limit shown | Tier |
| Epic boards, issue creation, label editing, close action, list and board settings, collapsing lists | n/a | Not implemented. Move to Closed or Open covers closing and reopening | Not implemented |
| Custom field filters | n/a | Refused | Not implemented |

## Markdown

The app renders with markdown-it's CommonMark parser, plus strikethrough and tables, into Rich, through the YouTrack app's sanitized pipeline (`youtrack_board/markup.py`), with GitLab rules in `gitlab_board/markup.py`. "Observed" means rendered by GitLab's stateless `POST /api/v4/markdown` on 2026-10-16.

| GitLab construct | GitLab | Terminal | |
| --- | --- | --- | --- |
| Single newline | Stays in the paragraph (Docs, Observed) | Same: no forced break, unlike YouTrack | Same |
| Two spaces or `\` at the end of a line | Line break | Line break | Same |
| Headings, emphasis, strikethrough, inline code, lists, quotes, rules | Rendered | Rich styles | Same |
| Tables | Rendered | Rich table | Same |
| Task lists `[ ]` `[x]` | Checkboxes | ☐ ☑ | Same |
| Inapplicable task `[~]` | Struck-through checkbox (Docs, Observed) | ⊘ | Different |
| Code fences with a language | Highlighted | Pygments highlighting | Same |
| Mermaid, PlantUML, Kroki fences | Diagrams | The source, in a code block | Not rendered |
| Math `$…$`, `$$…$$`, `math` fences | Rendered (first 1000 inline) | As typed or as a code block | Not rendered |
| `>>>` multi-line quote | Quote (Observed) | Quote | Same |
| Alerts `> [!note]` and the others | Box with a title (Observed) | Quote with a bold title | Different |
| Footnotes `[^1]` | Superscript and list (Observed) | As typed | Not rendered |
| References `#12`, `!3`, `&5`, `~label`, `~"a::b"`, `%milestone`, `@user` | Links (Observed: `#1` links to a work item) | As typed; not looked up | Not rendered |
| Images and uploads `![x](/uploads/…/a.png){width=…}` | Images | `[image: x]`, size removed; nothing downloaded; `o` opens the issue | Different |
| Links | Links | `text (url)`; nothing is clickable or followed | Different |
| Bare URLs | Auto-linked | The URL as text | Same |
| Raw HTML, `<details>` | Sanitized HTML | The source, in a code block; never interpreted | Different |
| Emoji `:tada:` | Emoji (Observed) | As typed | Not rendered |
| Color chips `` `#F00` `` | Chip | Inline code | Different |
| `[[_TOC_]]`, front matter | Descriptions or files only | As typed | Not rendered |

`s` shows every description and comment exactly as typed. Terminal control characters are removed before parsing. Rich markup in GitLab text stays literal: issue titles, names, labels and notification texts are plain text.

## Security and authentication

- **Tokens.** Only glab holds them, in its own configuration or keyring. The app never reads glab's configuration, never runs `glab auth login`, never passes `--show-token`, and never uses the YouTrack app's keychain entry or MCP files. The startup check reads only the exit code of `glab auth status --hostname <host>` and the first line of `glab --version`.
- **argv** holds only fixed flags, the host and encoded REST paths. Issue text, comments and filter values travel on stdin.
- **Read-only checks.** Every live check in this research was read-only. A wrapper refused any GraphQL document that did not start with `query`, any REST method other than GET, and any POST. The exceptions: GraphQL queries are POSTs by protocol, and `POST /api/v4/markdown`, which only renders text and stores nothing, was called once to observe Markdown and glab's `--input` behavior. No issue, board, label, assignee or comment was created or changed. The app's moves, assignments and comments were verified against the offline demo and against the live GraphQL schema (introspection) only.
- **Quick actions.** GitLab runs a comment line starting with `/` as a command (`/close`, `/assign`, Docs). The app refuses such comments rather than run commands from a one-line box.
- **Confidential issues** are marked; GitLab returns only what the account may see. Internal notes are marked "for project members only".
- **Browser.** `o` opens the issue's `webUrl` only if it is https on the board's host. Nothing is sent to the browser but the URL.
- **Rate limits.** Authenticated API traffic on GitLab.com is limited to 2,000 requests a minute (Docs; observed `RateLimit-Limit: 2000`, `throttle_authenticated_api`). Note creation is limited to 60 a minute. An unattended board makes 12 requests a minute. HTTP 429 shows a "wait a minute" message, and auto-refresh backs off as in the YouTrack app.
- **Repository contents.** The demo data is fictional. No issue content or personal data from the example board is in the repository, the tests or this report; only list names and counts.

## Verification

- `uv run pytest -q`: **99 passed** (68 YouTrack, 31 GitLab), with no network and no glab sign-in. The GitLab tests cover:
  - URL and filter semantics: the example URL, scope replacement, nested and group URLs, refusals, wildcards, negation and union.
  - GraphQL literal safety.
  - glab argv, stdin, environment, GET method, unsafe paths, errors, redaction, a missing binary, cancellation and timeout killing the process, and REST paging. These run a fake `glab` executable.
  - Mutation payloads and result checks, the comment quick-action refusal, the EE-to-core fallback, the epic fallback, notes and members.
  - Move-effect rules.
  - The real TUI: cards in two lists, a move out of the right list, URL filters and warnings, epic lanes and lane keys, assignment, permission refusals, plain-text safety, comments, the paged list view, auto-refresh, Markdown, 80×24 and 60×20, saved state, URL dialog validation, and the demo never running glab.
- Live, read-only, through the real client and TUI against the example board. The client read me, the place, the board, its lists, two list pages, members and notes. The TUI opened with the URL, showed comments and the full list, toggled lanes, refreshed and opened the move picker: 13 requests, all queries or GETs.
- Real terminal: the demo started in a pseudo-terminal at 80×24 and 60×20, and quit with exit code 0, writing no state.
- Captures of the offline TUI (board, URL warning, epic lanes, move picker, comments, list view at 80×24 and 60×20, details at 100×40) are in `/tmp/gitlab-board-shots/`, outside the repository.
- Not verified: writes against a real GitLab (by instruction); the web page for this board (see above); a GitLab Community Edition server; boards with more than 100 issues in a list on a live server.

## Limitations and risks

- **100 issues per list on the board.** Column titles say "100 of N", the status line says how many lists are cut, and `B` pages through the rest. Epic lanes group only the loaded cards, whereas the web page queries each lane.
- **Refresh cost.** Descriptions are read with the board, about 2 s and tens of kilobytes on the example board, every 5 s while the board is visible. A board with hundreds of long descriptions reads more.
- **Deprecated and experimental fields:** `Issue.epic`, `Issue.status` and `BoardList.status` (not used). If GitLab removes `Issue.epic`, the "doesn't exist" fallback turns off all EE fields, including epic lanes and weight. The board still works.
- **glab behavior is pinned to 1.115.0 observations:** stdin for `-F query=@-`, the `--input` content type, exit codes. Because glab runs outside a checkout, a change would fail visibly rather than quietly.
- **The move preview mirrors GitLab's service and can drift.** The move itself is GitLab's: the preview only explains, and the board shows GitLab's result after reloading.
- **Assignment replaces all assignees.** On Premium, an issue with several assignees says so in the picker title.
- **Members are capped at 500** per project in the picker.
- **One host per session.** Opening another host's URL from inside the app is refused with a message.
- **The demo** follows the rules above for its own data and refuses filters it does not implement. It is not a GitLab emulator.

## Future work, by value

1. Reorder within a list and move to the top or bottom (`positionInList`), a GitLab feature people use daily.
2. Add or remove one assignee on Premium (`APPEND`, `REMOVE`), with a tier check.
3. Load more than 100 cards per list on the board on demand, and per-epic lane queries like the web page.
4. Lighter refresh: leave out descriptions and fetch the selected issue's description on demand.
5. Binary Homebrew bottles and Linux distribution. The macOS source formula is published and tested.
6. Internal note writing, and threaded comments through the discussions API.
7. Close or reopen without a list move, label editing, and creating an issue in a list.
8. Epic boards (`group.epicBoards`).
9. Custom field filters (`custom-field[N]`) when the namespace has the feature.
10. Server-side recent boards (`recentIssueBoards`) to resume across machines. They already preselect the first-time default board question.

## Starting in a code checkout, and the default board

Checked on 2026-10-16 against glab 1.115.0, GitLab 19.4.2 (self-managed) and gitlab.com (19.6.0-pre), with read-only GraphQL queries.

**How glab finds a repository's project.** glab's resolver ([remote_resolver.go](https://gitlab.com/gitlab-org/cli/-/blob/main/internal/cmdutils/remote_resolver.go), [resolver.go](https://gitlab.com/gitlab-org/cli/-/blob/main/internal/glrepo/resolver.go), [git.go](https://gitlab.com/gitlab-org/cli/-/blob/main/internal/git/git.go)) reads `git remote -v`, translates SSH hosts through the SSH config, keeps remotes on hosts glab knows (gitlab.com and the hosts it is signed in to) that share one host, and reads `remote.<name>.glab-resolved[-base]` from the Git config. Without a prompt it takes the first remote; with one, it asks and writes `glab-resolved-base` into the repository's Git config. glab offers no command that prints the resolved project without an API call (`glab repo view` reads the project from GitLab), and running glab in the checkout would let it fill `:fullpath`-style placeholders from there.

So `gitlab_board/remotes.py` follows the same steps with read-only commands in the checkout: `git remote -v` (fetch URLs only), `git config --get-regexp` for a glab-resolved base, and `ssh -G <alias>` for SSH aliases, which prints the resolved settings without connecting. It differs from glab in one deliberate way: several projects are offered as a choice instead of taking the first, and nothing is written to the Git config. Credentials in remote URLs are dropped at parsing. Both boards use this one helper.

**Which hosts count.** `glab api --hostname <host>` sends the request even when glab has no sign-in for the host (tried with `example.invalid`: glab attempted `POST https://example.invalid/api/graphql`). `glab auth status --hostname <host>` answers *has not been authenticated with glab* from its own configuration, without a request (0.1 s, against 0.5 s for a signed-in host, which it checks against that host). So each remote host is first checked with `glab auth status`. A host glab does not know is left out, so a github.com remote is never contacted. A host it knows whose sign-in fails is reported, not skipped. `glab.check` now tells these apart (`UnknownHost`), and reports `dial tcp`, `no such host` and timeouts as an unreachable host instead of a missing sign-in.

**No reliable "freshest" board.** GitLab's GraphQL `Board` type has `createdAt` and `updatedAt`, "Timestamp of when the board was last updated": the board record, which changes when its name or scope changes, not when its issues move. Lists and issues carry their own times, but a board's issues are a filtered view, and inferring activity from them is the guess the requirements rule out. `Project.recentIssueBoards` and `Group.recentIssueBoards` are "recently visited boards", at most 4, per user: GitLab records a visit each time you open the board page and returns them newest first ([board_recent_visit.rb](https://gitlab.com/gitlab-org/gitlab/-/blob/master/app/models/concerns/board_recent_visit.rb): `order(updated_at: :desc)`). That says which board you looked at last on the web, not which one is most active. So the first start in a checkout asks, and preselects that board, labelled *your last visit on GitLab*. It is read only for that question, and an error there only removes the preselection.

**The default is a separate choice.** `defaults` in the state file maps a project or group path to a board, per host. Only three things set it: the first-start question, a project with a single board, and `D`. Opening a board with `b`, a URL or auto-resume only updates the recently-opened times, as before. A default that the next start no longer finds among the place's boards (deleted, or no longer visible) is explained and asked again. The place query reads the first 100 boards, so a default beyond them would also count as gone; no project checked has that many.

**YouTrack.** The YouTrack board reuses `remotes.find` lazily, on the first `g` of a project without a repository, in a thread, so its start needs neither git nor glab. A saved repository wins. A found repository is saved only after GitLab shows it (the same check as a typed URL), only for the selected story's project, and not when another YouTrack project already uses it: one checkout says nothing about which YouTrack project it belongs to.
