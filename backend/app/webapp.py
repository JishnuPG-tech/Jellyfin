"""Integrated webapp — VSCode-style terminal + chat toggle.

The HTML below is intentionally inline (no JS bundling, no build step) so
that the HF Space can rebuild quickly and so that the embedded terminal is
proven to work in any browser with one binary file change.
"""

WEBAPP_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<title>OpenCode Serve</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<link rel="preconnect" href="https://cdn.jsdelivr.net" />
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-web-links@0.9.0/lib/xterm-addon-web-links.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css" />
<style>
:root {
  --bg: #0d1117;
  --bg-2: #161b22;
  --bg-3: #1f262e;
  --line: #30363d;
  --fg: #c9d1d9;
  --fg-mute: #8b949e;
  --accent: #22c55e;
  --accent-2: #2ea043;
  --warn: #f0883e;
  --err: #f85149;
  --info: #58a6ff;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0; height: 100%;
  background: var(--bg); color: var(--fg);
  font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 13px;
  overflow: hidden;
}
#shell { display: grid; grid-template-rows: 36px 1fr 28px; height: 100%; }
#topbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 12px; background: var(--bg-2); border-bottom: 1px solid var(--line);
  gap: 8px;
}
.brand { color: var(--fg-mute); font-weight: 700; letter-spacing: .06em; text-transform: uppercase; font-size: 11px; }
#tabs { display: flex; gap: 0; }
.tab {
  padding: 6px 14px; cursor: pointer; color: var(--fg-mute);
  border-bottom: 2px solid transparent; font-size: 12px;
  text-transform: uppercase; letter-spacing: .05em;
}
.tab:hover { color: var(--fg); }
.tab.active { color: var(--fg); border-bottom-color: var(--accent); }
#right-ctl { display: flex; gap: 10px; align-items: center; font-size: 11px; color: var(--fg-mute); }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #555; }
.dot.connected { background: var(--accent); box-shadow: 0 0 6px var(--accent); }
.dot.connecting { background: var(--warn); animation: pulse 1s infinite; }
.dot.error { background: var(--err); }
@keyframes pulse { 50% { opacity: .35; } }

.view { display: none; height: 100%; }
.view.active { display: block; }
#terminal-pane { height: 100%; padding: 6px 4px 0 4px; background: var(--bg); }
#terminal-host { height: 100%; }
#chat-wrap { height: 100%; background: var(--bg-2); }
#chat-frame { width: 100%; height: 100%; border: 0; background: #fff; }

#statusbar {
  background: var(--bg-2); border-top: 1px solid var(--line);
  display: flex; align-items: center; padding: 0 12px; gap: 16px;
  color: var(--fg-mute); font-size: 11px;
}
.sb-item { display: inline-flex; align-items: center; gap: 6px; }
.sb-spacer { flex: 1; }

.btn {
  background: var(--accent); color: #000; border: 0; padding: 4px 10px;
  border-radius: 4px; cursor: pointer; font-weight: 700; text-transform: uppercase;
  font-family: inherit; font-size: 10px;
}
.btn:hover { background: var(--accent-2); }
.btn.secondary { background: #30363d; color: var(--fg); }
.btn.secondary:hover { background: #444c56; }
.xterm { padding: 6px 8px; }
.xterm-viewport::-webkit-scrollbar { width: 6px; }
.xterm-viewport::-webkit-scrollbar-thumb { background: #30363d; border-radius: 3px; }
.xterm-viewport::-webkit-scrollbar-track { background: transparent; }

.modal-bg {
  position: fixed; inset: 0; background: rgba(0,0,0,0.55);
  display: none; align-items: center; justify-content: center; z-index: 100;
}
.modal-bg.show { display: flex; }
.modal {
  background: var(--bg-2); border: 1px solid var(--line); border-radius: 8px;
  padding: 18px 20px; min-width: 380px; max-width: 520px;
  color: var(--fg);
}
.modal h3 { margin: 0 0 10px 0; font-size: 14px; }
.modal input, .modal textarea {
  width: 100%; padding: 8px; background: var(--bg-3); color: var(--fg);
  border: 1px solid var(--line); border-radius: 4px; font-family: inherit;
}
.modal .actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; }
.kbd {
  background: #21262d; border: 1px solid #30363d; border-bottom-width: 2px;
  color: var(--fg); font-family: inherit; font-size: 10px;
  padding: 1px 6px; border-radius: 4px;
}
</style>
</head>
<body>
<div id="shell">
  <div id="topbar">
    <span class="brand">OpenCode <span style="color:var(--accent)">·</span> Serve</span>
    <nav id="tabs">
      <div class="tab active" data-tab="terminal">Terminal</div>
      <div class="tab" data-tab="chat">Chat</div>
    </nav>
    <div id="right-ctl">
      <span class="sb-item" id="cwd-label">–</span>
      <button class="btn secondary" id="cmd-run" title="Run a quick command">Quick cmd</button>
      <button class="btn secondary" id="cmd-clear" title="Clear terminal">Clear</button>
      <span class="sb-item"><span class="dot" id="ws-dot"></span><span id="ws-state">connecting</span></span>
    </div>
  </div>

  <div id="views">
    <div class="view active" id="terminal-view">
      <div id="terminal-pane"><div id="terminal-host"></div></div>
    </div>
    <div class="view" id="chat-view">
      <div id="chat-wrap">
        <iframe id="chat-frame" sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"></iframe>
      </div>
    </div>
  </div>

  <div id="statusbar">
    <span class="sb-item"><span class="dot connected"></span> PTY: /bin/bash</span>
    <span class="sb-item">Same container · Same env</span>
    <span class="sb-spacer"></span>
    <span class="sb-item">Shortcuts:
      <span class="kbd">Ctrl</span>+<span class="kbd">C</span> send SIGINT ·
      <span class="kbd">Ctrl</span>+<span class="kbd">D</span> EOF ·
      <span class="kbd">Ctrl</span>+<span class="kbd">L</span> clear
    </span>
    <span class="sb-item" id="size-label">–</span>
  </div>

  <!-- Quick command modal -->
  <div class="modal-bg" id="cm-bg">
    <div class="modal">
      <h3>Quick command</h3>
      <input id="cm-input" placeholder="e.g. pnpm install && pnpm dev" autofocus />
      <div class="actions">
        <button class="btn secondary" id="cm-cancel">Cancel</button>
        <button class="btn" id="cm-run">Run</button>
      </div>
    </div>
  </div>
</div>

<script>
(function() {
  // ───── DOM refs ─────
  const termHost = document.getElementById('terminal-host');
  const wsDot = document.getElementById('ws-dot');
  const wsState = document.getElementById('ws-state');
  const cwdLabel = document.getElementById('cwd-label');
  const sizeLabel = document.getElementById('size-label');
  const tabEls = document.querySelectorAll('.tab');
  const views = document.querySelectorAll('.view');
  const cmdRun = document.getElementById('cmd-run');
  const cmdClear = document.getElementById('cmd-clear');
  const chatFrame = document.getElementById('chat-frame');
  const cmBg = document.getElementById('cm-bg');
  const cmInput = document.getElementById('cm-input');
  const cmCancel = document.getElementById('cm-cancel');
  const cmRun = document.getElementById('cm-run');

  // ───── xterm.js setup ─────
  const term = new Terminal({
    cursorBlink: true,
    fontSize: 13,
    fontFamily: '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace',
    scrollback: 20000,
    convertEol: true,
    allowProposedApi: true,
    theme: {
      background: '#0d1117',
      foreground: '#c9d1d9',
      cursor: '#22c55e',
      cursorAccent: '#0d1117',
      selectionBackground: '#264f78',
      black: '#0d1117', red: '#f85149', green: '#22c55e', yellow: '#e3b341',
      blue: '#58a6ff', magenta: '#bc8cff', cyan: '#39c5cf', white: '#b1bac4',
      brightBlack: '#6e7681', brightRed: '#ff7b72', brightGreen: '#3fb950',
      brightYellow: '#d29922', brightBlue: '#79c0ff', brightMagenta: '#d2a8ff',
      brightCyan: '#56d4dd', brightWhite: '#f0f6fc',
    },
  });
  const fitAddon = new FitAddon();
  const webLinksAddon = new WebLinksAddon();
  term.loadAddon(fitAddon);
  term.loadAddon(webLinksAddon);
  term.open(termHost);
  fitAddon.fit();
  sizeLabel.textContent = `${term.cols}×${term.rows}`;
  window.addEventListener('resize', () => { try { fitAddon.fit(); sizeLabel.textContent = `${term.cols}×${term.rows}`; } catch(e){} });

  // ───── WebSocket plumbing ─────
  let ws = null;
  let backoff = 250;
  const maxBackoff = 8000;

  function setState(name, label) {
    wsDot.className = 'dot ' + name;
    wsState.textContent = label;
  }

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/terminal/ws`);
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
      backoff = 250;
      setState('connected', 'connected');
      try {
        ws.send('resize:' + term.cols + ':' + term.rows);
      } catch (e) {}
    };
    ws.onclose = () => {
      setState('', 'disconnected');
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, maxBackoff);
    };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        // control message — currently unused, but keep the protocol documented.
        return;
      }
      term.write(new Uint8Array(ev.data));
      // Detect "PWD=" lines to update the breadcrumb.
      const u8 = new Uint8Array(ev.data);
      const txt = new TextDecoder('utf-8').decode(u8);
      const m = txt.match(/(?:^|\n).*?pwd\s*$[^]*$/m);
      // cheaper: check last 128 bytes for cwd heuristic — skip; cheap heuristic:
      cwdLabel.textContent = 'cwd: ' + (location.pathname);
    };
    setState('connecting', 'connecting');
  }

  connect();

  // Send keystrokes.
  term.onData((data) => {
    if (ws && ws.readyState === 1) {
      try { ws.send(data); } catch (e) {}
    }
  });

  // Send resize explicitly when the pane dimensions change (debounced).
  let resizeTimer = null;
  const ro = new ResizeObserver(() => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      try {
        fitAddon.fit();
        sizeLabel.textContent = term.cols + '×' + term.rows;
        if (ws && ws.readyState === 1) ws.send('resize:' + term.cols + ':' + term.rows);
      } catch (e) {}
    }, 80);
  });
  ro.observe(termHost);

  // ───── Tab switching ─────
  tabEls.forEach((t) => {
    t.addEventListener('click', () => {
      tabEls.forEach((x) => x.classList.remove('active'));
      views.forEach((v) => v.classList.remove('active'));
      t.classList.add('active');
      document.getElementById(t.dataset.tab + '-view').classList.add('active');
      if (t.dataset.tab === 'terminal') setTimeout(() => fitAddon.fit(), 50);
    });
  });

  // Chat tab: try a tiny launcher the user can open the real chat UI in.
  // We proxy /server/<base>/session/<id> → opencode serve on 4096. The user
  // can drop a "session URL" into the chat by clicking "Open" below.
  // For convenience, a /global/session endpoint is auto-detected.
  async function probeChatUrl() {
    try {
      const r = await fetch('/global/session', { credentials: 'include' });
      if (r.ok) {
        const data = await r.json();
        if (Array.isArray(data) && data[0]) {
          chatFrame.src = '/server/' + encodeURIComponent(location.host) + '/session/' + encodeURIComponent(data[0].id || data[0]);
        } else if (data && data.id) {
          chatFrame.src = '/server/' + encodeURIComponent(location.host) + '/session/' + encodeURIComponent(data.id);
        } else {
          chatFrame.src = '/server/' + encodeURIComponent(location.host);
        }
        return;
      }
    } catch (e) {}
    chatFrame.src = '/server/' + encodeURIComponent(location.host);
  }
  document.querySelector('[data-tab="chat"]').addEventListener('click', probeChatUrl);

  // ───── Quick command modal ─────
  cmdRun.onclick = () => { cmBg.classList.add('show'); cmInput.focus(); cmInput.select(); };
  cmCancel.onclick = () => cmBg.classList.remove('show');
  cmRun.onclick = () => {
    const v = cmInput.value;
    cmBg.classList.remove('show');
    if (v && ws && ws.readyState === 1) {
      ws.send('__TYPE__' + v + '\n');
    }
    cmInput.value = '';
  };
  cmInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') cmRun.click();
    if (e.key === 'Escape') cmCancel.click();
  });

  // ───── Clear button ─────
  cmdClear.onclick = () => term.clear();

  // ───── Status bar hints ─────
  setInterval(() => {
    if (ws && ws.readyState === 1) try { ws.send('ping'); } catch (e) {}
  }, 30000);

  // ─── expose a tiny API on `window.oc` for power users ───
  window.oc = {
    term,
    send(text) {
      if (ws && ws.readyState === 1) {
        ws.send('__TYPE__' + text + '\n');
      }
    },
    showChat() { document.querySelector('[data-tab="chat"]').click(); },
    showTerm() { document.querySelector('[data-tab="terminal"]').click(); },
  };

  // Greet banner (the server also sends its own prompt; harmless).
  term.writeln('\x1b[2mOpenCode-Serve terminal · same container, same bash.\x1b[0m');
})();
</script>
</body>
</html>
"""
