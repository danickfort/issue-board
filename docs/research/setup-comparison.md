# Guided setup comparison

Checked 2026-10-16. Source inspected without installing applications, executing repository code or reading credentials. Repository commits match the earlier Kanban comparison.

## Useful existing approaches

**omartelo/youtrack-tui** (`fb5457961c2f110952ed35387bdf3b3ba1adba9a`) offers a first-run form with provider name, instance URL, permanent token and optional CA file. It explains where to create a token, masks token entry and supports an explicit reveal toggle. Submission validates the form, then loads saved filters before saving; this tests more permissions than authentication alone. Storage is YAML with restrictive permissions, supporting environment references, rather than an OS keychain.

- [Form hints and fields](https://github.com/omartelo/youtrack-tui/blob/fb5457961c2f110952ed35387bdf3b3ba1adba9a/internal/ui/setup.go#L18-L39).
- [Submission and masked input](https://github.com/omartelo/youtrack-tui/blob/fb5457961c2f110952ed35387bdf3b3ba1adba9a/internal/ui/setup.go#L67-L114).
- [Config persistence](https://github.com/omartelo/youtrack-tui/blob/fb5457961c2f110952ed35387bdf3b3ba1adba9a/internal/config/config.go#L220-L260).

**dutch-casa/youtrack** (`0729f437ceb054e982ef91b8dd00b291a90e5dee`) has the closest pattern to reuse conceptually: `auth login --open` asks for the URL, optionally opens the instance-specific security page, takes a hidden token and verifies the current user before saving. Its browser URL preserves installation subpaths and appends `/users/me?tab=account-security`. It stores credentials in JSON with 0600 file permissions and provides `auth status`, which reports configuration rather than verifying it live.

- [Guided login and verification](https://github.com/dutch-casa/youtrack/blob/0729f437ceb054e982ef91b8dd00b291a90e5dee/internal/ytcli/root.go#L110-L173).
- [Contextual security URL](https://github.com/dutch-casa/youtrack/blob/0729f437ceb054e982ef91b8dd00b291a90e5dee/internal/ytcli/root.go#L1246-L1273).
- [Storage](https://github.com/dutch-casa/youtrack/blob/0729f437ceb054e982ef91b8dd00b291a90e5dee/internal/auth/auth.go#L73-L101), [hidden terminal input](https://github.com/dutch-casa/youtrack/blob/0729f437ceb054e982ef91b8dd00b291a90e5dee/internal/auth/auth.go#L196-L207), [status](https://github.com/dutch-casa/youtrack/blob/0729f437ceb054e982ef91b8dd00b291a90e5dee/internal/ytcli/root.go#L186-L201).

**GitHub CLI** documents interactive browser authentication, environment credentials, token input from stdin, and storage diagnostics. It prefers system credential storage but falls back to plaintext if that fails. Borrow its guided interaction and status visibility, but do not copy that fallback. [Official `gh auth login` manual](https://cli.github.com/manual/gh_auth_login).

## Smallest improvement to our current wizard

1. Reuse existing supported credential discovery first. Show the selected instance and source without revealing any token; do not prompt again when complete credentials exist. When multiple instances are discovered, require an explicit instance choice rather than silently mixing a URL and token from unrelated sources.
2. Ask for the instance URL first when missing. Preserve a legitimate installation subpath. Show its `/users/me?tab=account-security` link, explain **Profile → Account Security → Tokens → New token**, and request the **YouTrack** scope.
3. Offer to open that page in the browser; always leave a copyable link and continue if browser opening fails. This helps create a permanent token; it is not OAuth login.
4. Accept the token with masked input. Never display it in success messages or instruct the user to paste it into shell command arguments.
5. Verify with a read-only `GET /api/users/me` and show the account login before saving. Keep connection/TLS failures separate from rejected credentials; do not offer to disable certificate verification.
6. Save the token in the existing OS keychain integration and URL in local config. If secure storage fails, explain it and allow environment/session credentials; never silently persist plaintext.
7. Provide a concise auth status diagnostic: URL, credential source, storage availability and current account/check result. No token fragments are needed.

Permanent tokens are an officially supported YouTrack integration method. This review did not establish a generally available device authorization grant for personal CLI clients, so it recommends no OAuth implementation. [JetBrains permanent-token authentication documentation](https://www.jetbrains.com/help/youtrack/devportal/authentication-with-permanent-token.html).

## Distribution

omartelo documents Homebrew installation via `brew install --cask omartelo/tap/youtrack-tui`, alongside an installer script and AUR. [Pinned installation documentation](https://github.com/omartelo/youtrack-tui/blob/fb5457961c2f110952ed35387bdf3b3ba1adba9a/README.md#L15-L35).

dutch-casa documents its Go-backed installer script and `yt upgrade`; no Homebrew distribution was found in the inspected repository. [Pinned installation documentation](https://github.com/dutch-casa/youtrack/blob/0729f437ceb054e982ef91b8dd00b291a90e5dee/README.md#L5-L33).

Not checked: live browser routes across YouTrack versions, keychain behavior on other operating systems, or external packaging repositories.
