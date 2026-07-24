# Phase 11 — Documentation Index

1. [`ARCHITECTURE.md`](../ARCHITECTURE.md) — high-level design rationale
2. [`ARCHITECTURE_DIAGRAM.md`](../ARCHITECTURE_DIAGRAM.md) — process / data flow diagrams
3. [`STREAMING.md`](../STREAMING.md) — WebSocket vs SSE transportation analysis
4. [`DEPLOYMENT.md`](../DEPLOYMENT.md) — HF Space deployment notes
5. [`TESTING.md`](../TESTING.md) — test plan + results

## Quick "what do I read first?"

* New to the integration: start with **`ARCHITECTURE.md`**.
* Want to debug a live PTY: read `pty_service.py` (~210 lines),
  then `STREAMING.md`.
* Want to understand the chat-side proxy: read `opencode_proxy.py`
  (self-contained).
* Want to customise the UI: read `webapp.py` (everything is one HTML
  file).
* Deploying: `DEPLOYMENT.md`.
