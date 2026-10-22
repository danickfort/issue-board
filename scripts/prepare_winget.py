#!/usr/bin/env python3
"""Generate WinGet manifests from a built Windows ZIP, using its actual SHA-256."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'DanickFort.IssueBoard'
REPOSITORY = 'https://github.com/danickfort/issue-board'
SCHEMA = '1.6.0'


def manifests(archive, version):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('version must be MAJOR.MINOR.PATCH')
    archive = Path(archive)
    bundle = f'issueboard-{version}-windows-x86_64'
    if archive.name != bundle + '.zip':
        raise ValueError(f'expected {bundle}.zip')
    with zipfile.ZipFile(archive) as zipped:
        names = zipped.namelist()
        if any(PurePosixPath(n).is_absolute() or '..' in PurePosixPath(n).parts or '\\' in n for n in names):
            raise ValueError('archive contains unsafe paths')
        if f'{bundle}/issueboard.exe' not in names or not any(n.startswith(f'{bundle}/_internal/') for n in names):
            raise ValueError('archive must contain issueboard.exe and its _internal dependencies')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest().upper()
    # JSON strings are also YAML scalars. Quote metadata to keep ':' and other punctuation literal.
    quote = json.dumps
    common = f'PackageIdentifier: {PACKAGE}\nPackageVersion: {quote(version)}\n'
    installer = common + f'''InstallerType: zip
NestedInstallerType: portable
NestedInstallerFiles:
- RelativeFilePath: {bundle}\\issueboard.exe
  PortableCommandAlias: issueboard
Scope: user
MinimumOSVersion: 10.0.17763.0
UpgradeBehavior: uninstallPrevious
Commands:
- issueboard
Installers:
- Architecture: x64
  InstallerUrl: {REPOSITORY}/releases/download/v{version}/{archive.name}
  InstallerSha256: {digest}
'''
    locale = common + f'''PackageLocale: en-US
Publisher: Danick Fort
PublisherUrl: {REPOSITORY}
PackageName: Issue Board
PackageUrl: {REPOSITORY}
License: MIT
LicenseUrl: {REPOSITORY}/blob/v{version}/LICENSE
ShortDescription: A keyboard-driven terminal issue board for YouTrack, GitLab and Jira Cloud
Moniker: issueboard
Tags:
- cli
- gitlab
- issues
- jira
- kanban
- terminal
- tui
- youtrack
ReleaseNotesUrl: {REPOSITORY}/releases/tag/v{version}
InstallNotes: {quote('Run issueboard in your project folder. GitLab integration also needs glab: winget install --id GLab.GLab --exact. Tokens use Windows Credential Manager.')}
'''
    version_manifest = common + 'DefaultLocale: en-US\n'
    result = {}
    for suffix, kind, body in [('installer', 'installer', installer), ('locale.en-US', 'defaultLocale', locale),
                                ('', 'version', version_manifest)]:
        filename = PACKAGE + ('.' + suffix if suffix else '') + '.yaml'
        result[filename] = (f'# yaml-language-server: $schema=https://aka.ms/winget-manifest.{kind}.{SCHEMA}.schema.json\n'
                            + body + f'ManifestType: {kind}\nManifestVersion: {SCHEMA}\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--version', default=tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version'])
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/winget')
    args = parser.parse_args()
    try:
        content = manifests(args.archive, args.version)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        parser.error(str(error))
    folder = args.output / 'manifests/d/DanickFort/IssueBoard' / args.version
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in content.items():
        (folder / name).write_text(text, encoding='utf-8')
    print(f'WinGet manifests: {folder}')
    print('Validate and test on Windows before submitting to microsoft/winget-pkgs. Nothing was published.')


if __name__ == '__main__':
    main()
