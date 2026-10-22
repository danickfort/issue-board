#!/usr/bin/env python3
"""Build a native standalone Linux archive."""
import argparse
import hashlib
import platform
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    subprocess.run(args, cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', help='require v<project version> before building')
    args = parser.parse_args()
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        parser.error('release version must be MAJOR.MINOR.PATCH')
    if args.tag and args.tag != f'v{version}':
        parser.error(f'tag {args.tag!r} does not match v{version}')
    system = platform.system()
    if system != 'Linux':
        parser.error('build on Linux; use Homebrew for macOS')
    arch = {'aarch64': 'arm64', 'AMD64': 'x86_64'}.get(platform.machine(), platform.machine())
    name = f'issueboard-{version}-linux-{arch}'
    work = ROOT / 'build/release'
    output = ROOT / 'dist/release'
    if not all(shutil.which(tool) for tool in ('objdump', 'objcopy')):
        parser.error('Linux builds require binutils (objdump and objcopy); install it with your package manager')
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    output.mkdir(parents=True, exist_ok=True)
    run(sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
        '--console', '--name', 'issueboard', '--paths', str(ROOT),
        '--distpath', str(work / 'dist'), '--workpath', str(work / 'pyinstaller'),
        '--specpath', str(work), '--collect-all', 'textual', '--collect-submodules', 'keyring.backends',
        '--recursive-copy-metadata', 'keyring', str(ROOT / 'scripts/frozen_entry.py'))
    bundle = work / 'dist/issueboard'
    shutil.copy2(ROOT / 'LICENSE', bundle / 'LICENSE')
    # Check the frozen command and each tracker subcommand.
    for args in ([], ['youtrack'], ['gitlab'], ['jira']):
        run(str(bundle / 'issueboard'), *args, '--help')
    archive = output / f'{name}.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(bundle, arcname=name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name + '.sha256').write_text(f'{digest}  {archive.name}\n')


if __name__ == '__main__':
    main()
