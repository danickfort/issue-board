#!/usr/bin/env python3
"""Build a source release and checksummed macOS Homebrew tap. Requires uv and network access to PyPI."""
import argparse
import hashlib
import json
from pathlib import Path
import re
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
    return f'''class YoutrackBoard < Formula
  include Language::Python::Virtualenv

  desc "Keyboard-driven YouTrack Kanban board"
{homepage_line}{source_line}
  version {json.dumps(version)}
  license "MIT"

  depends_on :macos
  depends_on "python@3.14"

{blocks}
  def install
    virtualenv_install_with_resources
  end

  def caveats
    <<~EOS
      Run youtrack-board to connect, or youtrack-board --demo to explore.
      Tokens are stored in the OS keychain; existing YouTrack MCP credentials can be reused.
      Run youtrack-board --setup --open to create or replace your connection.
    EOS
  end

  test do
    assert_match "--setup", shell_output("#{{bin}}/youtrack-board --help")
    system libexec/"bin/python", "-c", <<~PYTHON
      import asyncio
      from youtrack_board.app import BoardApp
      from youtrack_board.demo import DemoClient
      from textual.worker import WorkerCancelled
      async def verify():
          app = BoardApp(DemoClient())
          async with app.run_test(size=(80, 24)) as pilot:
              for _ in range(8):
                  await pilot.pause()
                  try:
                      await app.workers.wait_for_complete()
                  except WorkerCancelled:
                      pass
              assert app.board_data, "Demo board did not load"
              assert app.issues_data, "Demo issues did not load"
      asyncio.run(verify())
    PYTHON
  end
end
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/homebrew', help='directory for the generated tap')
    parser.add_argument('--url', help='HTTPS URL where this exact source archive will be published; default: local archive')
    parser.add_argument('--homepage', help='HTTPS project homepage for a published formula')
    parser.add_argument('--git-source', action='store_true', help='use --url as a private HTTPS Git source pinned to the current commit')
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
    run('uv', 'build', '--sdist', '--out-dir', str(output / 'releases'))
    archive = output / 'releases' / f'youtrack_board-{metadata["version"]}.tar.gz'
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    target = output / 'Formula/youtrack-board.rb'
    target.parent.mkdir(exist_ok=True)
    target.write_text(formula(metadata['version'], args.url or archive.as_uri(), checksum, resources,
                              args.homepage or metadata.get("urls", {}).get("Homepage"), revision))
    print(f'Archive: {archive}\nSHA256: {checksum}\nFormula: {target}')
    print('For local installation, see docs/homebrew.md. Prepared artifacts only; this command does not publish.')


if __name__ == '__main__':
    main()
