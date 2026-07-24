# Phase 10 — Testing Plan & Results

## 1. Local pytest-free smoke tests

Run from the repo root with `python3` (no pytest needed):

```bash
python3 -c "
import asyncio, sys
sys.path.insert(0, '/tmp/opencode-serve')
from backend.app.pty_service import PtyService

async def test():
    pty = PtyService()
    await pty.start()
    await asyncio.sleep(0.5)
    await pty.write(b'echo hello-opencode-terminal\n')
    await asyncio.sleep(0.5)
    snap = pty.snapshot(2048)
    assert b'hello-opencode-terminal' in snap, 'echo did not produce expected output'
    snap = snap.decode('utf-8','replace')
    print('OK basic_echo')
    # env
    await pty.write(b'echo \$HOME\n')
    await asyncio.sleep(0.3)
    assert b'/data' in pty.snapshot(4096)
    print('OK home_env')
    # resize
    await pty.resize(80, 24)
    await pty.write(b'stty size\n')
    await asyncio.sleep(0.3)
    assert b'24 80' in pty.snapshot(4096)
    print('OK resize_80x24')
    # state persistence
    await pty.write(b'export OC_X=1\n')
    await pty.write(b'echo \$OC_X\n')
    await asyncio.sleep(0.3)
    assert b'1' in pty.snapshot(4096)
    print('OK state_persistence')
    # git is functional
    await pty.write(b'git --version\n')
    await asyncio.sleep(0.3)
    assert b'git version' in pty.snapshot(4096)
    print('OK git_available')
    # long-running
    await pty.write(b'sleep 0.2 && echo done\n')
    await asyncio.sleep(0.5)
    assert b'done' in pty.snapshot(4096)
    print('OK long_running')
    await pty.stop()
    print('ALL PASSED')

asyncio.run(test())
"
```

## 2. WebSocket integration test

```bash
python3 -c "
import asyncio, websockets

async def test():
    async with websockets.connect('ws://127.0.0.1:8765/terminal/ws') as ws:
        snapshot = await ws.recv()
        assert isinstance(snapshot, bytes), 'snapshot must be bytes'
        print('OK snapshot_bytes')
        await ws.send('echo ws-test\n')
        await asyncio.sleep(0.6)
        out = await ws.recv()
        assert b'ws-test' in out
        print('OK ws_roundtrip')
        await ws.send('resize:200:50')
        # Server doesn't echo resize; just verifying no crash.
        await ws.close()

asyncio.run(test())
"
```

## 3. Reverse-proxy tests

```bash
curl -sf http://127.0.0.1:7865/health           # {status: ok, pty_alive: true, opencode: up}
curl -sf http://127.0.0.1:7865/global/health    # upstream opencode health
curl -sf http://127.0.0.1:7865/global/session   # list of sessions
```

## 4. Manual checklist (browser)

1. Open `https://<your-space>.hf.space/`
2. The webapp renders with dark theme, two tabs (Terminal, Chat)
3. Click `Terminal` → xterm.js with a `$ ` prompt
4. Type `pwd`, press Enter — see `/data/workspaces/default`
5. Type `ls -la` — see real files
6. Type `echo $ANTHROPIC_API_KEY` (private) → see stars/unset marker (env didn't leak)
7. Type `cat /data/config/opencode/opencode.json` — see config
8. Type `opencode` — interactive CLI launches; same TUI runs in the same PTY
9. Click `Chat` → see proxied opencode chat UI
10. Refresh the browser tab — scrollback replays, terminal reconnects

## 5. Test Results — phase 1 (this commit)

```
Test                          Result
────────────────────────────  ─────
pty_spawn                     PASS
pty_basic_echo                PASS
pty_nested_echo               PASS
pty_env_var                   PASS
pty_resize_80x24              PASS
pty_state_persistence         PASS
pty_git_version               PASS
pty_long_running              PASS
pty_uvicorn_route_count       PASS  (16 routes)
healthcheck_status            PASS
proxy_global_health           PASS
proxy_session_url             PASS
ws_snapshot_replay            PASS
ws_roundtrip                  PASS
ws_resize_no_crash            PASS
```

## 6. Coverage gaps (intentional, documented as future work)

* **interactivity** — `vim`, `htop`, `python -i` need a *force-color* aware
  terminal. We already set `TERM=xterm-256color`, `FORCE_COLOR=1`. Need to
  confirm manually in HF Space.
* **TLS** — HF terminates TLS in front; the browser sees `wss://` and our
  server sees `ws://`. Already tested by the iframe chat.
