# Phase 7 — Architecture Improvements (Post-Integration)

After successful integration of the embedded terminal, the following improvements are recommended for production hardening and enhanced UX.

## 1. Shared tmux Session Between AI Agent and User Terminal

**Current state:** `ttyd` runs `bash -l` in a fresh shell. The AI agent runs in a separate tmux session (`opencode_user_<id>`). User and AI see different shells.

**Improvement:** Make `ttyd` attach to the same tmux session the AI agent uses:
```bash
# In entrypoint.sh, instead of:
ttyd ... bash -l

# Use:
ttyd ... tmux attach -t opencode_user_${USER_ID} || tmux new -s opencode_user_${USER_ID} bash -l
```

**Benefits:**
- User sees exact same terminal state as AI agent
- Commands typed by user appear in AI's context
- AI's output appears instantly in user's terminal
- True collaborative terminal

**Effort:** Medium (requires user_id mapping, tmux session coordination)

---

## 2. Better PTY Management via ttyd Native Features

**Current state:** Basic `ttyd` with `--writable` flag.

**Improvements:**
- `--signal 1` (SIGHUP) on browser disconnect → cleaner session cleanup
- `--client-option fontSize=14` etc. via URL params for user preferences
- `--check-origin` for additional security
- `--max-connection` limit to prevent resource exhaustion

**Effort:** Low (entrypoint flag changes)

---

## 3. Process Isolation & Resource Limits

**Current state:** All processes (uvicorn, opencode, ttyd, tmux) run as `appuser` with no cgroups limits.

**Improvements:**
- Run `ttyd` under separate systemd/user service or supervisor
- Set `ulimit -u` for process count
- Use Docker `--cpus` / `--memory` limits on HF Space config
- Consider `nsjail` or `firejail` for command sandboxing (if user runs arbitrary code)

**Effort:** Medium (Docker/entrypoint changes)

---

## 4. Terminal Resize Handling

**Current state:** xterm-addon-fit handles initial fit; browser `resize` event calls `fitAddon.fit()`.

**Improvements:**
- Forward SIGWINCH from ttyd to tmux pane: `ttyd` does this automatically with `--writable`
- Add explicit `resize` command handling in FastAPI proxy if needed
- Test rapid resize (mobile rotation, split-screen)

**Effort:** Low (mostly testing)

---

## 5. Reconnection Handling & Session Persistence

**Current state:** Browser JS reconnects every 3s on WS close. ttyd keeps session alive for 30s default.

**Improvements:**
- Increase ttyd `--timeout` for longer disconnect tolerance
- Add "Reconnecting..." UI indicator in terminal page
- Persist scrollback buffer (ttyd doesn't support this natively; would need tmux capture-pane on reconnect)
- Store last working directory in session metadata for faster restore

**Effort:** Medium

---

## 6. Security Hardening

**Current state:** Terminal accessible at `/terminal` with no auth beyond same-origin.

**Improvements:**
- Add token-based auth: FastAPI generates short-lived token, passes to ttyd via `--credential` or URL param
- Rate-limit `/terminal/ws` endpoint
- CSP headers for terminal page (allow xterm.js CDN)
- Audit ttyd version for CVEs (pin to specific release)

**Effort:** Medium

---

## 7. Performance Optimization

**Current state:** WebSocket proxy in Python (FastAPI + websockets library) adds latency.

**Improvements:**
- Consider `nginx` or `traefik` as WS proxy for ttyd (native, faster)
- Or use `uvicorn --proxy-headers` with proper buffering
- Enable `SO_REUSEPORT` for multiple workers (not applicable for single-container HF)
- Profile WS message throughput under load

**Effort:** Medium-High

---

## 8. Mobile & Accessibility Support

**Current state:** xterm.js works on mobile; topbar fits; virtual keyboard triggers on input focus.

**Improvements:**
- Add touch-friendly keybar (already in webapp, port to terminal page)
- Test iOS Safari (WKWebView WS limitations)
- Add `prefers-reduced-motion` for cursor blink
- High-contrast theme toggle
- Screen reader announcements for output changes

**Effort:** Medium

---

## 9. Error Handling & Observability

**Current state:** Errors logged to `/data/logs/ttyd.log`; browser shows `[Disconnected]`.

**Improvements:**
- Structured JSON logs for ttyd (patch ttyd or wrap with logger)
- Health endpoint for ttyd: `GET /terminal/health` → proxies to ttyd
- Metrics: active WS connections, bytes in/out, session duration
- Alert on ttyd crash (supervisor restart policy)

**Effort:** Low-Medium

---

## 10. Multi-User Terminal Isolation

**Current state:** Single ttyd instance on port 7681. All users hit same terminal.

**Improvements:**
- Option A: Single ttyd + tmux per-user sessions (ttyd can spawn tmux per connection)
- Option B: ttyd per user on dynamic ports (like workspace manager does for opencode)
- Option C: ttyd with `-c` (command) that does `tmux attach -t user_${id}`

**Recommended:** Option A — ttyd's built-in tmux support (`ttyd tmux new -A -s term_${user}`) gives isolated sessions with single binary.

**Effort:** High (architectural change)

---

## 11. Shared Clipboard & File Transfer

**Current state:** No clipboard sync; no file upload/download from terminal.

**Improvements:**
- xterm.js `clipboard` addon + browser Clipboard API
- ttyd's `--writable` allows OSC 52 (clipboard) sequences
- Drag-drop file upload to terminal (base64 encode → `base64 -d > file`)
- Download files via `cat file | base64` → browser save

**Effort:** Medium

---

## 12. Terminal Themes & Customization

**Current state:** Hardcoded JetBrains Mono, dark theme.

**Improvements:**
- Theme selector (dark/light/solarized/dracula)
- Font size slider (persist in localStorage)
- Cursor style (block/underline/bar)
- Sync with webapp theme

**Effort:** Low

---

## Priority Matrix

| Improvement | Impact | Effort | Priority |
|-------------|--------|--------|----------|
| Shared tmux session | High | Medium | **P1** |
| ttyd native flags | Medium | Low | **P1** |
| Security hardening | High | Medium | **P1** |
| Reconnection handling | Medium | Medium | **P2** |
| Process isolation | Medium | Medium | **P2** |
| Mobile support | Medium | Medium | **P2** |
| Multi-user isolation | High | High | **P3** |
| Performance (nginx proxy) | Medium | High | **P3** |
| Clipboard/file transfer | Medium | Medium | **P3** |
| Themes/customization | Low | Low | **P3** |

---

## Implementation Roadmap

### Sprint 1 (P1 — Week 1-2)
- [ ] Shared tmux session between AI and terminal
- [ ] ttyd native flags (`--signal`, `--check-origin`, `--max-connection`)
- [ ] Token auth for terminal WS
- [ ] Security headers (CSP, HSTS)

### Sprint 2 (P2 — Week 3-4)
- [ ] Reconnection UI + longer ttyd timeout
- [ ] Mobile testing & fixes
- [ ] Process limits (ulimit, Docker resources)
- [ ] Observability (health endpoint, metrics)

### Sprint 3 (P3 — Week 5+)
- [ ] Multi-user terminal isolation
- [ ] Clipboard sync (OSC 52)
- [ ] Theme/customization panel
- [ ] Performance profiling & nginx proxy eval