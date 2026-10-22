#!/usr/bin/env python3
"""Prepare a source release and checksummed macOS Homebrew formula. Requires uv and network access to PyPI."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tomllib
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, text=True, capture_output=True, **kwargs).stdout


def formula(version, source_url, checksum, resources, homepage, revision=None):
    # JSON string literals are also Ruby strings; refuse interpolation in externally provided URLs.
    if any('#{' in value for value in [source_url, homepage or '', *(part for resource in resources for part in resource)]):
        raise ValueError('URLs must not contain Ruby interpolation')
    homepage_line = f'  homepage {json.dumps(homepage)}\n' if homepage else ''
    if revision and not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Git revision must be a full commit hash')
    source_line = (f'  url {json.dumps(source_url)}, tag: {json.dumps("v" + version)}, revision: {json.dumps(revision)}'
                   if revision else f'  url {json.dumps(source_url)}\n  sha256 {json.dumps(checksum)}')
    blocks = '\n'.join(f'''  resource {json.dumps(name)} do
    url {json.dumps(url)}
    sha256 {json.dumps(sha)}
  end
''' for name, url, sha in resources)
    return f'''class Issueboard < Formula
  include Language::Python::Virtualenv

  desc "Keyboard-driven issue board for YouTrack, GitLab and Jira Cloud"
{homepage_line}{source_line}
  version {json.dumps(version)}
  license "MIT"

  depends_on :macos
  depends_on "python@3.14"

{blocks}
  def install
    venv = virtualenv_create(libexec, "python3.14")
    venv.pip_install resources
    venv.pip_install buildpath
    bin.install_symlink libexec/"bin/issueboard"
  end

  def caveats
    <<~EOS
      Run issueboard in a project folder: it asks once whether the folder uses YouTrack, GitLab or Jira.
      Try it with issueboard --demo youtrack, issueboard --demo gitlab or issueboard --demo jira.
      YouTrack and Jira tokens stay in the OS keychain; GitLab needs glab (brew install glab) and its own sign-in.
      Use issueboard youtrack, issueboard gitlab or issueboard jira for tracker-specific options.
    EOS
  end

  test do
    assert_match "--setup", shell_output("#{{bin}}/issueboard --help")
    assert_match "--setup", shell_output("#{{bin}}/issueboard youtrack --help")
    assert_match "--demo", shell_output("#{{bin}}/issueboard gitlab --help")
    assert_match "--setup", shell_output("#{{bin}}/issueboard jira --help")
    system libexec/"bin/python", "-c", <<~PYTHON
      import asyncio
      from youtrack_board.app import BoardApp
      from youtrack_board.demo import DemoClient
      from gitlab_board.app import GitLabApp, Prefs
      from gitlab_board.demo import DemoClient as GitLabDemo, demo_state
      from gitlab_board.target import parse
      from jira_board.app import JiraApp
      from jira_board.demo import DemoClient as JiraDemo, demo_state as jira_state
      from issueboard.prefs import Prefs as JiraPrefs
      from textual.worker import WorkerCancelled
      async def verify(app):
          async with app.run_test(size=(80, 24)) as pilot:
              for _ in range(8):
                  await pilot.pause()
                  try:
                      await app.workers.wait_for_complete()
                  except WorkerCancelled:
                      pass
              assert app.board_data, "Demo board did not load"
              assert app.issues_data, "Demo issues did not load"
      asyncio.run(verify(BoardApp(DemoClient())))
      client, state = GitLabDemo(), demo_state()
      asyncio.run(verify(GitLabApp(client, Prefs(None, client.host, state), start=parse(state["project"], client.host))))
      client = JiraDemo()
      asyncio.run(verify(JiraApp(client, JiraPrefs(None, client.base_url, jira_state()))))
    PYTHON
  end
end
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/homebrew', help='directory for the generated tap')
    parser.add_argument('--archive', type=Path, help='reuse the exact published source archive instead of rebuilding')
    parser.add_argument('--url', help='HTTPS URL where this exact source archive will be published; default: local archive')
    parser.add_argument('--homepage', help='HTTPS project homepage for a published formula')
    parser.add_argument('--git-source', action='store_true', help='use --url as an HTTPS Git source pinned to the current commit')
    args = parser.parse_args()
    if args.url and (urlsplit(args.url).scheme != 'https' or not urlsplit(args.url).hostname or urlsplit(args.url).username or urlsplit(args.url).password):
        parser.error('--url must be an HTTPS release URL without credentials')
    if args.homepage and (urlsplit(args.homepage).scheme != 'https' or not urlsplit(args.homepage).hostname):
        parser.error('--homepage must be HTTPS')
    if args.git_source and (not args.url or not args.url.endswith('.git')):
        parser.error('--git-source requires --url pointing to an HTTPS .git repository')
    revision = run('git', 'rev-parse', 'HEAD').strip() if args.git_source else None
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    metadata = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    # Export the lock first: every dependency remains pinned when targeting Homebrew's macOS Python.
    locked = run('uv', 'export', '--locked', '--no-dev', '--format', 'requirements-txt', '--no-hashes', '--no-emit-project')
    pinned = run('uv', 'pip', 'compile', '-', '--python-platform', 'aarch64-apple-darwin', '--python-version', '3.14',
                 '--no-header', '--no-annotate', input=locked)
    resources = []
    for line in pinned.splitlines():
        match = re.fullmatch(r'([\w.-]+)==([\w.+-]+)', line)
        if not match:
            raise ValueError(f'Expected an exact dependency pin, got {line!r}')
        name, version = match.groups()
        with urlopen(f'https://pypi.org/pypi/{name}/{version}/json', timeout=30) as response:
            release = json.load(response)
        source = next(file for file in release['urls'] if file['packagetype'] == 'sdist')
        resources.append((name, source['url'], source['digests']['sha256']))
    archive = output / 'releases' / f'issueboard-{metadata["version"]}.tar.gz'
    if args.archive:
        source = args.archive.resolve()
        if not source.is_file() or source.name != archive.name:
            parser.error(f'--archive must be an existing {archive.name}')
        archive.parent.mkdir(parents=True, exist_ok=True)
        if source != archive:
            shutil.copy2(source, archive)
    else:
        run('uv', 'build', '--sdist', '--out-dir', str(output / 'releases'))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    target = output / 'Formula/issueboard.rb'
    target.parent.mkdir(exist_ok=True)
    target.write_text(formula(metadata['version'], args.url or archive.as_uri(), checksum, resources,
                              args.homepage or metadata.get("urls", {}).get("Homepage"), revision))
    print(f'Archive: {archive}\nSHA256: {checksum}\nFormula: {target}')
    print('For local installation, see docs/homebrew.md. Prepared artifacts only; this command does not publish.')


if __name__ == '__main__':
    main()
