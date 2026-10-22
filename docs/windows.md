# Windows and WinGet

Windows distribution is being prepared. The release workflow builds and tests a native **Windows x64 portable ZIP** and generates WinGet manifests. Existing releases do not yet include this ZIP, and **Issue Board is not yet published in the WinGet community repository**. Native Windows CI, a published ZIP and Microsoft's manifest review must succeed before advertising a WinGet install as available.

## What installation will look like

After the first manifest is accepted into `microsoft/winget-pkgs`:

```powershell
winget install --id DanickFort.IssueBoard --exact --source winget
issueboard
issueboard --demo youtrack
```

WinGet extracts the application and its bundled Python/dependencies, then exposes `issueboard` as a command. The package installs for the current user; users do not need Python or uv. If the command is not found immediately after installing, open a new terminal. Use Windows Terminal or PowerShell on Windows 10 1809 or newer, x64.

```powershell
winget upgrade --id DanickFort.IssueBoard --exact
winget uninstall --id DanickFort.IssueBoard --exact
```

GitLab boards and merge-request integration additionally need the official GitLab CLI:

```powershell
winget install --id GLab.GLab --exact
glab auth login --hostname gitlab.com
```

Use your own GitLab host. GitLab credentials stay in glab. YouTrack and Jira tokens use Windows Credential Manager. Configuration and view state are kept under the user's home directory (the existing `.config` / `.local/state` paths, or the configured XDG overrides), outside the portable package. Uninstalling the package preserves them; use the tracker subcommands' `--logout` first to remove saved tokens.

Once a release contains `issueboard-<version>-windows-x86_64.zip`, users can also extract the ZIP and run `issueboard.exe` directly. Keep its `_internal` directory beside the executable. Never copy just the EXE.

## Build and check on Windows

With Git, Python 3.12+ and uv installed, from the checkout:

```powershell
uv sync --locked --group release
uv run pytest -q
uv run python scripts/build_release.py
```

The builder is native: Windows builds must run on Windows, not Linux. It freezes a console application, checks `issueboard --help` and each tracker subcommand, archives the entire bundle, extracts the ZIP again, and repeats those checks against the extracted distribution. The release workflow runs the full test suite on both Linux and Windows before publishing a tagged release. A branch/manual run only uploads CI artifacts.

Before the first Windows release, also run all three interactive demos in Windows Terminal and verify keyboard navigation, story moves, comments and the offline MR panel. Test a real account's sign-in/save/logout with Windows Credential Manager. This Linux development environment cannot perform those native Windows checks.

## Generate manifests from the release ZIP

```powershell
# Use the version of the ZIP you are preparing to publish.
$version = '0.3.4'
uv run python scripts/prepare_winget.py "dist/release/issueboard-$version-windows-x86_64.zip" --version $version
```

This produces three YAML files under `dist/winget/manifests/d/DanickFort/IssueBoard/<version>/`: the version, installer and English metadata manifests. It verifies the archive's layout and calculates its actual SHA-256. It does not invent a checksum or submit anything. The installer uses `zip` with a nested `portable` executable and the command alias `issueboard`; all bundled dependencies remain in the extracted package. glab is optional, so it is not a mandatory package dependency.

The Windows CI build also uploads a **winget-manifests** artifact. Generated files point at the versioned, public GitHub release ZIP; do not submit manifests for a branch build or an unpublished release. Never replace an already-published ZIP without a new version and checksum.

## First submission and updates

1. Complete the native Windows checks and publish a new version with the Windows ZIP using the [release workflow](releases.md). The previously published versions cannot acquire Windows assets through that workflow, which refuses to overwrite releases.
2. Download the published ZIP, regenerate the manifests from it and verify that their installer URL downloads the same bytes.
3. On Windows, run `winget validate <manifest-directory>`. In an elevated terminal, enable local manifest testing with `winget settings --enable LocalManifestFiles`, then install using `winget install --manifest <manifest-directory> --scope user`. Test `issueboard --demo youtrack`, the other demos and uninstall. Local manifest installation needs the ZIP already available at its public release URL.
4. Copy the generated `manifests/d/DanickFort/IssueBoard/<version>` folder into a fork of `microsoft/winget-pkgs` and submit a PR. Microsoft's automated validation and review determine acceptance. The package identifier is proposed until that first submission is accepted.
5. For each future release, generate a new version folder using that release's ZIP/checksum and submit another manifest update. Publishing a GitHub release alone does not update the WinGet catalog.

No Store registration, MSI wrapper or release token is required for this portable package. The existing release workflow only publishes GitHub assets; it does not submit to Microsoft's repository.

[Microsoft: submitting packages](https://learn.microsoft.com/windows/package-manager/package/) · [Manifest reference](https://learn.microsoft.com/windows/package-manager/package/manifest) · [Validation and repository review](https://learn.microsoft.com/windows/package-manager/package/repository)
