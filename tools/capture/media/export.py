"""Convert a browser recording into a GitHub-compatible H.264 MP4."""
import argparse
from pathlib import Path
import subprocess
import imageio_ffmpeg

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('source', type=Path)
parser.add_argument('destination', type=Path)
args = parser.parse_args()
if not args.source.is_file():
    parser.error(f'Recording not found: {args.source}')
args.destination.parent.mkdir(parents=True, exist_ok=True)
subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-y', '-i', str(args.source),
                '-an', '-vf', r'fps=30,scale=w=min(1440\,iw):h=-2',
                '-c:v', 'libx264', '-preset', 'medium', '-crf', '20', '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart', str(args.destination)], check=True)
print(f'Saved {args.destination} ({args.destination.stat().st_size / 1024 / 1024:.1f} MiB)')
