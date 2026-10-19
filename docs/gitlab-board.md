# GitLab Board

A keyboard-driven GitLab issue board in the terminal. It shares the YouTrack board's layout, keys, auto-refresh and view options, and talks to GitLab only through the official GitLab CLI, `glab`.

## Installation

The application and tap repositories are private. Your GitHub account needs access to both `krabilousse/gitlab-board` and `krabilousse/homebrew-gitlab`.

```sh
brew install gh
gh auth login
gh auth setup-git
HOMEBREW_GITHUB_API_TOKEN="$(gh auth token)" brew install krabilousse/gitlab/gitlab-board
```

Skip GitHub login if already signed in with access. Homebrew installs Python and the official GitLab CLI, `glab`; no uv or source checkout is needed to run the app. This package installs only `gitlab-board` and can coexist with YouTrack Board.

To update or uninstall:

```sh
brew update
HOMEBREW_GITHUB_API_TOKEN="$(gh auth token)" brew upgrade krabilousse/gitlab/gitlab-board
brew uninstall gitlab-board
```

Uninstalling keeps local preferences and glab authentication. Start inside your code repository with `gitlab-board`, or pass a board URL from any directory.

Design, GitLab behavior, and limits are in [docs/research/gitlab-board.md](research/gitlab-board.md).

## Requirements

- The GitLab CLI, signed in to your GitLab host. The board uses glab's own sign-in: it never asks for, reads, stores or prints a token.

```sh
brew install glab                          # or see https://gitlab.com/gitlab-org/cli#installation
glab auth login --hostname gitlab.com      # once; choose a token or the browser flow glab offers
glab auth status --hostname gitlab.com     # should say "Logged in to gitlab.com"
```

For a self-managed GitLab, use its host name instead of `gitlab.com`.

## Try the demo

```sh
gitlab-board --demo
```

No GitLab account, glab or network is needed. The demo never runs glab and never reads or writes saved state; changes disappear on exit. It has a project board with label lists (*Development*, where one issue sits in two lists), a scoped sprint board with status lists and a WIP limit (*Permits – Sprint 10*), and a group board with assignee and milestone lists (*Team planning*). Press `e` on *Development* for epic swimlanes, `B` on its Open list to page through 122 issues, `Enter` on #3 (*Doing*) for its details with its comments, and `g` there for its merge requests. `G` lists every open merge request of the project; `i` on one opens the issue it links.

To see how the app reads a board URL whose filters repeat, as the GitLab example does:

```sh
gitlab-board --demo 'https://gitlab.example.com/demo/portal/-/boards/2?milestone_title=Sprint+9&label_name[]=Permits&milestone_title=Sprint+10'
```

## Open a board

Pass the board URL as GitLab shows it in the browser. Quote it: `&`, `[` and `]` mean something to the shell.

```sh
gitlab-board 'https://gitlab.com/geocity/geocity/-/boards/11511337?milestone_title=Sprint+9&label_name[]=PolCo&milestone_title=Sprint+10#/'
```

Other forms:

```sh
gitlab-board 'https://gitlab.com/groups/my-group/-/boards/42'   # a group board
gitlab-board 'https://gitlab.com/my-group/my-project'           # the project's default board, else its most recent
gitlab-board my-group/my-project --hostname gitlab.example.com  # a path on another host
gitlab-board                                                    # in a code checkout: its GitLab project, else the last board
gitlab-board --auth-status                                      # check glab and its sign-in
```

The URL's filters apply as on GitLab's board page. A key given twice keeps its last value, and a scoped board's own milestone, labels, assignee, weight and iteration are added. The line under the toolbar shows the filters in effect and warns when the URL was adjusted. URL parameters the app does not apply (custom fields, for example) stop it before it starts, naming each one, instead of showing more issues than the URL meant.

If glab is missing or not signed in, the app says so and prints the `glab` command to run.

## Start inside a code checkout

Without a URL, the board looks at the Git checkout it starts in. Any subfolder or worktree of it works.

1. It reads the fetch URLs of `git remote -v`, as glab does to find a repository's project: HTTPS and SSH remotes (`git@host:group/subgroup/app.git`, `ssh://…`) with nested groups, and SSH host aliases from your SSH config, resolved with `ssh -G`, which does not connect. Push-only URLs are ignored. A user name or token in a remote URL is dropped and never shown.
2. It keeps the remotes on hosts glab is signed in to, asking `glab auth status --hostname` once per host. A host glab has no sign-in for, such as github.com, is left out without being contacted.
3. One GitLab project: the board opens it, on its [default board](#default-board). Several projects on the same host, such as a fork and its upstream: it asks which, unless you chose a base remote in glab before (`remote.<name>.glab-resolved-base` in the repository's Git config), which it follows. Remotes on several GitLab hosts: it stops and names them. Pass `--hostname <host>`, or a URL.

A URL on the command line always wins over the checkout. Outside a checkout, or when no remote is on a GitLab host glab is signed in to, it reopens the last place you used on `--hostname` (gitlab.com unless given), and a warning says why the checkout was not used. In a checkout, `--auth-status` checks the host of its remote and names the project found.

Only `git remote -v`, `git config --get-regexp` and `ssh -G` run in your checkout, and they only read. glab's API calls still run outside it, as before. Nothing in Git, glab, the SSH config or GitLab is changed.

## Default board

Each project and group can have a default board. It opens when you start in the project's checkout, open the project or group URL, or pick it with `p`.

- **First start in a checkout.** A project with one board opens it and makes it the default. With several boards, the app asks: *Found project group/app — choose your default board*. Your choice is saved. Escape opens no board and saves nothing; `b` then opens a board without making it the default.
- **No guessing.** GitLab has no board activity time: `Board.updatedAt` changes only with the board's own settings. So the question preselects the board you visited last on GitLab's web pages (GitLab keeps your last four visits, `recentIssueBoards`), marked *your last visit on GitLab*, else the one you opened last here. Board IDs, names and issue dates are never used to pick one.
- **Changing it.** `b` switches boards without changing the default. `D` makes the board on screen the default of its project or group, and `b` marks the default.
- **A gone default.** If the default board was deleted, or you can no longer see it, the next start says so and asks again.
- A board URL opens that board and leaves the default alone. A project or group URL without a default opens its most recently opened board, as before.

## Keys

| Key | Does |
| --- | --- |
| `←` `→` / `h` `l` | Previous or next list |
| `↑` `↓` / `j` `k` | Previous or next card; past a lane's edge, the same list in the next swimlane |
| `Enter`, `Esc` | Expand the details, with the issue's [comments](#comments-in-the-details) under them; back to the board |
| `/`, `x` | Filter loaded cards by `#`, title, assignee or label; clear local filters |
| `M`, `v` | Only issues assigned to me; view options: compact cards, epic swimlanes, hidden lists |
| `e` | Epic swimlanes on or off for this board (GitLab Premium or Ultimate) |
| `m` | Move the card to another list. Each choice shows what GitLab changes: labels, open or closed, assignee, milestone, iteration or status |
| `a` | Assign a project member, or unassign. Replaces all current assignees |
| `c`, `C` | Write a one-line comment; read comments, newest first |
| `g` | The issue's open [merge requests](#merge-requests) and their status, read-only |
| `G` | Every open merge request of the board's project or group, most recently updated first; `i` opens the issue one links |
| `s`, `o` | Markdown source or formatted text; open the issue in the browser |
| `B` | The focused list in full, 50 issues at a time (Open is GitLab's backlog list), read-only |
| `b`, `p` | Another board here; another project or group, or a URL or path |
| `D` | Make this board the [default](#default-board) of its project or group |
| `r`, `R` | Reload (on expanded details: their comments); auto-refresh every 5 s on or off for this board |
| `?`, `q` | All keys; quit |

## Comments in the details

`Enter` expands the selected issue's details and reads its comments, newest first, under the description: author, time, edited, internal notes marked, and formatted Markdown (`s` shows the source). The details scroll with `↑` `↓`, `PgUp` `PgDn`, `Home` `End`. GitLab's system notes are left out, as with `C`. Comments are read only then, and on `r` while the details are expanded, never while the cursor moves between cards and never by auto-refresh. While they load, the details say so; an empty thread or an error says so too, and `r` retries. An answer for an issue that is no longer selected is dropped. A comment written with `c` shows at once. The same works in the full list (`B`). `C` still opens the comments on their own.

## Merge requests

`g` lists the selected issue's open merge requests (MRs), the ones GitLab's issue page shows under *Development*: MRs that close the issue when merged, and MRs that mention it (in the issue's description or comments, or from an MR, a commit or another project, which GitLab records as "mentioned in"). No setup is needed. MRs in other projects of the same GitLab are included, shown as `group/project!12`; MRs from forks too.

Each MR shows whether it closes or mentions the issue, draft, pipeline (or "no pipeline"), GitLab's merge status ("ready to merge", "needs approval", "conflicts"…, "not checked yet" while GitLab has not computed it) and, on Premium, approvals. The panel also shows each MR's author, assignees and reviewers, whether auto-merge is set, its number of comments, and how many of its resolvable threads are resolved or unresolved. GitLab's system activity is not counted, and ordinary comments cannot be resolved. `PgUp` and `PgDn` scroll the selected MR's details. Merged and closed MRs are counted, not listed. `Enter` or `o` opens an MR in the browser if its address is an MR page on the board's host. `r` reloads; Escape, `g` or `G` returns to the card. MRs are read for the selected issue when the panel opens (one GraphQL query) and on `r`, never by auto-refresh. The panel only reads.

`G` lists every open MR of the board's project, or of its group and subgroups, most recently updated first, as GitLab sorts them, with the same status and details. It reads 50 at a time, up to the 500 most recently updated, and says so when there are more.

`i` opens an issue the selected MR links, in both panels. GitLab says which: the issues the MR closes when merged (`closes_issues`), then those it mentions (`related_issues`), including issues of other projects, as far as your account may see them. One issue opens at once; several are offered to choose from; none is said. An issue on the board opens in place: its card is selected and its details expand with its comments. Any other issue opens in the browser, only if its address is on the board's host. The issues are read for that MR when you press `i`.

Not listed: MRs GitLab hides from your account, and branches without an MR. Up to 100 closing and 100 mentioning MRs are read; the panel says when GitLab has more. Sources and limits: [docs/plans/linked-merge-requests.md](plans/linked-merge-requests.md).

## What is saved

`$XDG_STATE_HOME/gitlab-board/state.json` (or `~/.local/state/gitlab-board/state.json`), one section per GitLab host: when each board was opened, the project or group you used last, each project's or group's [default board](#default-board) (`defaults`), and each board's view options. It holds board paths, timestamps and list titles, never tokens or issue content. Nothing in GitLab changes when you change a view. The demo and tests never touch this file.

## Diagnostic logs

Start with `--verbose` to show a scrollable log panel (`youtrack-board --verbose` or `gitlab-board --verbose`; also works with `--demo`). **F12** hides or shows it. Tab to the panel, then use the arrow keys or Page Up / Page Down to scroll; End follows the latest entries. Logging continues while the panel is hidden. Dialogs cover the board's panel; close the dialog to read its logs.

The panel records app status, warnings and errors, and network request methods, endpoints, completion times and failures. GitLab requests show glab success/failure and HTTP status when glab supplies it; GraphQL errors are logged separately. Authentication headers, tokens, query values, request/response bodies and raw glab stderr are omitted. Logs stay in memory with bounded history (up to 1,000 rendered lines); nothing is written to disk. Without `--verbose`, no diagnostic history is collected. Startup authentication happens before the panel opens and still reports failures in the terminal.

## Development

A source checkout requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). From this repository, run `uv sync`, then `uv run gitlab-board --demo`. Use `uv run --project /path/to/checkout gitlab-board` to launch the source version from another code repository.

```sh
uv run pytest -q                       # YouTrack and GitLab tests; no network, no glab sign-in needed
uv run pytest -q tests/test_gitlab.py  # GitLab only
```

The merge request tests (`tests/test_merge_requests.py`) cover both boards. The startup tests (`tests/test_startup.py`) build real temporary Git repositories (HTTPS, SSH and aliased remotes, nested groups, subfolders, worktrees, forks, credentials in URLs) with a fake glab sign-in, and check the default board choice, cancelling, a gone default, and the YouTrack repository link. Tests start in an empty folder, so they never read this checkout's remotes or your SSH config. The GitLab tests run a fake `glab` executable to check argv, stdin, environment, errors and process cleanup, and drive the real TUI against the offline demo at 80×24 and 60×20.
