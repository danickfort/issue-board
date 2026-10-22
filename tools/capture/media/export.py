"""Convert a browser recording into a GitHub-compatible H.264 MP4."""
import argparse
from pathlib import Path
import subprocess
import imageio_ffmpeg

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('source', type=Path)
parser.add_argument('destination', type=Path)
parser.add_argument('--end-frame', type=Path, help='Append two seconds of the final captured screen')
args = parser.parse_args()
if not args.source.is_file():
    parser.error(f'Recording not found: {args.source}')
args.destination.parent.mkdir(parents=True, exist_ok=True)
command = [imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-y', '-i', str(args.source)]
video_filter = r'fps=30,scale=w=min(1440\,iw):h=-2,setsar=1,format=yuv420p'
if args.end_frame:
    if not args.end_frame.is_file():
        parser.error(f'Final screen not found: {args.end_frame}')
    reader = imageio_ffmpeg.read_frames(str(args.source))
    source_width, source_height = next(reader)['size']
    reader.close()
    width = min(1440, source_width)
    height = round(source_height * width / source_width / 2) * 2
    end_filter = (f'fps=30,scale={width}:{height}:force_original_aspect_ratio=decrease,'
                  f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p')
    command += ['-loop', '1', '-t', '2', '-i', str(args.end_frame), '-filter_complex',
                f'[0:v]{video_filter}[tour];[1:v]{end_filter}[end];'
                '[tour][end]concat=n=2:v=1:a=0[out]', '-map', '[out]']
else:
    command += ['-vf', video_filter]
subprocess.run(command + ['-an',
                '-c:v', 'libx264', '-preset', 'medium', '-crf', '20', '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart', str(args.destination)], check=True)
print(f'Saved {args.destination} ({args.destination.stat().st_size / 1024 / 1024:.1f} MiB)')
