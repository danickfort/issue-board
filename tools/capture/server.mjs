import http from 'node:http';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { WebSocketServer } from 'ws';
import { spawn } from 'node:child_process';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../..');
const port = Number(process.env.CAPTURE_PORT || 4178);
const shell = process.env.CAPTURE_SHELL || path.join(here, '.local/bin/fish');
if (!existsSync(shell)) throw new Error('Install fish first (see docs/captures.md), or set CAPTURE_SHELL to its absolute path.');
const files = {
  '/': ['index.html', 'text/html'],
  '/symbols.ttf': ['DejaVuSansMono.ttf', 'font/ttf'],
  '/font.ttf': ['JetBrainsMono-Regular.ttf', 'font/ttf'],
  '/client.js': ['client.js', 'text/javascript'],
  '/xterm.js': ['node_modules/@xterm/xterm/lib/xterm.js', 'text/javascript'],
  '/xterm.css': ['node_modules/@xterm/xterm/css/xterm.css', 'text/css'],
  '/webgl.js': ['node_modules/@xterm/addon-webgl/lib/addon-webgl.js', 'text/javascript'],
  '/fit.js': ['node_modules/@xterm/addon-fit/lib/addon-fit.js', 'text/javascript'],
};
const allowedHost = host => [`127.0.0.1:${port}`, `localhost:${port}`].includes(host);
const server = http.createServer((req, res) => {
  if (!allowedHost(req.headers.host)) { res.writeHead(403).end(); return; }
  const file = files[new URL(req.url, 'http://localhost').pathname];
  if (!file) { res.writeHead(404).end(); return; }
  res.writeHead(200, {'Content-Type': file[1], 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'});
  res.end(readFileSync(path.join(here, file[0])));
});
const shellEnv = {...process.env};
delete shellEnv.NO_COLOR;
const wss = new WebSocketServer({noServer: true, maxPayload: 64 * 1024});
server.on('upgrade', (req, socket, head) => {
  if (!allowedHost(req.headers.host) || ![`http://localhost:${port}`, `http://127.0.0.1:${port}`].includes(req.headers.origin) || req.url !== '/terminal') {
    socket.destroy(); return;
  }
  wss.handleUpgrade(req, socket, head, ws => wss.emit('connection', ws));
});
wss.on('connection', ws => {
  // Fish's config and history are scoped to this session, not the user's login shell.
  const term = spawn(path.join(root, '.venv/bin/python'), ['-u', path.join(here, 'pty_bridge.py'), shell, path.join(here, 'profile.fish')], {
    cwd: root, env: {...shellEnv, TERM: 'xterm-256color', COLORTERM: 'truecolor',
      PATH: `${path.join(root, '.venv/bin')}:${process.env.PATH}`, fish_history: '', CAPTURE_ROOT: root},
  });
  term.stdout.setEncoding('utf8');
  term.stdout.on('data', data => { if (ws.readyState === 1) ws.send(data); });
  term.stderr.on('data', data => console.error(data.toString()));
  term.on('exit', () => ws.close());
  term.on('error', error => {console.error(error); ws.close(1011, 'Could not start terminal');});
  ws.on('message', raw => {
    try {
      const message = JSON.parse(raw.toString());
      if (message.type === 'input' && typeof message.data === 'string' ||
          message.type === 'resize' && Number.isInteger(message.cols) && Number.isInteger(message.rows))
        term.stdin.write(JSON.stringify(message) + '\n');
      else ws.close(1008, 'Invalid message');
    } catch { ws.close(1008, 'Invalid message'); }
  });
  term.stdin.on('error', () => {});
  ws.on('close', () => term.kill());
});
server.listen(port, '127.0.0.1', () => console.log(`Capture terminal: http://127.0.0.1:${port}`));
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => {
  for (const ws of wss.clients) ws.terminate();
  server.close(() => process.exit(0));
});
