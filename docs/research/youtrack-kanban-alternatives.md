# YouTrack TUI alternatives: Kanban comparison

Checked 2026-10-16 against shallow clones of each repository's default branch. Neither implementation provides a comparable side-by-side Kanban card view at the inspected commits. This conclusion follows the actual navigation and render paths, supported by their tests and documentation.

## Versions inspected

- `omartelo/youtrack-tui`: [`fb5457961c2f110952ed35387bdf3b3ba1adba9a`](https://github.com/omartelo/youtrack-tui/commit/fb5457961c2f110952ed35387bdf3b3ba1adba9a), commit date 2026-09-11.
- `dutch-casa/youtrack`: [`0729f437ceb054e982ef91b8dd00b291a90e5dee`](https://github.com/dutch-casa/youtrack/commit/0729f437ceb054e982ef91b8dd00b291a90e5dee), commit date 2026-06-11.

## omartelo/youtrack-tui

**Issue browser, not an Agile board UI.** The complete screen enumeration contains setup, saved filters, issues, issue detail, field picker and value picker. The root renderer selects a single list or detail screen; it does not compose workflow columns. An issue list item renders its ID/summary and a secondary line of field values and update time. Those display fields are not Kanban columns.

- [Screen enumeration](https://github.com/omartelo/youtrack-tui/blob/fb5457961c2f110952ed35387bdf3b3ba1adba9a/internal/ui/app.go#L21-L30).
- [Complete screen renderer](https://github.com/omartelo/youtrack-tui/blob/fb5457961c2f110952ed35387bdf3b3ba1adba9a/internal/ui/app.go#L716-L739).
- [Issue item rendering](https://github.com/omartelo/youtrack-tui/blob/fb5457961c2f110952ed35387bdf3b3ba1adba9a/internal/ui/render.go#L106-L140).

Its field/value picker can change State or another supported single-value field. It reads project bundle options, not available state-machine transition actions; its writer explicitly notes that a listed value can still be rejected by workflow rules. `StateMachineIssueCustomField` is absent from its writable type map.

- [Supported types and bundle fetching](https://github.com/omartelo/youtrack-tui/blob/fb5457961c2f110952ed35387bdf3b3ba1adba9a/internal/youtrack/write.go#L16-L38).
- [Bundle options and write behavior](https://github.com/omartelo/youtrack-tui/blob/fb5457961c2f110952ed35387bdf3b3ba1adba9a/internal/youtrack/write.go#L90-L126).
- [State field edit test](https://github.com/omartelo/youtrack-tui/blob/fb5457961c2f110952ed35387bdf3b3ba1adba9a/internal/ui/edit_test.go#L118-L133).

## dutch-casa/youtrack

**Agile board/sprint metadata browser, not a Kanban renderer.** There is an Agile section, but its path is:

1. List Agile boards as generic resource rows.
2. Enter on a board fetches its sprints and replaces the resource list.
3. Select a sprint to show its name, ID and state in the right detail pane. Enter has no sprint-to-issues branch.

- [Enter navigation guard](https://github.com/dutch-casa/youtrack/blob/0729f437ceb054e982ef91b8dd00b291a90e5dee/internal/tui/tui.go#L396-L402).
- [Board selection and sprint fetch](https://github.com/dutch-casa/youtrack/blob/0729f437ceb054e982ef91b8dd00b291a90e5dee/internal/tui/tui.go#L866-L884), [sprint result conversion](https://github.com/dutch-casa/youtrack/blob/0729f437ceb054e982ef91b8dd00b291a90e5dee/internal/tui/tui.go#L925-L929).
- [Sprint metadata renderer](https://github.com/dutch-casa/youtrack/blob/0729f437ceb054e982ef91b8dd00b291a90e5dee/internal/tui/resources.go#L56-L95).

The root renderer always composes one left list and one right detail pane. Its separate Issues section likewise displays one flat issue list. These two panes are not two workflow columns.

- [Root composition and section routing](https://github.com/dutch-casa/youtrack/blob/0729f437ceb054e982ef91b8dd00b291a90e5dee/internal/tui/views.go#L37-L70).
- [Flat issue list renderer](https://github.com/dutch-casa/youtrack/blob/0729f437ceb054e982ef91b8dd00b291a90e5dee/internal/tui/views.go#L73-L98).
- [Board-to-sprint navigation test](https://github.com/dutch-casa/youtrack/blob/0729f437ceb054e982ef91b8dd00b291a90e5dee/internal/tui/tui_test.go#L311-L353), [sprint action hidden test](https://github.com/dutch-casa/youtrack/blob/0729f437ceb054e982ef91b8dd00b291a90e5dee/internal/tui/tui_test.go#L660-L678).
- [Maintainer decision describing metadata browsing](https://github.com/dutch-casa/youtrack/blob/0729f437ceb054e982ef91b8dd00b291a90e5dee/docs/adr/0044-agile-sprint-resource-drilldown.md#L9-L27).

It supports a free-text YouTrack command prompt for issues, rather than the board's picker of available workflow actions. [Prompt implementation](https://github.com/dutch-casa/youtrack/blob/0729f437ceb054e982ef91b8dd00b291a90e5dee/internal/tui/tui.go#L623-L652).

## Implication for this project

Keep this app's board-first direction: simultaneous status columns containing issue cards, left/right navigation, sprint selection, workflow action discovery and local board views. Neither inspected alternative replaces that core experience. Their issue-reading and generic CLI facilities overlap, but Agile API resource support alone does not provide a Kanban UI.

No bundled screenshot assets or README screenshot embeds were found in either inspected tree. The review did not install/run either application, use credentials or test live writes. It covers the default-branch commits above, not unpublished work or forks. This app also does not yet provide swimlanes, manual card ordering or WIP enforcement.
