"""Install capture-only dependencies locally; run with the project's Python."""
import hashlib
import io
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import urllib.request

HERE = Path(__file__).resolve().parent
FISH_VERSION = '4.9.3'
DIGESTS = {
    'x86_64': '8f643d10ad1abb0ef7072764c01ceb5cf61b2d373fda400cf60f0bad69b7e095',
    'aarch64': '57349f0111728c64b703a84e611aa97ff4d1fa8466385d64e67bd5bc3ffa49fc',
}
subprocess.run(['npm', 'ci', '--prefix', str(HERE)], check=True)
local_fish = HERE / '.local/bin/fish'
local_fish.parent.mkdir(parents=True, exist_ok=True)
if not local_fish.exists():
    installed = os.environ.get('CAPTURE_SHELL') or shutil.which('fish')
    if installed:
        local_fish.symlink_to(Path(installed).resolve())
    else:
        arch = platform.machine()
        if platform.system() != 'Linux' or arch not in DIGESTS:
            raise SystemExit('Install fish with your package manager, then rerun setup (macOS: brew install fish).')
        url = f'https://github.com/fish-shell/fish-shell/releases/download/{FISH_VERSION}/fish-{FISH_VERSION}-linux-{arch}.tar.xz'
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != DIGESTS[arch]:
            raise SystemExit('Fish archive checksum mismatch; nothing installed.')
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            local_fish.write_bytes(archive.extractfile('fish').read())
        local_fish.chmod(0o755)
subprocess.run([str(local_fish), '--version'], check=True)
subprocess.run(['uv', 'sync', '--project', str(HERE / 'media'), '--frozen'], check=True)
print('Ready: npm start --prefix tools/capture')
