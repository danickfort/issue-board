# App screenshots and walkthroughs

The README media is captured from the real Textual app, running its offline YouTrack demo in **xterm.js** and **fish**. The demo changes only in-memory data. The terminal is a local development tool; it opens an interactive shell with your account's permissions.

## Start the saved terminal

Prerequisites: Node.js/npm, Python 3.12+ and uv. From the repository root:

```sh
uv sync
.venv/bin/python tools/capture/setup.py
npm start --prefix tools/capture
```

Open <http://127.0.0.1:4178>. The setup installs npm packages from the lockfile and a separate media export environment. On Linux x86_64/aarch64, it installs the official fish 4.9.3 static binary under `tools/capture/.local/bin`, verifying the archive's SHA-256. If fish is already installed, it uses a local symlink. On macOS install fish first (`brew install fish`). `CAPTURE_SHELL=/absolute/path/to/fish` overrides the shell. The PTY bridge uses Python's Unix APIs; Windows needs WSL.

The committed profile, JetBrains Mono font and DejaVu Sans Mono symbol fallback (with their licenses), terminal theme and window styling live in `tools/capture/`. The session clears `NO_COLOR`, sets truecolor, adds this checkout's `.venv/bin` to PATH and disables fish history. It does not load your personal fish config or change your login shell. Use `CAPTURE_PORT=4180` to select another port. Stop the server with Ctrl+C when finished; it listens only on loopback and checks HTTP Host and WebSocket Origin.

## Record one complete walkthrough

Use a **1440 × 960** browser viewport at 100% zoom. Reload the page for a fresh shell and demo. Click **Run walkthrough** once. It drives the actual app through the PTY, with captions and pauses for:

1. Launching the board and navigating stories.
2. Moving DEMO-1 from Open to In Progress.
3. Opening DEMO-3's details and comments.
4. Posting a comment and reopening the updated thread.
5. Opening merge requests and inspecting pipeline, review and discussion status.
6. Opening the backlog and returning to the board.

The tour uses only `issueboard --demo youtrack`; do not substitute a live board when reproducing documentation media. A stopped tour names the screen it expected. Reload before retrying. The demo's MR panel is offline; opening a real GitLab web page is outside this recording.

In T3 Code, use its collaborative preview tools:

- `preview_status`, then `preview_open` if necessary; navigate to the local URL and resize to 1440 × 960.
- Wait for `capture.waitFor('issue-board ›')` in `preview_evaluate`.
- Start `preview_recording_start` before launching the tour.
- Start the tour without holding an evaluate call open: `capture.walkthrough().catch(console.error); 'started'`.
- Check `{step:capture.step, done:capture.done, error:capture.error}` periodically. `done: true` means every screen assertion passed; any `error` means the recording should be discarded.
- After `done: true`, take a `preview_snapshot` to flush the rendered final board into the recording, then stop with `preview_recording_stop`. Its returned path is the recording source.
- Use `preview_snapshot({save:true})` for PNG screenshots and copy its returned path to `docs/media/`. Inspect the image before using it.

For another browser, record the same page with a browser/window recorder. Captions are part of the page. The tour hides its start button in captures. `capture.send(text)`, `capture.chapter(title, hint)` and `capture.screen()` support manual screenshots; changes to the app's key bindings or demo data may require updating `client.js`.

## Export and inspect

```sh
uv run --project tools/capture/media --frozen python tools/capture/media/export.py \
  /absolute/path/to/recording.webm docs/media/walkthrough.mp4
```

The export uses H.264, YUV 4:2:0 and MP4 faststart for broad playback compatibility. It deliberately has no audio. Check the start, story move, saved comment, both MR selections, backlog and final board; captions and text should remain readable. Keep generated files small enough for the repository. Commit only the chosen PNG/MP4 assets, source tooling and lockfiles; downloaded binaries, environments and intermediate recordings are ignored.

[xterm.js documentation](https://xtermjs.org/docs/) · [fish documentation](https://fishshell.com/docs/current/) · [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono)
