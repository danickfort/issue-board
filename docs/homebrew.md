# Homebrew distribution

Issue Board and its Homebrew tap are public:

- Source: https://github.com/danickfort/issue-board
- Tap: https://github.com/danickfort/homebrew-issueboard
- Releases: https://github.com/danickfort/issue-board/releases

## Install on macOS

```sh
brew install danickfort/issueboard/issueboard
brew test danickfort/issueboard/issueboard
```

No GitHub account, `gh` login, credential helper or token environment variable is required. You can also run `brew tap danickfort/issueboard`, then `brew install issueboard`. This is a third-party tap; the formula is not part of Homebrew core.

The formula downloads a public source release with a SHA-256 checksum and pins the Python dependency resources with their own checksums. It installs an isolated Python 3.14 environment and links only `issueboard`. Runtime use needs no uv or checkout.

The older standalone formulas are no longer needed and can be removed with `brew uninstall youtrack-board gitlab-board`. Preferences and credentials are preserved. GitLab boards also need `glab` (`brew install glab`) and its own sign-in.

```sh
brew update
brew upgrade danickfort/issueboard/issueboard
brew uninstall issueboard
```

Existing users of the private tap can update normally without authentication environment variables. Uninstalling preserves preferences, folder choices and credentials. Run `issueboard youtrack --logout` and `issueboard jira --logout` first to remove the apps' saved tokens; MCP configuration is never changed. GitLab sign-ins belong to glab.

The tap's GitHub Actions workflow installs through the public tap on macOS and runs `brew test`, which checks the command and its tracker subcommands and loads the three demo boards headlessly without credentials or a live tracker.

## Update the tap after a release

1. Publish a version tag using the [release workflow](releases.md), and wait for the release assets.
2. Download the published source archive and checksum. Verify with `sha256sum` on Linux or `shasum -a 256` on macOS.
3. Generate a formula using that exact archive, from the matching release checkout:

   ```sh
   uv run python scripts/prepare_homebrew.py \
     --archive /absolute/path/to/issueboard-<version>.tar.gz \
     --url https://github.com/danickfort/issue-board/releases/download/v<version>/issueboard-<version>.tar.gz \
     --homepage https://github.com/danickfort/issue-board
   ```

4. Copy `dist/homebrew/Formula/issueboard.rb` into `Formula/issueboard.rb` in the public tap, commit and push. The app release workflow does not write to the tap; no cross-repository token is needed.
5. Check the tap's installation workflow, then verify with `brew update`, `brew upgrade danickfort/issueboard/issueboard` and `brew test danickfort/issueboard/issueboard`.

Use the exact published archive: rebuilding from a different checkout can produce a different checksum. Do not change an existing release asset silently.

## Local development

`uv run python scripts/prepare_homebrew.py` builds a source archive and generates a local-file formula. Use `brew tap-new local/issueboard` to create a development tap, copy the generated formula into its Formula directory, then install it. After edits, regenerate the formula and use `brew reinstall --build-from-source local/issueboard/issueboard`.

Uninstall a local development formula before installing the public formula with the same name. Avoid overwriting the published tap with a formula that points to a machine-local archive.

The formula supports macOS source installation; no bottles or Linux formula are provided. Linux users should use the [standalone release archive](../README.md#linux-standalone-download). Homebrew documents [tap installation](https://docs.brew.sh/How-to-Create-and-Maintain-a-Tap) and [Python applications](https://docs.brew.sh/Python-for-Formula-Authors).
