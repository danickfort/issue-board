"""Release archive layout, checksum integrity and WinGet metadata."""
import hashlib
import importlib.util
from pathlib import Path
import sys
import tarfile
import zipfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('system,extension', [('Linux', '.tar.gz'), ('Windows', '.zip')])
def test_native_release_keeps_dependencies_and_checks_packaged_commands(tmp_path, monkeypatch, system, extension):
    build = script('build_release')
    monkeypatch.setattr(build, 'ROOT', tmp_path)
    monkeypatch.setattr(build.platform, 'system', lambda: system)
    monkeypatch.setattr(build.platform, 'machine', lambda: 'AMD64' if system == 'Windows' else 'x86_64')
    monkeypatch.setattr(build.shutil, 'which', lambda _: '/test/tool')
    monkeypatch.setattr(sys, 'argv', ['build_release.py', '--tag', 'v1.2.3'])
    (tmp_path / 'pyproject.toml').write_text('[project]\nversion = "1.2.3"\n')
    (tmp_path / 'LICENSE').write_bytes(b'MIT\n')
    calls = []
    exe = 'issueboard.exe' if system == 'Windows' else 'issueboard'

    def run(*args):
        calls.append(args)
        if args[1:3] == ('-m', 'PyInstaller'):
            bundle = tmp_path / 'build/release/dist/issueboard'
            (bundle / '_internal').mkdir(parents=True)
            (bundle / exe).write_bytes(b'executable fixture')
            (bundle / '_internal/runtime.bin').write_bytes(b'dependencies')
        else:
            assert Path(args[0]).is_file()
    monkeypatch.setattr(build, 'run', run)
    build.main()
    name = f'issueboard-1.2.3-{system.lower()}-x86_64'
    archive = tmp_path / 'dist/release' / (name + extension)
    digest, filename = archive.with_name(archive.name + '.sha256').read_text().split()
    assert digest == hashlib.sha256(archive.read_bytes()).hexdigest() and filename == archive.name
    if system == 'Windows':
        with zipfile.ZipFile(archive) as zipped:
            assert zipped.read(f'{name}/_internal/runtime.bin') == b'dependencies'
            assert zipped.read(f'{name}/LICENSE') == b'MIT\n'
        # Run all four commands again after ZIP extraction, where missing DLLs would matter.
        commands = [args[1:] for args in calls if 'smoke' in Path(args[0]).parts]
        assert commands == [('--help',), ('youtrack', '--help'), ('gitlab', '--help'), ('jira', '--help')]
    else:
        with tarfile.open(archive) as tar:
            assert tar.extractfile(f'{name}/_internal/runtime.bin').read() == b'dependencies'


def windows_zip(tmp_path, *, version='1.2.3', extra=None, internal=True):
    name = f'issueboard-{version}-windows-x86_64'
    archive = tmp_path / (name + '.zip')
    with zipfile.ZipFile(archive, 'w') as zipped:
        zipped.writestr(f'{name}/issueboard.exe', b'fake EXE: not a Windows smoke test')
        if internal:
            zipped.writestr(f'{name}/_internal/runtime.dll', b'runtime')
        if extra:
            zipped.writestr(extra, b'extra')
    return archive


def test_winget_manifests_match_archive_hash_url_layout_and_command(tmp_path):
    winget = script('prepare_winget')
    archive = windows_zip(tmp_path)
    docs = [yaml.safe_load(text) for text in winget.manifests(archive, '1.2.3').values()]
    by_type = {doc['ManifestType']: doc for doc in docs}
    assert set(by_type) == {'installer', 'defaultLocale', 'version'}
    for doc in docs:
        assert doc['PackageIdentifier'] == 'DanickFort.IssueBoard' and doc['PackageVersion'] == '1.2.3'
    installer = by_type['installer']
    assert installer['InstallerType'] == 'zip' and installer['NestedInstallerType'] == 'portable'
    assert installer['Scope'] == 'user'
    binary = installer['NestedInstallerFiles'][0]
    assert binary['PortableCommandAlias'] == 'issueboard'
    with zipfile.ZipFile(archive) as zipped:
        assert binary['RelativeFilePath'].replace('\\', '/') in zipped.namelist()
    download = installer['Installers'][0]
    assert download['Architecture'] == 'x64'
    assert download['InstallerSha256'] == hashlib.sha256(archive.read_bytes()).hexdigest().upper()
    assert download['InstallerUrl'] == f'{winget.REPOSITORY}/releases/download/v1.2.3/{archive.name}'
    assert by_type['version']['DefaultLocale'] == by_type['defaultLocale']['PackageLocale'] == 'en-US'


@pytest.mark.parametrize('extra,internal', [('../escape', True), (None, False)])
def test_winget_rejects_a_zip_that_is_not_the_distributable(tmp_path, extra, internal):
    winget = script('prepare_winget')
    archive = windows_zip(tmp_path, extra=extra, internal=internal)
    with pytest.raises(ValueError):
        winget.manifests(archive, '1.2.3')
    with pytest.raises(ValueError, match='expected'):
        winget.manifests(archive, '2.0.0')
