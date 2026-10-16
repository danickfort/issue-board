# GitLab Board

A keyboard-driven GitLab issue board in the terminal. It shares the YouTrack board's layout, keys, auto-refresh and view options, and talks to GitLab only through the official GitLab CLI, `glab`.

**Source checkout only.** The GitLab board is not part of the Homebrew release. Homebrew installs only the `youtrack-board` command, and it is not changed by this. There is no installed `gitlab-board` command; run the board from a checkout with `uv run python -m gitlab_board`.

Design, GitLab behavior, and limits are in [docs/research/gitlab-board.md](research/gitlab-board.md).

## Requirements

- Python 3.12 or later, and [uv](https://docs.astral.sh/uv/).
- The GitLab CLI, signed in to your GitLab host. The board uses glab's own sign-in: it never asks for, reads, stores or prints a token.

```sh
brew install glab                          # or see https://gitlab.com/gitlab-org/cli#installation
glab auth login --hostname gitlab.com      # once; choose a token or the browser flow glab offers
glab auth status --hostname gitlab.com     # should say "Logged in to gitlab.com"
```

For a self-managed GitLab, use its host name instead of `gitlab.com`.

## Set up the checkout

From the repository directory:

```sh
uv sync
```

## Try the demo

```sh
uv run python -m gitlab_board --demo
```

No GitLab account, glab or network is needed. The demo never runs glab and never reads or writes saved state; changes disappear on exit. It has a project board with label lists (*Development*, where one issue sits in two lists), a scoped sprint board with status lists and a WIP limit (*Permits – Sprint 10*), and a group board with assignee and milestone lists (*Team planning*). Press `e` on *Development* for epic swimlanes, and `B` on its Open list to page through 122 issues.

To see how the app reads a board URL whose filters repeat, as the GitLab example does:

```sh
uv run python -m gitlab_board --demo 'https://gitlab.example.com/demo/portal/-/boards/2?milestone_title=Sprint+9&label_name[]=Permits&milestone_title=Sprint+10'
```

## Open a board

Pass the board URL as GitLab shows it in the browser. Quote it: `&`, `[` and `]` mean something to the shell.

```sh
uv run python -m gitlab_board 'https://gitlab.com/geocity/geocity/-/boards/11511337?milestone_title=Sprint+9&label_name[]=PolCo&milestone_title=Sprint+10#/'
```

Other forms:

```sh
uv run python -m gitlab_board 'https://gitlab.com/groups/my-group/-/boards/42'   # a group board
uv run python -m gitlab_board 'https://gitlab.com/my-group/my-project'           # the project's most recent board
uv run python -m gitlab_board my-group/my-project --hostname gitlab.example.com  # a path on another host
uv run python -m gitlab_board                                                    # reopen the last board on gitlab.com
uv run python -m gitlab_board --auth-status                                      # check glab and its sign-in
```

The URL's filters apply as on GitLab's board page. A key given twice keeps its last value, and a scoped board's own milestone, labels, assignee, weight and iteration are added. The line under the toolbar shows the filters in effect and warns when the URL was adjusted. URL parameters the app does not apply (custom fields, for example) stop it before it starts, naming each one, instead of showing more issues than the URL meant.

If glab is missing or not signed in, the app says so and prints the `glab` command to run.

## Keys

| Key | Does |
| --- | --- |
| `←` `→` / `h` `l` | Previous or next list |
| `↑` `↓` / `j` `k` | Previous or next card; past a lane's edge, the same list in the next swimlane |
| `Enter`, `Esc` | Expand the details; back to the board |
| `/`, `x` | Filter loaded cards by `#`, title, assignee or label; clear local filters |
| `M`, `v` | Only issues assigned to me; view options: compact cards, epic swimlanes, hidden lists |
| `e` | Epic swimlanes on or off for this board (GitLab Premium or Ultimate) |
| `m` | Move the card to another list. Each choice shows what GitLab changes: labels, open or closed, assignee, milestone, iteration or status |
| `a` | Assign a project member, or unassign. Replaces all current assignees |
| `c`, `C` | Write a one-line comment; read comments, newest first |
| `s`, `o` | Markdown source or formatted text; open the issue in the browser |
| `B` | The focused list in full, 50 issues at a time (Open is GitLab's backlog list), read-only |
| `b`, `p` | Another board here; another project or group, or a URL or path |
| `r`, `R` | Reload; auto-refresh every 5 s on or off for this board |
| `?`, `q` | All keys; quit |

## What is saved

`$XDG_STATE_HOME/gitlab-board/state.json` (or `~/.local/state/gitlab-board/state.json`), one section per GitLab host: when each board was opened, the project or group you used last, and each board's view options. It holds board paths, timestamps and list titles, never tokens or issue content. Nothing in GitLab changes when you change a view. The demo and tests never touch this file.

## Development

```sh
uv run pytest -q                       # YouTrack and GitLab tests; no network, no glab sign-in needed
uv run pytest -q tests/test_gitlab.py  # GitLab only
```

The GitLab tests run a fake `glab` executable to check argv, stdin, environment, errors and process cleanup, and drive the real TUI against the offline demo at 80×24 and 60×20.
