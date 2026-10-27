# Jira Board

A keyboard-driven board for any Jira Cloud site (`https://<site>.atlassian.net`). Choose Jira for a folder in `issueboard`, or run `issueboard jira` on its own. Jira Data Center and Server are not supported.

```sh
issueboard --demo jira   # or: issueboard jira --demo; offline, needs no account
issueboard jira               # the board you opened last
issueboard jira --setup       # sign in to a Jira site and save it
issueboard jira --auth-status # check the credential source and account, then exit
issueboard jira --logout      # remove the sites and tokens this app saved
```

## Connect to Jira Cloud

Jira Cloud signs in with your Atlassian account email and an API token. Create the token at <https://id.atlassian.com/manage-profile/security/api-tokens> with **Create API token**. Tokens "with scopes" only work through Atlassian's API gateway, not on the site address, so they are rejected with HTTP 401. The token acts with your permissions: it needs to browse the boards' projects, and to transition, assign and comment on the issues you change.

Setup asks for the site address, the email and the token (typing hidden), checks them with a read-only `GET /rest/api/3/myself`, and saves them. Setup never changes anything in Jira. It stops after three failed tries and saves nothing; Ctrl+C or Ctrl+D cancels it.

### Where credentials come from

The first source that has credentials for the site is used. A site, its email and its token always come from the same source, and a token is only sent to the site it came with.

1. **Environment**: `JIRA_URL`, `JIRA_EMAIL` and `JIRA_API_TOKEN`, all three set. They are used for that session and never saved. If only some are set, the app stops and names the missing ones. If Jira rejects them, the app stops.
2. **Saved by this app**: each site and its email in `$XDG_CONFIG_HOME/jira-board/config.json` (or `~/.config/jira-board/config.json`), and its token in the OS credential store under the service `jira-board`, keyed by the site address. Several sites can be saved; `issueboard jira` alone opens the one set up last, and each `issueboard` folder opens its own site.
3. **The ALS workspace keychain item** `als-atlassian-api` (macOS only; account = email, password = API token). It holds a token for `https://richemont.atlassian.net`, so it is used for that site only and never sent anywhere else. It is read in place with Apple's `security` tool on each start, never copied, and never changed or removed by this app.
4. Otherwise, setup runs. Without an interactive terminal, the app exits with instructions instead.

If Jira rejects a saved token (HTTP 401 or 403), the app says so and tries the next source, then setup. If Jira cannot be reached, the board opens with the connection error, and `r` retries.

The token is never written to a file, printed, logged or passed as an argument. Requests use HTTPS with verified certificates and never follow redirects, so credentials go to the configured site only. Tokens and the `Authorization` value are removed from error text and from `--verbose` logs. If the OS credential store is unavailable, or keyring is configured with a plaintext backend, nothing is saved and the token works for that session only.

## Boards and sprints

The board opened last opens again. `b` lists the boards of the project scope, recently opened first. `p` narrows that list to one of your recent projects or a project key you type; it does not hide cards, as a Jira board shows whatever its filter selects. On a big site, listing every board takes a while; a project scope keeps it short.

- **Columns** come from the board configuration, in its order. A column can hold several statuses; cards are grouped by status ID, so renamed or same-named statuses do not mix. Configured columns without issues stay visible. A sprint issue whose status no column holds is shown under "Not on a board column" rather than dropped. `v` hides columns and toggles compact cards, per board, on this computer.
- **Scrum boards** open their active sprint, or the sprint you chose there in this session. Without an active sprint, the board shows nothing as current: it says so, and the Sprint list offers future and then closed sprints, newest first. `B` opens the backlog.
- **Kanban boards** have no sprint list. They show every issue of the board; the board's sub-filter (for example hiding released work) is passed to Jira as the JQL it is. Jira offers a kanban backlog only when it is turned on in the board settings; otherwise `B` says there is none.
- **Order** is Jira's rank order, as the enhanced board endpoints return it. The cards show key, type, summary, assignee, status and priority. `/` filters by key, summary, assignee, status or type; `M` shows only your issues; `x` clears both.
- The board is flat: Jira's swimlane settings are not in its public board API.

Auto-refresh reads the board every 10 seconds, retries rate-limited reads at most twice after Jira's `Retry-After` (up to 10 s), and backs off while Jira fails.

## Details, comments and changes

Moving the cursor never sends a request. `Enter` expands the details and reads the description and every comment (newest first, with author, time, edited mark and role or group visibility). Descriptions and comments are Atlassian Document Format: headings, lists, task lists, quotes, panels, code, tables, links, mentions, dates, status lozenges and smart links are kept; attachments and media show their file name. `s` shows the text as plain source. Unknown content keeps its text or names its type.

- `m` lists the issue's workflow transitions as Jira has them now, with the transition into the next column preselected. A transition that requires a choice field, such as the resolution, asks for it. One that requires anything else (time spent, text, a multi-value field) is not applied: the status line names the field and `o` opens the issue in Jira.
- `a` lists the people Jira can assign the issue to, you first, then people on this board, then everyone else, and Unassigned. On very large sites the list holds the first 1,000.
- `c` adds a plain-text comment. If saving fails, the text is kept: `c` brings it back.

Each change is a single request and is never retried automatically; after a connection failure, refresh before trying again. The board is read again after a transition or assignment.

## GitLab merge requests

`g` lists the open GitLab merge requests whose title, source branch or description names the selected issue's key as a whole word (`WEB-1`, never `WEB-10`), read-only, as on the [YouTrack board](../README.md#gitlab-merge-requests). `G` lists every open MR of the repository, for the selected issue's project, else the project chosen with `p`, else the board's own project; a board spanning several projects asks you to choose one with `p`. `i` on an MR opens the Jira issue it names: on the board, its details expand; otherwise it opens in the browser. `u` changes the repository.

The first `g` for a Jira project asks for its GitLab repository, or, when the board started inside a Git checkout with a GitLab remote, uses that remote. The repository is saved only once GitLab, through glab, shows it to you, per Jira site and project key. Requires glab, signed in to the GitLab host; the board itself never needs it. In `issueboard --demo jira`, WEB's repository is chosen already: `g` on WEB-2 lists one MR, and the demo's GitLab is offline.

## Saved state

Per site, in `$XDG_STATE_HOME/jira-board/state.json` (or `~/.local/state/jira-board/state.json`): when you last opened each board, the project scope, each board's view options, and each project's GitLab repository (`repos`, keyed by project key). No tokens. `issueboard` saves the folder's site, board, board name and project scope in its folder list, never credentials.

## Out of scope

Jira Data Center and Server, board administration, creating or ranking issues, changing sprint membership, Jira's swimlanes, and transition screens with fields other than single choices.
