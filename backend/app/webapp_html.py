HTML_CONTENT = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>OpenCode CLI</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }

        html, body {
            height: 100%;
            background: #000;
            color: #e0e0e0;
            font-family: 'JetBrains Mono', 'Courier New', monospace;
            font-size: 13px;
            line-height: 1.45;
            overflow: hidden;
            -webkit-font-smoothing: antialiased;
        }

        /* ── Top Bar ── */
        .topbar {
            height: 36px;
            background: #0a0a0a;
            border-bottom: 1px solid #1a1a1a;
            display: flex;
            align-items: center;
            padding: 0 12px;
            gap: 10px;
            flex-shrink: 0;
        }

        .topbar-title {
            color: #555;
            font-size: 11px;
            letter-spacing: 0.5px;
            text-transform: uppercase;
        }

        .topbar-status {
            margin-left: auto;
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 11px;
            color: #444;
        }

        .topbar-dot {
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background: #333;
        }

        .topbar-dot.connected { background: #22c55e; box-shadow: 0 0 6px #22c55e; }
        .topbar-dot.connecting { background: #eab308; animation: pulse 1s infinite; }

        @keyframes pulse { 50% { opacity: 0.4; } }

        /* ── Terminal ── */
        #terminal {
            position: absolute;
            top: 36px;
            left: 0;
            right: 0;
            bottom: 0;
            padding: 12px 14px;
            overflow-y: auto;
            overflow-x: hidden;
            white-space: pre-wrap;
            word-break: break-all;
            color: #c8c8c8;
            scroll-behavior: smooth;
        }

        #terminal::-webkit-scrollbar { width: 6px; }
        #terminal::-webkit-scrollbar-track { background: transparent; }
        #terminal::-webkit-scrollbar-thumb { background: #222; border-radius: 3px; }

        .cursor {
            display: inline-block;
            width: 7px;
            height: 14px;
            background: #c8c8c8;
            vertical-align: text-bottom;
            animation: blink 1s step-end infinite;
        }

        @keyframes blink { 50% { opacity: 0; } }

        /* ── Input Bar ── */
        .input-bar {
            position: fixed;
            bottom: 0;
            left: 0;
            right: 0;
            background: #0a0a0a;
            border-top: 1px solid #1a1a1a;
            padding: 8px 10px;
            display: flex;
            align-items: center;
            gap: 8px;
            z-index: 20;
            padding-bottom: max(8px, env(safe-area-inset-bottom));
        }

        .prompt-symbol {
            color: #22c55e;
            font-weight: 700;
            font-size: 13px;
            flex-shrink: 0;
        }

        #cmd {
            flex: 1;
            background: transparent;
            border: none;
            outline: none;
            color: #e0e0e0;
            font-family: 'JetBrains Mono', monospace;
            font-size: 13px;
            caret-color: #22c55e;
        }

        #cmd::placeholder { color: #333; }

        /* ── Key Row ── */
        .keyrow {
            position: fixed;
            bottom: 48px;
            left: 0;
            right: 0;
            background: #050505;
            border-top: 1px solid #111;
            padding: 6px 8px;
            display: flex;
            gap: 5px;
            overflow-x: auto;
            z-index: 19;
            -webkit-overflow-scrolling: touch;
        }

        .keyrow::-webkit-scrollbar { display: none; }

        .kbtn {
            flex-shrink: 0;
            background: #0f0f0f;
            border: 1px solid #1a1a1a;
            color: #666;
            border-radius: 4px;
            padding: 6px 10px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 10px;
            font-weight: 500;
            cursor: pointer;
            text-transform: uppercase;
            letter-spacing: 0.3px;
        }

        .kbtn:active {
            background: #1a1a1a;
            color: #aaa;
            border-color: #333;
        }

        .kbtn.accent {
            color: #22c55e;
            border-color: #166534;
        }

        /* ── Login Card ── */
        .login-card {
            background: #0a0a0a;
            border: 1px solid #1a1a1a;
            border-radius: 8px;
            padding: 24px;
            margin: 16px 0;
            text-align: center;
        }

        .login-card a {
            display: inline-block;
            margin-top: 12px;
            padding: 8px 20px;
            background: #22c55e;
            color: #000;
            text-decoration: none;
            border-radius: 6px;
            font-weight: 700;
            font-size: 12px;
        }
    </style>
</head>
<body>

<div class="topbar">
    <span class="topbar-title">opencode</span>
    <div style="margin-left: 20px; display: flex; align-items: center; gap: 8px;">
        <span style="font-size: 11px; color: #555; font-weight: 500;">WORKSPACE:</span>
        <select id="workspace-select" style="background: #0f0f0f; color: #bbb; border: 1px solid #222; border-radius: 4px; padding: 3px 8px; font-size: 11px; font-family: 'JetBrains Mono', monospace; outline: none; cursor: pointer;" onchange="changeWorkspace()">
            <option value="default">default</option>
        </select>
        <button onclick="promptClone()" style="background: #22c55e; border: none; color: #000; font-size: 10px; font-weight: 700; padding: 4px 10px; border-radius: 4px; cursor: pointer; text-transform: uppercase; font-family: 'JetBrains Mono', monospace;">CLONE REPO</button>
        <button onclick="promptDelete()" style="background: #ef4444; border: none; color: #fff; font-size: 10px; font-weight: 700; padding: 4px 10px; border-radius: 4px; cursor: pointer; text-transform: uppercase; font-family: 'JetBrains Mono', monospace;">DELETE</button>
        <button onclick="window.open('/terminal', '_blank')" style="background: #22c55e; border: none; color: #000; font-size: 10px; font-weight: 700; padding: 4px 10px; border-radius: 4px; cursor: pointer; text-transform: uppercase; font-family: 'JetBrains Mono', monospace;">TERMINAL</button>
    </div>
    <div class="topbar-status">
        <div class="topbar-dot" id="ws-dot"></div>
        <span id="ws-label">connecting</span>
    </div>
</div>

<div id="terminal"><span class="cursor" id="cursor"></span></div>

<div class="keyrow" id="keyrow">
    <button class="kbtn" onclick="sendKey('Tab')">Tab</button>
    <button class="kbtn" onclick="sendKey('Up')">Up</button>
    <button class="kbtn" onclick="sendKey('Down')">Down</button>
    <button class="kbtn" onclick="sendKey('Left')">Left</button>
    <button class="kbtn" onclick="sendKey('Right')">Right</button>
    <button class="kbtn" onclick="sendKey('Escape')">Esc</button>
    <button class="kbtn" onclick="sendKey('BSpace')">Bksp</button>
    <button class="kbtn" onclick="sendKey('PPage')">PgUp</button>
    <button class="kbtn" onclick="sendKey('NPage')">PgDn</button>
    <button class="kbtn" onclick="sendKey('Home')">Home</button>
    <button class="kbtn" onclick="sendKey('End')">End</button>
    <button class="kbtn" onclick="sendCtrl('C-c')">Ctrl+C</button>
    <button class="kbtn" onclick="sendCtrl('C-d')">Ctrl+D</button>
    <button class="kbtn accent" onclick="sendCommand('opencode')">Launch</button>
</div>

<div class="input-bar">
    <span class="prompt-symbol">&gt;</span>
    <input type="text" id="cmd" placeholder="type command..." autocomplete="off" autocapitalize="off" spellcheck="false">
</div>

<script>
(function() {
    const params = new URLSearchParams(location.search);
    const userId = params.get('user_id') || '1769298522';
    const term = document.getElementById('terminal');
    const cursor = document.getElementById('cursor');
    const cmdInput = document.getElementById('cmd');
    const wsDot = document.getElementById('ws-dot');
    const wsLabel = document.getElementById('ws-label');
    const wsSelect = document.getElementById('workspace-select');
    let ws = null;
    let lastContent = '';
    let currentProject = 'default';
    let reconnectTimer = null;

    // ── Workspace Functions ──
    let workspaceStatuses = {};

    async function loadWorkspaces() {
        try {
            const [listResp, statusResp] = await Promise.all([
                fetch(`/api/workspace/list?user_id=${userId}`),
                fetch(`/api/workspace/status?user_id=${userId}`).catch(() => ({ json: () => ({ workspaces: [] }) }))
            ]);
            const data = await listResp.json();
            const statusData = await statusResp.json();

            workspaceStatuses = {};
            (statusData.workspaces || []).forEach(ws => {
                workspaceStatuses[ws.name] = ws;
            });

            if (data.folders) {
                const prevSel = wsSelect.value || currentProject;
                wsSelect.innerHTML = '';
                data.folders.forEach(f => {
                    const opt = document.createElement('option');
                    opt.value = f;
                    const st = workspaceStatuses[f];
                    const indicator = st && st.status === 'running' ? ' ●' : '';
                    opt.textContent = f + indicator;
                    wsSelect.appendChild(opt);
                });
                if (data.folders.includes(prevSel)) {
                    wsSelect.value = prevSel;
                } else {
                    currentProject = data.folders[0] || 'default';
                    wsSelect.value = currentProject;
                }
            }
        } catch (e) {
            console.error("Failed to load workspaces:", e);
        }
    }

    window.changeWorkspace = function() {
        currentProject = wsSelect.value;
        term.innerHTML = `<span style="color:#555;">Switching to workspace: ${currentProject}...</span>\n<span class="cursor" id="cursor"></span>`;
        if (ws) {
            if (reconnectTimer) {
                clearTimeout(reconnectTimer);
                reconnectTimer = null;
            }
            ws.onclose = null;
            ws.close();
        }
        connect();
    };

    window.promptClone = async function() {
        const repoUrl = prompt("Enter Git Repository URL to clone (e.g. https://github.com/username/project.git):");
        if (!repoUrl) return;
        const folderName = prompt("Enter local folder name (optional, defaults to repo name):");
        
        term.innerHTML = `<span style="color:#22c55e;">Cloning ${repoUrl}... (this may take a few moments)</span>\n<span class="cursor" id="cursor"></span>`;
        
        try {
            const resp = await fetch('/api/workspace/clone', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id: parseInt(userId), repo_url: repoUrl, folder_name: folderName || null })
            });
            const data = await resp.json();
            if (resp.ok) {
                alert(`Successfully cloned: ${data.folder}`);
                await loadWorkspaces();
                wsSelect.value = data.folder;
                changeWorkspace();
            } else {
                alert(`Clone failed: ${data.detail || 'Unknown error'}`);
                term.innerHTML = `<span style="color:#ef4444;">Clone failed: ${data.detail || 'Unknown error'}</span>\n<span class="cursor" id="cursor"></span>`;
            }
        } catch (e) {
            alert(`Clone failed: ${e.message}`);
            term.innerHTML = `<span style="color:#ef4444;">Clone failed: ${e.message}</span>\n<span class="cursor" id="cursor"></span>`;
        }
    };

    window.promptDelete = async function() {
        const activeFolder = wsSelect.value;
        if (activeFolder === 'default') {
            alert("Cannot delete the default workspace.");
            return;
        }
        if (!confirm(`Are you sure you want to delete workspace "${activeFolder}"? This will delete all files permanently.`)) {
            return;
        }
        try {
            const resp = await fetch('/api/workspace/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id: parseInt(userId), folder_name: activeFolder })
            });
            const data = await resp.json();
            if (resp.ok) {
                alert(`Deleted workspace: ${activeFolder}`);
                await loadWorkspaces();
                changeWorkspace();
            } else {
                alert(`Delete failed: ${data.detail || 'Unknown error'}`);
            }
        } catch (e) {
            alert(`Delete failed: ${e.message}`);
        }
    };

    // ── WebSocket ──
    function connect() {
        const proto = location.protocol === 'https:' ? 'wss' : 'ws';
        const projectParam = encodeURIComponent(currentProject);
        ws = new WebSocket(`${proto}://${location.host}/api/ws/session/${userId}?project=${projectParam}`);
        lastContent = '';

        wsDot.className = 'topbar-dot connecting';
        wsLabel.textContent = 'connecting';

        ws.onopen = () => {
            wsDot.className = 'topbar-dot connected';
            wsLabel.textContent = 'connected';
        };

        ws.onmessage = (e) => {
            try {
                const msg = JSON.parse(e.data);
                if (msg.type === 'output' && msg.text) {
                    renderOutput(msg.text);
                }
            } catch {}
        };

        ws.onclose = () => {
            wsDot.className = 'topbar-dot';
            wsLabel.textContent = 'disconnected';
            if (reconnectTimer) clearTimeout(reconnectTimer);
            reconnectTimer = setTimeout(connect, 3000);
        };

        ws.onerror = () => ws.close();
    }

    function renderOutput(text) {
        let clean = text
            .replace(/\x1b\]8;[^\x1b\x07]*[\x1b\x07]/g, '')
            .replace(/\x1b\[[0-9;?]*[a-zA-Z]/g, '')
            .replace(/\x1b./g, '')
            .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, '')
            .replace(/\[?2004[lh]/g, '')
            .replace(/\[[0-9;?]*[mJKhHdDL]/g, '')
            .trimEnd();

        if (!clean) return;

        const googleMatch = clean.match(/https:\/\/accounts\.google\.com\/o\/oauth2\/auth\?[^\s'"]+/);
        if (googleMatch) {
            const url = googleMatch[0].replace(/[)\]]+$/, '');
            term.innerHTML = `
                <div class="login-card">
                    <div style="font-size:14px;color:#eab308;margin-bottom:8px;">Authentication Required</div>
                    <div style="font-size:11px;color:#666;margin-bottom:12px;">Google sign-in detected. Click below to authorize.</div>
                    <a href="${url}" target="_blank" rel="noopener">Sign in with Google</a>
                    <div style="font-size:10px;color:#444;margin-top:10px;">Paste the code into the input after signing in.</div>
                </div>`;
            return;
        }

        if (clean === lastContent) return;
        lastContent = clean;

        term.textContent = clean;
        term.appendChild(document.getElementById('cursor') || cursor);
        term.scrollTop = term.scrollHeight;
    }

    // ── Send ──
    window.sendCommand = function(text) {
        if (!text) text = cmdInput.value;
        if (!text.trim()) return;
        cmdInput.value = '';
        if (ws && ws.readyState === 1) {
            ws.send(JSON.stringify({ type: 'command', text: text }));
        }
    };

    window.sendKey = function(key) {
        if (ws && ws.readyState === 1) {
            ws.send(JSON.stringify({ type: 'key', key: key }));
        }
    };

    window.sendCtrl = function(key) {
        if (ws && ws.readyState === 1) {
            ws.send(JSON.stringify({ type: 'key', key: key }));
        }
    };

    cmdInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            sendCommand();
        }
    });

    term.addEventListener('click', () => cmdInput.focus());

    loadWorkspaces().then(() => connect());
})();
</script>
</body>
</html>
"""
