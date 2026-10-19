# Homebrew distribution

The app and tap are private GitHub repositories:

- Source: https://github.com/krabilousse/youtrack-board
- Tap: https://github.com/krabilousse/homebrew-youtrack
- Releases: https://github.com/krabilousse/youtrack-board/releases

## Install

Your GitHub account needs read access to both repositories. Configure GitHub's HTTPS credential helper, then supply Homebrew's dedicated token environment variable during downloads:

```sh
brew install gh
gh auth login
gh auth setup-git
HOMEBREW_GITHUB_API_TOKEN="$(gh auth token)" brew install krabilousse/youtrack/youtrack-board
brew test krabilousse/youtrack/youtrack-board
```

Skip login when already signed in. The formula uses Homebrew's built-in authenticated Git download strategy, with both release tag and full commit revision pinned. Runtime Python dependencies use immutable source URLs and SHA-256 checksums. No GitHub or YouTrack token is stored in a formula, source URL or repository.

On recent Homebrew, sandboxed Git downloads receive `HOMEBREW_GITHUB_API_TOKEN` as `GH_TOKEN` through the configured gh helper; an arbitrary token environment variable is not enough. Homebrew's [private download guidance](https://docs.brew.sh/FAQ#how-do-i-authenticate-private-git-source-downloads) describes this behavior. Use an up-to-date Homebrew. The tap itself is cloned using your Git credentials.

The formula links only `youtrack-board`; the package's `gitlab-board` command, for source checkouts, is left to the [GitLab Board](https://github.com/krabilousse/gitlab-board) formula, so both can be installed side by side. 0.1.3 linked both, which conflicts with GitLab Board: Homebrew then leaves `youtrack-board` unlinked; upgrade to 0.1.4 or later. The app uses an isolated Python 3.14 environment on macOS. It needs no uv or checkout at runtime. The formula test runs the installed demo headlessly and verifies an actual board and its issues without credentials, config writes or network requests.

## Update and uninstall

```sh
brew update
HOMEBREW_GITHUB_API_TOKEN="$(gh auth token)" brew upgrade krabilousse/youtrack/youtrack-board
brew uninstall youtrack-board
```

Preferences and saved credentials survive uninstall. To remove the app's own keychain entry and URL first, run `youtrack-board --logout`. It never removes credentials from Claude/Codex MCP configs.

## Publish a release

1. Update the version in `pyproject.toml`, update docs, run `uv sync` and tests, and commit the final source.
2. Generate the private-source formula and source distribution:

   ```sh
   uv run python scripts/prepare_homebrew.py --git-source --url https://github.com/krabilousse/youtrack-board.git --homepage https://github.com/krabilousse/youtrack-board
   ```

3. Tag that exact commit `v<version>` and push the commit and tag. Upload the exact generated `dist/homebrew/releases/youtrack_board-<version>.tar.gz` plus its SHA-256 checksum to the matching GitHub release. Do not replace an existing release silently.
4. Copy the generated `Formula/youtrack-board.rb` into the private tap repository and commit/push it. Its Git revision must match the release tag. The Git source is used instead of a private HTTP asset to reuse Homebrew's maintained authentication without a custom downloader; the checksummed archive remains available on the release.
5. Fetch/install from the published tap, then run `brew test krabilousse/youtrack/youtrack-board`. Check the actual installed behavior, not only the generated formula.

The package archive includes only app files, metadata, README and MIT license. Local credentials, generated artifacts and caches are excluded. The MIT license is included in both repositories; private visibility controls who can access the source.

## Local development installation

`uv run python scripts/prepare_homebrew.py` generates a local-file formula for development. Use uv's Python: a separate python.org macOS installation may lack its CA bundle until its Install Certificates command is run.

If using a development tap, create it once with `brew tap-new local/youtrack-board`, copy the generated formula into its Formula directory, and install it. After source edits, regenerate/copy the formula and use `brew reinstall --build-from-source local/youtrack-board/youtrack-board`.

If the local formula is currently installed, uninstall it before installing the published formula with the same name. Neither uninstall nor reinstall deletes preferences or credentials. Do not keep generating/copying a local formula over the published tap: the published formula must point to GitHub, not a machine-local archive.

## Scope

Source installation on macOS is supported. No bottles or Linux formula are provided yet. The implementation follows Homebrew's [Python application guidance](https://docs.brew.sh/Language-Specific-Formulae#python) and [tap documentation](https://docs.brew.sh/How-to-Create-and-Maintain-a-Tap).
