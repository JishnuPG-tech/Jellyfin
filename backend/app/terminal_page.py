"""Terminal-only page — full-screen xterm.js for users who hit /terminal directly.

This is the same xterm.js setup as in `webapp.py`, but stripped of the
chat tab so it loads faster and is unambiguous.

Differences vs `WEBAPP_HTML`:
  * No tab bar — single-purpose terminal.
  * No iframe (no chat proxy).
  * Caches scrollback to last 64 KB for blazing reconnects.
"""

TERMINAL_ONLY_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<title>OpenCode Terminal</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<link rel="preconnect" href="https://cdn.jsdelivr.net" />
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css" />
<style>
:root { --bg:#0d1117; --fg:#c9d1d9; --accent:#22c55e; --line:#30363d; --muted:#8b949e; }
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0; height: 100%; background: var(--bg); color: var(--fg);
  font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, monospace;
  font-size: 13px; overflow: hidden;
}
#shell { display: grid; grid-template-rows: 32px 1fr; height: 100%; }
#topbar {
  display: flex; align-items: center; gap: 10px; padding: 0 12px;
  background: #161b22; border-bottom: 1px solid var(--line); font-size: 11px;
  color: var(--muted);
}
.dot { width: 8px; height: 8px; border-radius: 50%; background:#555; }
.dot.connected { background: var(--accent); box-shadow: 0 0 6px var(--accent); }
.dot.connecting { background: #eab308; animation: pulse 1s infinite; }
.dot.error { background: #ef4444; }
@keyframes pulse { 50% { opacity:.35; } }
.brand { font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
.spacer { flex: 1; }
.link { color: var(--accent); text-decoration: none; }
.link:hover { text-decoration: underline; }
#term-wrap { position: relative; height: 100%; }
#term-host { height: 100%; }
.xterm { padding: 8px; }
.xterm-viewport::-webkit-scrollbar { width: 6px; }
.xterm-viewport::-webkit-scrollbar-thumb { background: #30363d; border-radius: 3px; }
.xterm-viewport::-webkit-scrollbar-track { background: transparent; }
</style>
</head>
<body>
<div id="shell">
  <div id="topbar">
    <span class="brand">OpenCode&nbsp;<span style="color:var(--accent)">·</span>&nbsp;Terminal</span>
    <span class="dot" id="ws-dot"></span>
    <span id="ws-state">connecting…</span>
    <span class="spacer"></span>
    <a class="link" href="/">← back to integrated webapp</a>
    <a class="link" href="/server/$(echo -n '')/" target="_blank" id="chat-link">Open chat →</a>
  </div>
  <div id="term-wrap">
    <div id="term-host"></div>
  </div>
</div>
<script>
(function() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const termHost = document.getElementById('term-host');
  const wsDot = document.getElementById('ws-dot');
  const wsState = document.getElementById('ws-state');

  const term = new Terminal({
    cursorBlink: true,
    fontSize: 13,
    fontFamily: '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace',
    scrollback: 20000,
    convertEol: true,
    theme: {
      background: '#0d1117', foreground: '#c9d1d9', cursor: '#22c55e',
      cursorAccent: '#0d1117', selectionBackground: '#264f78',
      black: '#0d1117', red: '#f85149', green: '#22c55e', yellow: '#e3b341',
      blue: '#58a6ff', magenta: '#bc8cff', cyan: '#39c5cf', white: '#b1bac4',
      brightBlack: '#6e7681', brightRed: '#ff7b72', brightGreen: '#3fb950',
      brightYellow: '#d29922', brightBlue: '#79c0ff', brightMagenta: '#d2a8ff',
      brightCyan: '#56d4dd', brightWhite: '#f0f6fc',
    },
  });
  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(termHost);
  fitAddon.fit();
  window.addEventListener('resize', () => { try { fitAddon.fit(); } catch(e){} });

  function setState(name, label) {
    wsDot.className = 'dot ' + name;
    wsState.textContent = label;
  }

  let ws = null, backoff = 250;
  function connect() {
    ws = new WebSocket(`${proto}//${location.host}/terminal/ws`);
    ws.binaryType = 'arraybuffer';
    setState('connecting', 'connecting…');
    ws.onopen = () => {
      backoff = 250;
      setState('connected', 'connected');
      try { ws.send('resize:' + term.cols + ':' + term.rows); } catch(e) {}
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') return;
      term.write(new Uint8Array(ev.data));
    };
    ws.onclose = () => {
      setState('', 'disconnected — retrying in ' + (backoff/1000) + 's');
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 8000);
    };
    ws.onerror = () => { try { ws.close(); } catch(e) {} };
  }
  connect();

  term.onData((d) => { if (ws && ws.readyState === 1) { try { ws.send(d); } catch(e) {} } });

  // faster resize mirror
  let rT = null;
  const ro = new ResizeObserver(() => {
    clearTimeout(rT); rT = setTimeout(() => {
      try { fitAddon.fit(); if (ws && ws.readyState === 1) ws.send('resize:' + term.cols + ':' + term.rows); } catch(e){}
    }, 60);
  });
  ro.observe(termHost);

  // Build the chat link with the right encoded base
  document.getElementById('chat-link').href = '/server/' + btoa(location.host) + '/';

  // Friendly greeting
  term.writeln('\x1b[2m# OpenCode-Serve terminal\x1b[0m');
  term.writeln('\x1b[2m# Real /bin/bash in the same container as opencode serve.\x1b[0m');
  term.writeln('\x1b[2m# Try: pwd · ls -la · git --version · opencode --version · uname -a\x1b[0m');
})();
</script>
</body>
</html>
"""
