# Open merge requests linked to a story

Status: implemented in both boards. YouTrack MR support ships in 0.1.2; GitLab Board remains source-only. The findings and design below were written before the code; results are at the end. Stage 1 is the GitLab board, stage 2 the YouTrack board. Researched on 2026-10-16 against GitLab.com 19.6.0-pre (Enterprise Edition), gitlab.liip.ch 19.4.2 (Community Edition), glab 1.115.0, GitLab's source at `master` commit `042d7c216cf9dee29ab0cd7a26804009e995b21e`, and track.liip.ch. Every live check was a GraphQL query or a REST GET.

## What the user asked for

Press a key on a card and see the open GitLab merge requests (MRs) linked to it, with their status: draft, pipeline, mergeability, conflicts, branches. On the GitLab board this works without setup. On the YouTrack board, the first use for a YouTrack project asks for the GitLab repository URL and remembers it. It needs `glab`. The panel only reads: no merge, rebase, approve or pipeline action.

## Findings

Evidence labels as in [docs/research/gitlab-board.md](../research/gitlab-board.md): Docs, Source, Observed, Inferred.

### GitLab: which MRs belong to an issue

| Source of the link | API | Covers | Evidence |
| --- | --- | --- | --- |
| References | GraphQL `Issue.relatedMergeRequests`; the work item `Development` widget's `relatedMergeRequests` | MRs referenced in the issue's description and notes, including the "mentioned in merge request !N" system notes GitLab writes when an MR description, comment or commit mentions the issue. Cross-project MRs included. Filtered to MRs the account may read | Source: `Issues::ReferencedMergeRequestsService#referenced_merge_requests`, `Resolvers::MergeRequests::IssueRelatedResolver`. Docs: crosslinking issues |
| Explicit "related" links | Same widget field, through `related_merge_request_ids` | Adds `merge_requests_closing_issues` rows with `link_type: related`, behind the `explicit_mr_work_item_relations` feature flag | Source: `WorkItemRelatedResolver`, `MergeRequestIssue` (`closes`, `mentioned`, `related`) |
| Closing MRs | Development widget `closingMergeRequests` | MRs that close the issue when merged: from the closing pattern in the MR description (`fromMrDescription`) or added by hand | Source: `DevelopmentType`, `ClosingMergeRequestType`. Observed |
| REST | `GET …/issues/:iid/related_merge_requests`, `GET …/issues/:iid/closed_by` | The same union as the widget; `closed_by` only lists MRs **in the issue's own project** (`MergeRequestsFinder` with `project_id: user_project.id`) | Source: `lib/api/issues.rb` |

GitLab's own issue page queries the Development widget with both `closingMergeRequests` and `relatedMergeRequests` (`work_items/graphql/work_item_development.fragment.graphql`, Source). The app sends the same two connections in one GraphQL query, so the panel lists what the issue page lists. Neither connection takes a state filter: the app keeps `opened` and `locked` (being merged) and counts the merged and closed ones it leaves out.

Observed on public `gitlab-org/cli` issues (references only, no text copied): `#8623` has closing and related `!4134`; `#8598` has open `!4118` and merged `!4051`; `#8565` has two related MRs and no closing one; MRs from forks (`sourceProject` differs from `project`) are included and target the issue's project. The REST endpoints returned the same MRs. Query complexity: 83 of 250 with 100 MRs per connection.

Coverage limits, stated in the panel and the guides:

- MRs the account cannot read are left out by GitLab. The panel cannot tell that they exist.
- A branch named after the issue, without an MR, is not an MR (GitLab lists such branches separately as `relatedBranches`; not shown).
- `relatedMergeRequests` "can only be resolved for one issue in any single request" (Source: `FieldCallCount limit: 1`), so the MRs are read for the selected card only, when the panel opens.
- Up to 100 MRs per connection. More is reported, not paged (shortcut; no issue in the samples had more than 3).
- The widget's `relatedMergeRequests` is marked experimental (17.6). The core fallback is `Issue.relatedMergeRequests`, which is stable but misses explicit `related` links. Both servers accept the widget field today (Observed).

### GitLab: MR status

From the live schema (Observed on both hosts):

- `state`: `opened`, `locked` (a merge in progress), `merged`, `closed`.
- `draft`, `conflicts`, `sourceBranch`, `targetBranch`, `project.fullPath` (target), `sourceProject.fullPath` (a fork when different).
- `detailedMergeStatus` (24 values, for example `MERGEABLE`, `CHECKING`, `UNCHECKED`, `CI_MUST_PASS`, `CI_STILL_RUNNING`, `DISCUSSIONS_NOT_RESOLVED`, `NOT_APPROVED`, `REQUESTED_CHANGES`, `CONFLICT`, `NEED_REBASE`). GitLab computes mergeability asynchronously; a list "might not proactively update" it (Docs, merge requests API). Observed: 8 of the 12 open MRs sampled in `gitlab-org/cli` were `unchecked`. The panel says "not checked yet" or "checking", never "mergeable", for those.
- `headPipeline { status detailedStatus { label } }`. `null` means no pipeline, or none the account may see. The panel says "no pipeline"; it never treats a missing pipeline as passed.
- `approvalsLeft`, `approvalsRequired`: Premium only. gitlab.liip.ch (CE) answers "Field 'approvalsLeft' doesn't exist on type 'MergeRequest'" (Observed). The client's existing EE fallback drops them, and `detailedMergeStatus` still reports `NOT_APPROVED`.

### YouTrack: which MRs belong to a story

- YouTrack's GitLab integration links commits and MRs to issues by ID: in commit messages, in the MR title or description, and optionally in branch names. It only recognizes prefixes starting with an uppercase letter (Docs: [GitLab integration](https://www.jetbrains.com/help/youtrack/cloud/integration-with-gitlab.html)).
- The links are readable at `GET /api/issues/{id}/vcsChanges`, where MRs are entries with `$type: PullRequest` and a `url` (Docs: [Issue VCS changes](https://www.jetbrains.com/help/youtrack/devportal/resource-api-issues-issueID-vcsChanges.html), [PullRequest](https://www.jetbrains.com/help/youtrack/devportal/api-entity-PullRequest.html)).
- Observed on track.liip.ch: the 400 most recently updated issues with VCS changes hold 4,461 links, **all commits** (`VcsChange` on gitlab.liip.ch) and **no `PullRequest`**. YouTrack's native MR links are not populated on the user's server, so reading them would add code that finds nothing there. Not used; listed as future work.
- A repository URL alone cannot prove that an MR belongs to a story. The app therefore uses the rule YouTrack's own integration uses, labelled as such: an open MR in the mapped repository whose **title, source branch or description** contains the issue ID as a whole token. `DEMO-1` matches `DEMO-1`, `demo-1-fix` and `[DEMO-1]`, never `DEMO-10` or `XDEMO-1`. Matching ignores case, because branch names are usually lower case.

Coverage limits, stated in the panel and the guide: only the mapped project; only open MRs (`state: opened`; a `locked` MR is skipped for the moment it is being merged); MRs from forks are included because they target the mapped project; the first 500 open MRs of the project are scanned (shortcut, reported when cut); MRs in other repositories, or that mention the story only in comments or commits, are not found.

### Tokens and processes

- GitLab requests go through glab with its own sign-in (`gitlab_board/glab.py`): fixed argv, GraphQL on stdin, 30 s limit, killed on cancel, tokens redacted from errors. No token is read, stored or passed.
- The YouTrack board does not need glab to start or for `--demo`. On the first use for a project it checks that `glab` is on `PATH` (no subprocess) before asking for the URL, and validates the URL with a GraphQL read of the project through glab. A missing sign-in comes back as glab's 401, shown with the `glab auth login --hostname <host>` command. Nothing blocks the UI: every call runs in a worker that closing the panel cancels.
- The mapping is saved in the YouTrack app's state file, in the section of the YouTrack server, as `repos: {<project id>: "https://<host>/<group>/<project>"}`. Other servers' sections stay as they are (`Prefs.save` replaces only its own).

## Design

**One shared panel, one shared module.** `gitlab_board/merge_requests.py` holds the status wording (pure functions) and `MergeRequestsScreen`, a modal like the comments screen: a list of open MRs with a status line each, details for the highlighted one, `r` reload, `Enter`/`o` open in the browser, `Esc`/`g` back to the same card. It loads in a screen worker, so closing it cancels glab and drops a late answer. The board's auto-refresh already pauses while a modal is open, and MRs are read only when the panel opens or on `r`.

**Key `g`** (for Git; free in both apps). Listed in `?` and in the details pane's title.

**Browser.** `o` opens an MR's `webUrl` only if it is `https://<host>/<project fullPath>/-/merge_requests/<iid>`, with the host of the board (GitLab) or the mapping (YouTrack). That allows cross-project MRs on the same host and nothing else.

**Client.** In `gitlab_board/client.py`: `issue_merge_requests(issue)` (stage 1, the Development widget query) and `project_merge_requests(path, key)` (stage 2: open MRs of the project, 100 per page by cursor, then the status fields for the matches only, by `iids`). The demo client answers both from fictional fixtures.

**YouTrack.** `BoardApp.action_merge_requests`: no mapping → check glab (skipped in the demo) → the GitLab project of the checkout the board started in, if exactly one and not linked to another project (`find_repository`, see [the startup findings](../research/gitlab-board.md#starting-in-a-code-checkout-and-the-default-board)), else a choice or the URL dialog → panel that validates, saves and loads. With a mapping → panel. `u` in the panel asks for another URL, prefilled; nothing is saved until GitLab confirms the project. YouTrack imports the GitLab modules only when `g` is pressed.

**Packaging.** The wheel and sdist currently hold only `youtrack_board`. Stage 2 adds the GitLab modules the YouTrack board imports (`__init__`, `glab`, `client`, `target`, `demo`, `merge_requests`) and not `gitlab_board/app.py` or `__main__.py`, so no GitLab command is installed or advertised. Verified by building the wheel and importing from a temporary environment outside the checkout. The version stays 0.1.1 and nothing is published.

## Order of work

1. Stage 1: client query, demo fixtures, shared screen, `g` on the GitLab board. Tests with a fake glab and the offline TUI; a read-only live run against a public issue. Full GitLab test file green before stage 2.
2. Stage 2: prefs `repos`, YouTrack action and prompt, project MR scan, packaging. Tests for the mapping (prompt, save, reuse, invalid, cancel, missing glab, auth error, server and project isolation) and the TUI at 80×24 and 60×20. Full suite.
3. Docs: README, GitLab guide, this file's results.

## Not done

- YouTrack's native `PullRequest` links (none on the user's server), commits-to-MR lookups, MRs in more than one repository per YouTrack project.
- A count badge on cards: it would cost one request per card per refresh.
- Any MR action: merge, rebase, approve, retry a pipeline.

## Results

Code: `gitlab_board/merge_requests.py` (panel, status words, URL checks), `gitlab_board/client.py` (`issue_merge_requests`, `project_merge_requests`, `mentions`, `issue_links`), `gitlab_board/demo.py` (fixtures), `gitlab_board/app.py` and `youtrack_board/app.py` (`g`), `youtrack_board/prefs.py` (`repos`), `youtrack_board/demo.py` (`demo_gitlab`), `pyproject.toml` (packaging).

Stage 1, GitLab board:

- Tests (`tests/test_merge_requests.py`): the query document and the Community Edition fallback; closing and mentioning MRs merged once, cross-project and fork MRs kept; the truncation note; glab argv, stdin and environment through a fake `glab` executable; status words for unchecked, checking, no pipeline, draft, conflicts, locked and unknown values; browser URLs limited to MR pages on the host; the panel at 80×24 and 60×20 with focus, card and scroll kept on return; cancelling on close and on another card; no MR reads from auto-refresh; errors, empty results and retry.
- Live, read-only, through the real client: `gitlab-org/cli#8598` lists open `!4118` (closes it) and merged `!4051`; `#8565` lists a fork MR (`mergeability not checked yet`) and `!3955` (`changes requested · conflicts`); `#8623` a fork MR that needs approval. The real TUI on the example board opened the panel on three cards (closed issues with merged MRs only, shown as "No open · N merged or closed not shown"): 7 requests, all GraphQL queries, under a guard that refused anything else.

Stage 2, YouTrack board:

- Tests: whole-token matching (`DEMO-1` never `DEMO-10`, `XDEMO-1`, `DEMO-1a`); repository URLs (nested paths, any project page, clone URL, port; groups, credentials and bad paths refused); the cursor scan, the status query for matches only, the 500-MR cap, a missing project; the story ID kept out of argv and stdin; `repos` kept per server and project, malformed values dropped; the TUI: the panel on a mapped project at 80×24 and 60×20, first use with cancel, a group URL refused, an unknown project not saved, `u` prefilled, a working URL saved and reused without a new check, another project asking for its own, another server's section untouched; glab missing (no prompt) and signed out (401 from a fake `glab` on `PATH`, nothing saved); the board starting without importing GitLab code; the package shipping every GitLab module `g` imports.
- Live, read-only: on track.liip.ch, 6 unresolved stories whose commits live in 3 gitlab.liip.ch repositories had no open MR naming them; a fourth repository was refused by validation (not visible to the account). On gitlab.liip.ch (CE 19.4.2), 5 open MRs whose title or branch carries a story ID were each found by the scan of their project, none matched a longer ID, and the status query fell back to core fields. All 17 GitLab requests were GraphQL queries or one REST GET.
- Packaging: the wheel and sdist, and a wheel built from the sdist as Homebrew builds it, hold the six GitLab modules and not `gitlab_board/app.py`, `__main__.py` or `markup.py`. Installed into a temporary environment and run from another directory, the YouTrack demo's `g` listed DEMO-3's MRs from `site-packages`; `python -m gitlab_board` does not run there and the only command is `youtrack-board`.

Not verified: the YouTrack TUI against track.liip.ch with a real repository (the client path it runs was checked live, the screen offline); GitLab Premium approval fields on a live MR in the YouTrack flow; issues with more than 100 linked MRs.
