HTML_CONTENT = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>OpenCode CLI</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css">
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

        .topbar-actions {
            margin-left: 20px;
            display: flex;
            align-items: center;
            gap: 8px;
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
        }

        /* ── Workspace Select ── */
        .workspace-select {
            background: #0f0f0f;
            color: #bbb;
            border: 1px solid #222;
            border-radius: 4px;
            padding: 3px 8px;
            font-size: 11px;
            font-family: 'JetBrains Mono', monospace;
            outline: none;
            cursor: pointer;
        }

        .btn {
            background: #22c55e;
            border: none;
            color: #000;
            font-size: 10px;
            font-weight: 700;
            padding: 4px 10px;
            border-radius: 4px;
            cursor: pointer;
            text-transform: uppercase;
            font-family: 'JetBrains Mono', monospace;
        }

        .btn-danger {
            background: #ef4444;
            color: #fff;
        }

        .btn-secondary {
            background: #3b82f6;
            color: #fff;
        }
    </style>
</head>
<body>

<div class="topbar">
    <span class="topbar-title">opencode</span>
    <div class="topbar-actions">
        <span style="font-size: 11px; color: #555; font-weight: 500;">WORKSPACE:</span>
        <select id="workspace-select" class="workspace-select" onchange="changeWorkspace()">
            <option value="default">default</option>
        </select>
        <button class="btn" onclick="promptClone()">CLONE REPO</button>
        <button class="btn btn-danger" onclick="promptDelete()">DELETE</button>
    </div>
    <div class="topbar-status">
        <div class="topbar-dot" id="ws-dot"></div>
        <span id="ws-label">connecting</span>
    </div>
</div>

<div id="terminal"></div>

<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
<script>
(function() {
    const params = new URLSearchParams(location.search);
    const userId = params.get('user_id') || '1769298522';
    const termDiv = document.getElementById('terminal');
    const wsDot = document.getElementById('ws-dot');
    const wsLabel = document.getElementById('ws-label');
    const wsSelect = document.getElementById('workspace-select');
    
    let xterm = null;
    let fitAddon = null;
    let ws = null;
    let reconnectTimer = null;
    let currentProject = 'default';
    let workspaceStatuses = {};

    // ── Initialize xterm.js ──
    function initTerminal() {
        xterm = new Terminal({
            cursorBlink: true,
            fontSize: 13,
            fontFamily: 'JetBrains Mono',
            theme: { background: '#000', foreground: '#c8c8c8', cursor: '#c8c8c8' },
            convertEol: true,
            scrollback: 10000
        });
        fitAddon = new FitAddon.FitAddon();
        xterm.loadAddon(fitAddon);
        xterm.open(termDiv);
        fitAddon.fit();
        window.addEventListener('resize', () => fitAddon.fit());
    }

    // ── WebSocket to ttyd ──
    function connectTerminal() {
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        ws = new WebSocket(`${proto}//${location.host}/terminal/ws`);
        ws.binaryType = 'arraybuffer';
        
        wsDot.className = 'topbar-dot connecting';
        wsLabel.textContent = 'connecting';

        ws.onopen = () => {
            wsDot.className = 'topbar-dot connected';
            wsLabel.textContent = 'connected';
        };

        ws.onmessage = (e) => {
            if (xterm) xterm.write(new Uint8Array(e.data));
        };

        ws.onclose = () => {
            wsDot.className = 'topbar-dot';
            wsLabel.textContent = 'disconnected';
            if (reconnectTimer) clearTimeout(reconnectTimer);
            reconnectTimer = setTimeout(connectTerminal, 3000);
        };

        ws.onerror = () => ws.close();
    }

    // ── Workspace Functions ──
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

    window.changeWorkspace = async function() {
        currentProject = wsSelect.value;
        if (ws) {
            if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
            ws.onclose = null;
            ws.close();
        }
        // When workspace changes, we need to reconnect to ttyd with the new working directory
        // ttyd doesn't support dynamic cwd change, so we'll just reconnect (it will start in the default cwd)
        // For true per-workspace terminals, we'd need a separate ttyd instance per workspace
        connectTerminal();
    };

    window.promptClone = async function() {
        const repoUrl = prompt("Enter Git Repository URL to clone (e.g. https://github.com/username/project.git):");
        if (!repoUrl) return;
        const folderName = prompt("Enter local folder name (optional, defaults to repo name):");
        
        if (xterm) {
            xterm.write('\r\n\x1b[32mCloning ' + repoUrl + '... (this may take a few moments)\x1b[0m\r\n');
        }
        
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
                if (xterm) xterm.write('\r\n\x1b[31mClone failed: ' + (data.detail || 'Unknown error') + '\x1b[0m\r\n');
            }
        } catch (e) {
            alert(`Clone failed: ${e.message}`);
            if (xterm) xterm.write('\r\n\x1b[31mClone failed: ' + e.message + '\x1b[0m\r\n');
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

    // ── Input handling ──
    function sendInput(data) {
        if (ws && ws.readyState === 1) {
            ws.send(data);
        }
    }

    // Initialize
    initTerminal();
    connectTerminal();
    loadWorkspaces().then(() => {});

    // Handle keyboard input
    document.addEventListener('keydown', (e) => {
        // Let xterm handle most keys, but we can intercept special combos if needed
    });

    // Focus terminal on click
    termDiv.addEventListener('click', () => xterm.focus());
})();
</script>
</body>
</html>
"""

(End of file - total 469 lines)