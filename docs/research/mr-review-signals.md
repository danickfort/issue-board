# Review signals in the merge request panel

Status: research only, no code. Which review signals the shared MR panel (`gitlab_board/merge_requests.py`, used by both boards) lacks, ranked by value against cost. Researched on 2026-10-16 against GitLab.com 19.6.0-pre (EE), GitLab's source at `master` commit `97fb12911e42c678f9b48d86e626fb7352fb971b`, and glab 1.115.0. Live checks were GraphQL queries through `glab api graphql` on public `gitlab-org/cli` only. This report copies no titles, bodies or usernames.

Evidence labels as in [gitlab-board.md](gitlab-board.md): **Docs**, **Source**, **Observed**, **Inferred**. "CE" below means the field is defined in `app/graphql/types/merge_request_type.rb` and exists on every tier. "EE" means it is defined in `ee/app/graphql/ee/types/merge_request_type.rb` and is missing from the gitlab.liip.ch (CE 19.4.2) schema. Tier comes from where the source defines the field. The parent's live CE introspection confirms it.

## Already shown

| Signal | Field | Note |
| --- | --- | --- |
| Open, being merged | `state` (`locked`) | |
| Draft, conflicts, branches, fork | `draft`, `conflicts`, `sourceBranch`, `targetBranch`, `sourceProject` | |
| Pipeline | `headPipeline { status detailedStatus { label } }` | The label already tells "failed", "passed with warnings" (a job allowed to fail failed: `Status::SuccessWarning`) and "waiting for manual action" (a blocking manual job: `Status::Pipeline::Blocked`) apart. **Source** `lib/gitlab/ci/status/` |
| The first failing merge check | `detailedMergeStatus` | One value only (see mergeability checks below) |
| Approvals left (Premium) | `approvalsLeft`, `approvalsRequired` | EE |
| Last update | `updatedAt` | |
| Comments, threads resolved (implemented in source) | `userNotesCount`, `resolvableDiscussionsCount`, `resolvedDiscussionsCount` | CE. Definitions below |

### Comments, notes and threads, defined

- A **comment** is a user note: a `Note` with `system: false`. System notes are GitLab's activity log ("added 1 commit", label changes). `userNotesCount` counts every user note on the MR: general comments, diff comments and replies. It uses `Note.count_for_collection`, which starts from the `user` scope (`where(system: false)`). **Source** `app/models/note.rb`, `app/graphql/resolvers/user_notes_count_resolver.rb`.
- A **thread** is the set of notes that share a `discussion_id`. A thread is **resolvable** if it holds a non-system `DiffNote` or `DiscussionNote` (`ResolvableNote::RESOLVABLE_TYPES`). A plain standalone comment is not resolvable. `resolvableDiscussionsCount` counts distinct `discussion_id`s over resolvable notes, and `resolvedDiscussionsCount` counts the same over resolvable notes with `resolved_at` set. **Source** `app/models/concerns/resolvable_note.rb`, `merge_request_type.rb#notes_count_for_collection`.
- Unresolved threads = resolvable − resolved. GitLab's own MR list shows exactly this pair as "N of M threads resolved". **Source** `app/assets/javascripts/merge_requests/list/components/discussions_badge.vue`.
- `mergeableDiscussionsState` (REST: `blocking_discussions_resolved`) is `true` whenever the project does not require resolved threads (`return true unless only_allow_merge_if_all_discussions_are_resolved?`). It is a merge condition, not a count, so do not show it. `detailedMergeStatus: DISCUSSIONS_NOT_RESOLVED` already says when threads block a merge. **Source** `app/models/merge_request.rb#mergeable_discussions_state?`, `lib/api/entities/merge_request_basic.rb`.
- No per-MR discussion read is needed. All three counts are `BatchLoader` aggregates: one grouped SQL count per field for all MRs in the response. **Source**. Observed on `!4118`: comments 6, threads 0 of 1 resolved (parent's query).
- Internal notes count too. `count_for_collection` filters `system` but not `internal`, and MRs support internal notes (**Docs** `user/discussions/_index.md#add-an-internal-note`). A viewer who cannot read internal notes may see a count that includes them. **Inferred** from source. GitLab returns this number itself, so showing it exposes nothing new.

## Ranked missing signals

Costs are the GraphQL complexity added at `first: 100` on GitLab.com (limit 250 for authenticated requests, **Docs** `api/graphql/_index.md`). Every recommended field is CE and does no Gitaly call (no `calls_gitaly: true` in the source).

| # | Signal | Fields | Tier | Where | Why it matters |
| --- | --- | --- | --- | --- | --- |
| 1 | Reviewers and their state | `reviewers { nodes { username name mergeRequestInteraction { reviewState } } }` | CE. Free allows one reviewer, Premium several (**Docs** reviews `#request-a-review`) | Summary: `changes requested` when any reviewer has `REQUESTED_CHANGES`. Details: `Reviewers: Ann (approved), Bo (awaiting review)` | Says who acts next. On CE it is the **only** way to see a change request: `detailedMergeStatus: REQUESTED_CHANGES` comes from a Premium check ("Prevent merge when you request changes", Premium, **Docs**). Observed on `!4118`: `UNREVIEWED`, `APPROVED` |
| 2 | Approvals received | `approvedBy { nodes { username name } }` | CE | Details: EE `1 of 1 still needed · approved by Ann`; CE `approved by Ann` | On Free, approvals are optional and do not block (**Docs** approvals). Today CE shows no approval at all. Observed on `!4118`: one approval while `approvalsLeft` is 1 of 1, so "approved by" does not mean the rules are met |
| 3 | Auto-merge and earliest merge time | `autoMergeEnabled`, `mergeUser { username name }`, `mergeAfter` | CE (`mergeAfter` since 17.6, **Docs** auto_merge) | Summary: `auto-merge on`. Details: `Auto-merge: on, set by Ann`, `Merge not before 2026-10-20 08:00 UTC` while in the future | An MR set to auto-merge merges when its checks pass, with nobody acting. `mergeUser` is the "user who merged the merge request or set it to auto-merge" (**Source**) |
| 4 | Author | `author { username name }` | CE | Details only | Cheap. Answers "whose MR is this" without the browser |
| 5 | Blocking MRs (optional) | `blockingMergeRequests { totalCount hiddenCount visibleMergeRequests { reference state } }` | EE, Premium (**Docs** dependencies) | Details: `Blocked by !12, group/x!3 (+1 hidden)` | `BLOCKED_STATUS` already says "blocked by another MR". This only names which. Add it only if teams use MR dependencies |

Cost of 1–4: the GitLab board's issue query goes from 86 to 130. Of that, 1 costs +20 (**Observed**).

### Wording and unknown-versus-clear

- A field that is missing, `null` or not readable produces no word. It never becomes "none", "0" or "approved". This follows the module's existing rule ("never claim more than GitLab says").
- `reviewState` words follow GitLab's sidebar (**Docs** approvals `#individual-reviewer-status`, **Source** `MergeRequestReviewStateEnum`): `UNREVIEWED` awaiting review, `REVIEW_STARTED` reviewing, `REVIEWED` commented, `REQUESTED_CHANGES` requested changes, `APPROVED` approved, `UNAPPROVED` removed approval. An unknown value is lower-cased, as `merge_status` does. A `null` interaction shows the name alone.
- An empty `reviewers` list is a clear fact: "no reviewers". An empty `approvedBy` list is not. GitLab hides approver information from non-members when an approver comes from a private group (**Docs** approvals `#for-a-single-merge-request`). Show approvals only when the list has at least one entry, or as `none yet` on EE when `approvalsRequired` > 0. Whether `approvedBy` itself is filtered for non-members was not checked (**Inferred** caution).
- Do not use the `approved` boolean. On CE it means "has at least one approval" (`Approvable#approved?` is `approvals.present?`). EE overrides it to mean "the rules are met". The same word would mean two different things. **Source** `app/models/concerns/approvable.rb`.
- Under REQUESTED_CHANGES on CE, show "changes requested" as review information, not as a merge blocker. On CE it does not block (**Inferred** from the Premium tier of the blocking check).
- `autoMergeEnabled: false` and a past or `null` `mergeAfter` produce no word.

### Not worth adding

| Candidate | Why not |
| --- | --- |
| Stale pipeline versus head SHA | GitLab already handles it. `headPipeline` is `diff_head_pipeline`, which returns the pipeline only if it `matches_sha_or_source_sha?(diff_head_sha)` and otherwise returns `null`, which the panel shows as "no pipeline" (**Source** `merge_request.rb`). Do **not** compare `headPipeline.sha` to `diffHeadSha` in the app. On `!4118` they differ while GitLab returns the pipeline as current, because merged-results pipelines run on a merge commit (**Observed**) |
| Failed versus manual jobs | The pipeline label already distinguishes them (table above). Job names need a nested `jobs` connection per MR, which multiplies complexity. Enter/`o` opens the MR page for that |
| Behind target / rebase | When a rebase blocks (fast-forward projects), `detailedMergeStatus: NEED_REBASE` already says so (`should_be_rebased?` is `ff_merge_must_be_possible? && !ff_merge_possible?`). `divergedFromTargetBranch` calls Gitaly per MR and is only informational: on `!4118` it is `true` while the rebase check is `INACTIVE` (**Observed**) |
| Every mergeability check | `mergeabilityChecks { identifier status }` (CE, an experiment since 16.5) lists 18 checks on `!4118`, two `FAILED` (threads, approval), while `detailedMergeStatus` names only the first (**Observed**). With the thread counts and approvals shown, the extra checks mostly repeat what the panel says, and an experiment field can change. If it is ever added, `detailedMergeStatus` reuses its results, so selecting both costs one evaluation (**Source**, comment in `merge_request_type.rb`) |
| Merge train position | Premium (`mergeTrainIndex`, `mergeTrainCar`). `locked` and auto-merge already cover "about to merge" |
| `mergeRequestInteraction.canMerge`, upvotes, labels, diff stats | `canMerge` runs a permission check per reviewer. The others are not review blockers |

## Query budget and the YouTrack path

| Query | Now (with counts) | With 1–4 |
| --- | --- | --- |
| GitLab board: Development widget, two connections of 100 | 86 | 130 |
| YouTrack board: `project.mergeRequests(iids:, first: 100)` | 184 | **316, refused** ("exceeds max complexity of 250") |
| Same, `first: 50` | 109 | 186 |
| Same, `first: 40` | 94 | 160 |

Observed on GitLab.com with one actual MR in the response. The score depends on the requested `first`, not on the number of MRs returned. `project_merge_requests` sends `first: len(batch)` with batches of up to `FIRST` (100) story-matching MRs. Adding 1–4 therefore needs a status batch of 50 for that query, which still needs only one request below 50 matches. Without that change, a project with more than about 75 matching open MRs would fail (**Inferred**: about 2.6 points per requested MR between `first: 40` and `first: 50`). Self-managed instances use the same default limit unless an admin changes it.

`MR_EE` falls back to `MR` only for EE fields: `Client.ask` retries with core fields when a field "doesn't exist on type". Put 1–4 in `MR` and 5 in `MR_EE`. A CE field missing from an older self-managed GitLab would fail both attempts. The parent's introspection of gitlab.liip.ch (19.4.2) confirms 1–4 there; older versions were not checked.

## Implemented follow-up

The source version now shows `autoMergeEnabled`, author, reviewers with their review state, and assignees in both boards' shared MR panel. Auto-merge and requested changes appear in the list. Author, reviewer and assignee identities remain separate in the details. Unknown or inaccessible values are not shown as empty lists. The details pane scrolls with PgUp/PgDn without changing the selected MR.

The status batch is 50. A read-only GitLab.com query with the actual field selection measured complexity **200 of 250** at `first: 50`. Reviewer and assignee connections read the first 20 people each; a longer connection says “more in GitLab”. No per-person requests are made. Received approvals, auto-merge actor/time, scheduled merge times and blocking MR links remain recommendations rather than implemented features.

## Security and performance

- No new transport. These are GraphQL reads through glab's stdin, as now. No mutation, token or argv change.
- Names and usernames are user-controlled text. Render them through `plain()` and `Text`, as the module does with titles.
- GitLab authorizes each field (`read_merge_request`). `blockingMergeRequests.hiddenCount` reports blockers the viewer cannot see. Show the number only, never a guess.
- Load per panel open stays one request (GitLab board) or one status request per 50 matches (YouTrack). `reviewState` reads each MR's reviewer rows once (`Users::MergeRequestInteraction#reviewer` on the loaded association), at most the 200 MRs a panel can list. **Inferred** acceptable.

## Sources

- Source at `97fb1291`: `app/graphql/types/merge_request_type.rb`, `ee/app/graphql/ee/types/merge_request_type.rb`, `app/graphql/resolvers/user_notes_count_resolver.rb`, `app/graphql/resolvers/user_discussions_count_resolver.rb`, `app/models/note.rb`, `app/models/concerns/resolvable_note.rb`, `app/models/concerns/noteable.rb`, `app/models/concerns/approvable.rb`, `app/models/merge_request.rb`, `app/models/users/merge_request_interaction.rb`, `app/graphql/types/merge_request_review_state_enum.rb`, `app/graphql/types/merge_requests/mergeability_check_status_enum.rb`, `ee/app/graphql/types/merge_requests/blocking_merge_requests_type.rb`, `lib/api/entities/merge_request_basic.rb`, `lib/gitlab/ci/status/{success_warning,manual,pipeline/blocked}.rb`, `app/assets/javascripts/merge_request_dashboard/queries/merge_request.fragment.graphql` (GitLab's own dashboard row selects reviewers with `reviewState`, both thread counts, `mergeabilityChecks` and approvals), `app/assets/javascripts/merge_requests/list/components/discussions_badge.vue`.
- Docs at the same commit: `doc/user/project/merge_requests/approvals/_index.md`, `reviews/_index.md`, `dependencies.md`, `auto_merge.md`, `doc/ci/pipelines/merge_trains.md`, `doc/user/discussions/_index.md`, `doc/api/graphql/_index.md`.
- Observed: `gitlab-org/cli!4118` and issue `#8598` on GitLab.com, read-only GraphQL with `queryComplexity`.
