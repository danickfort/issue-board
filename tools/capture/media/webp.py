"""Export a looping animated WebP preview for a GitHub README."""
import argparse
from pathlib import Path
import subprocess
import imageio_ffmpeg

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('source', type=Path)
parser.add_argument('destination', type=Path)
args = parser.parse_args()
if not args.source.is_file():
    parser.error(f'Video not found: {args.source}')
args.destination.parent.mkdir(parents=True, exist_ok=True)
subprocess.run([
    imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'warning',
    '-y', '-i', str(args.source), '-an', '-vf', r'fps=10,scale=w=min(1200\,iw):h=-2',
    '-c:v', 'libwebp_anim', '-lossless', '1', '-compression_level', '4',
    '-loop', '0', str(args.destination),
], check=True)
print(f'Saved {args.destination} ({args.destination.stat().st_size / 1024 / 1024:.1f} MiB)')
