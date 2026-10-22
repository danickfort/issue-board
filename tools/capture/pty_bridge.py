"""Small Unix PTY bridge: JSON lines in, unmodified terminal bytes out."""
import errno
import fcntl
import json
import os
import pty
import select
import signal
import struct
import sys
import termios

pid, master = pty.fork()
if pid == 0:
    os.execv(sys.argv[1], [sys.argv[1], '--no-config', '--interactive', '--init-command',
                         f"source '{sys.argv[2]}'"])


def stop(*_):
    try:
        os.killpg(pid, signal.SIGHUP)
    except ProcessLookupError:
        pass
    os.close(master)
    os.waitpid(pid, 0)


def interrupted(*_):
    raise SystemExit(0)


signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)
fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 38, 140, 0, 0))
pending = b''
try:
    while True:
        readable, _, _ = select.select([master, sys.stdin.fileno()], [], [])
        if master in readable:
            try:
                chunk = os.read(master, 65536)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
        if sys.stdin.fileno() in readable:
            chunk = os.read(sys.stdin.fileno(), 65536)
            if not chunk:
                break
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                message = json.loads(line)
                if message['type'] == 'input':
                    data = message['data'].encode()
                    while data:
                        data = data[os.write(master, data):]
                elif message['type'] == 'resize':
                    cols = max(20, min(300, message['cols']))
                    rows = max(10, min(100, message['rows']))
                    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
finally:
    stop()
