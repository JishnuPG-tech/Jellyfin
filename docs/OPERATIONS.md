# Phase 11 — Operation & Maintenance Notes

## Health endpoint

`GET /health` returns:

```json
{
  "status": "ok",
  "pty_alive": true,
  "opencode": "up"
}
```

* `pty_alive` — is the Python `/bin/bash` PTY process still running? `false`
  means we need to restart uvicorn.
* `opencode` — upstream `opencode serve` responding on `:4096`?

## Restarting

The entrypoint script *does not* auto-restart either component on crash.
Both are critical-path; HF Spaces will rebuild the container on crash
anyway via the docker `restart: unless-stopped` policy in
`docker-compose.yml`.

If you ever want to restart just the gateway without rebuilding:

```bash
docker exec opencode-serve pkill -TERM -f uvicorn   # uvicorn respawns via supervise if enabled
```

(We do **not** ship a process supervisor in the image. Add one if you
need fine-grained restarts.)

## Resource usage

The PTY service uses negligible CPU when idle. It only fires when the
user or a tailing script writes data. Memory is dominated by the 200 KB
scrollback buffer (one `bytearray`).

The reverse proxy holds zero per-connection state — it streams bytes
through `httpx` and `StreamingResponse` chunks.

## Security review

| Concern | Mitigation |
|---------|------------|
| Anyone on the internet can issue commands if they can reach the WS | The Space is behind the HF gateway; same trust model as before. For real auth, terminate at HF-level (private Space) or add an `OPENCODE_SERVER_USERNAME`/`OPENCODE_SERVER_PASSWORD` derived Basic-auth header to `/terminal/ws`. |
| `opencode serve` is bound to the open-code port (4096) on `127.0.0.1`. | Not externally reachable from the HF proxy chain. Only the FastAPI gateway at `7860` is. |
| PTY can `Ctrl-S`/`Ctrl-Q` lock | Not enabled (default bash settings use `*-icanon` + `ixon`, but `userspace@container` can opt in). |
| Long-running command blocks the bash | The read loop is in a thread-pool executor, not blocking the event loop. Foreground commands do block the bash prompt, by design (interactive). |
| PTY size limit | `resource.setrlimit(RLIMIT_NPROC, ...)`. Not currently set; add if abuse is a concern. |
| WebSocket origin check | We do not currently enforce an Origin. Spaces are first-party trust; add `Sec-WebSocket-Origin` checks if exposed publicly. |

### Future hardening (out of scope for v1)

* Short-lived JWT cookie + per-connection claim
* Per-user PTYs (name → PtyService dict; we currently share one globally)
* Audit log of every command (cite from `cleaner.py`'s sqlite expertise)
* `setpriv`/`seccomp` profile to deny dangerous syscalls in the bash child

## Known limitations

* **One PTY per container.** Two browsers connecting to `/terminal/ws`
  share the same bash session. This is desired for "shared AI
  terminal" semantics, but typing collisions are possible (uncommon in
  single-user Spaces).
* **Snapshots are 16 KB on reconnect.** Long scrollbacks beyond that get
  truncated.
* **No scrollback persistence across Space restarts.** We only persist
  the bash history file when bash exits (within the container).
