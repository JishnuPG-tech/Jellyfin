/* components.js — sidebar + chat shell. Everything lives here. */

import {
  listSessions, getSession, createSession, deleteSessions, renameSession,
  getMessages, sendMessage, sessionStatus, abortSession,
  listProviders, listAgents, openEventStream, partText,
} from "./http.js";
import {
  h, clear, debounce, formatTimeAgo, groupByDate, setOverlay, openOverlay,
  promptInput, confirmAction, snackbar,
} from "./ui.js";
import {
  getPinnedIds, getArchivedIds, getLastSessionId, setLastSessionId,
  getDrawerCollapsed, setDrawerCollapsed,
  getSelectedModel, setSelectedModel, getSelectedAgent, setSelectedAgent,
  getDraft, setDraft, togglePin, toggleArchive,
  refreshSessions, getFilteredSessions, subscribe,
  upsertSessionFromEvent, updateSessionFromEvent,
} from "./store.js";

// ─── state ───
let _currentSessionId = null;
let _abortCtrl = null;
let _sidebarOpen = window.innerWidth > 860;
let _search = "";
let _showArchived = false;
let _sending = false;
let _agents = [];
let _providers = [];
let _currentAgent = null;
let _currentModel = null;
let _sse = null;

// ─── root DOM references (created in mount) ───
let _root;
let _sidebar;
let _chat;
let _msgContainer;
let _inputBox;
let _titleEl;

// ─── sidebar ───
function renderSidebar() {
  clear(_sidebar);
  const collapsed = getDrawerCollapsed();
  _sidebar.style.width = collapsed ? "0px" : "";
  _sidebar.style.minWidth = collapsed ? "0px" : "";
  _sidebar.style.overflow = collapsed ? "hidden" : "";
  _sidebar.style.padding = collapsed ? "0" : "";

  if (collapsed) {
    const btn = h("button", { title: "Open sidebar", style: { padding: "10px", fontSize: "18px", color: "var(--fg-2)" }, text: "\u2630" });
    btn.addEventListener("click", () => { setDrawerCollapsed(false); renderSidebar(); });
    _sidebar.appendChild(btn);
    return;
  }

  // Header
  const header = h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "8px 12px", borderBottom: "1px solid var(--border)" } });
  const collapseBtn = h("button", { title: "Collapse sidebar", style: { padding: "6px 10px", color: "var(--fg-3)", fontSize: "14px" }, text: "\u25c0" });
  collapseBtn.addEventListener("click", () => { setDrawerCollapsed(true); renderSidebar(); });
  const newBtn = h("button", { style: { padding: "6px 14px", background: "var(--accent)", color: "#0a0a0a", borderRadius: "6px", fontWeight: "600", fontSize: "13px" }, text: "New Chat" });
  newBtn.addEventListener("click", newChat);
  header.append(collapseBtn, h("div", { style: { flex: "1" } }), newBtn);
  _sidebar.appendChild(header);

  // Search
  const searchWrap = h("div", { style: { padding: "10px 12px 6px" } });
  const searchInput = h("input", {
    placeholder: "Search chats...",
    value: _search,
    style: { width: "100%", padding: "8px 10px", background: "var(--bg-2)", border: "1px solid var(--border)", borderRadius: "6px", fontSize: "13px" },
  });
  searchInput.addEventListener("input", (e) => { _search = e.target.value; renderSessionList(); });
  searchWrap.appendChild(searchInput);
  _sidebar.appendChild(searchWrap);

  // Toggle archived
  const toggleRow = h("div", { style: { padding: "4px 12px", display: "flex", gap: "8px" } });
  const archiveBtn = h("button", {
    style: { padding: "4px 10px", fontSize: "12px", borderRadius: "4px", color: _showArchived ? "var(--accent)" : "var(--fg-3)", border: _showArchived ? "1px solid var(--accent)" : "1px solid var(--border)" },
    text: "Archived",
  });
  archiveBtn.addEventListener("click", () => { _showArchived = !_showArchived; renderSidebar(); });
  toggleRow.appendChild(archiveBtn);
  _sidebar.appendChild(toggleRow);

  // Session list
  const listWrap = h("div", { style: { flex: "1", overflow: "auto", padding: "0 6px" } });
  _sidebar.appendChild(listWrap);

  // Re-render just the list part
  _renderSessionListInto(listWrap);
}

function renderSessionList() {
  // Find the list container and re-render
  if (!_sidebar) return;
  const listWrap = _sidebar.querySelector("div:last-child");
  if (!listWrap) return;
  _renderSessionListInto(listWrap);
}

function _renderSessionListInto(container) {
  clear(container);
  const sessions = getFilteredSessions({ search: _search, showArchived: _showArchived });
  if (!sessions.length) {
    container.appendChild(h("div", { style: { padding: "24px 12px", textAlign: "center", color: "var(--fg-3)", fontSize: "13px" }, text: _search ? "No matches" : "No chats yet" }));
    return;
  }

  const groups = groupByDate(sessions);
  for (const [label, items] of Object.entries(groups)) {
    if (!items.length) continue;
    const section = h("div", { style: { marginBottom: "8px" } });
    const heading = h("div", { style: { padding: "8px 12px 4px", fontSize: "11px", fontWeight: "600", color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.05em" }, text: label });
    section.appendChild(heading);
    for (const s of items) {
      section.appendChild(renderSessionItem(s));
    }
    container.appendChild(section);
  }
}

function renderSessionItem(s) {
  const pinned = getPinnedIds().has(s.id);
  const active = s.id === _currentSessionId;
  const archived = getArchivedIds().has(s.id);
  const preview = s.__preview || s.title || "New chat";
  const modelLabel = (s.model && s.model.id) ? s.model.id.split("/").pop() : "";

  const row = h("div", {
    style: {
      padding: "10px 12px",
      borderRadius: "6px",
      cursor: "pointer",
      background: active ? "var(--bg-3)" : "transparent",
      marginBottom: "2px",
      position: "relative",
    },
    "data-sid": s.id,
  });
  row.addEventListener("click", () => openSession(s.id));

  // Hover effects
  row.addEventListener("mouseenter", () => { if (!active) row.style.background = "var(--bg-2)"; });
  row.addEventListener("mouseleave", () => { if (!active) row.style.background = "transparent"; });

  const titleRow = h("div", { style: { display: "flex", alignItems: "center", gap: "6px" } });
  if (pinned) titleRow.appendChild(h("span", { style: { fontSize: "11px", color: "var(--accent)" }, text: "\u{1F4CC}" }));
  titleRow.appendChild(h("span", {
    style: { flex: "1", fontSize: "13px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontWeight: active ? "600" : "400" },
    text: preview,
  }));

  // Actions menu button (on hover)
  const actionsWrap = h("div", { style: { display: "flex", gap: "2px", opacity: "0", transition: "opacity .12s" } });
  row.addEventListener("mouseenter", () => { actionsWrap.style.opacity = "1"; });
  row.addEventListener("mouseleave", () => { actionsWrap.style.opacity = "0"; });

  const pinBtn = h("button", { title: pinned ? "Unpin" : "Pin", style: { padding: "2px 5px", fontSize: "12px", color: "var(--fg-3)" }, text: pinned ? "\u{1F4CC}" : "\u{1F4CE}" });
  pinBtn.addEventListener("click", (e) => { e.stopPropagation(); togglePin(s.id); renderSessionList(); });
  const renameBtn = h("button", { title: "Rename", style: { padding: "2px 5px", fontSize: "12px", color: "var(--fg-3)" }, text: "\u270F\uFE0F" });
  renameBtn.addEventListener("click", (e) => { e.stopPropagation(); renameSessionAction(s); });
  const archiveBtn2 = h("button", { title: archived ? "Restore" : "Archive", style: { padding: "2px 5px", fontSize: "12px", color: "var(--fg-3)" }, text: archived ? "\u21A9\uFE0F" : "\u{1F4E6}" });
  archiveBtn2.addEventListener("click", (e) => { e.stopPropagation(); toggleArchive(s.id); renderSessionList(); });
  const deleteBtn = h("button", { title: "Delete", style: { padding: "2px 5px", fontSize: "12px", color: "var(--danger)" }, text: "\u{1F5D1}\uFE0F" });
  deleteBtn.addEventListener("click", (e) => { e.stopPropagation(); deleteSessionAction(s); });
  actionsWrap.append(pinBtn, renameBtn, archiveBtn2, deleteBtn);

  titleRow.appendChild(actionsWrap);
  row.appendChild(titleRow);

  // Meta line
  const meta = h("div", { style: { display: "flex", alignItems: "center", gap: "6px", marginTop: "3px", fontSize: "11px", color: "var(--fg-3)" } });
  meta.appendChild(h("span", { text: formatTimeAgo(s.time && s.time.updated) }));
  if (modelLabel) meta.appendChild(h("span", { text: modelLabel, style: { padding: "1px 5px", background: "var(--bg-3)", borderRadius: "3px" } }));
  row.appendChild(meta);

  return row;
}

// ─── session actions ───
async function newChat() {
  try {
    const s = await createSession();
    const id = s && s.id;
    if (id) {
      await openSession(id);
      renderSidebar();
    }
  } catch (e) {
    snackbar("Failed to create chat", "error");
  }
}

async function openSession(id) {
  if (_abortCtrl) { _abortCtrl.abort(); _abortCtrl = null; }
  _sending = false;
  _currentSessionId = id;
  setLastSessionId(id);
  renderSidebar();
  renderChat();
  // Load messages
  await loadMessages(id);
}

async function loadMessages(id) {
  if (!_msgContainer) return;
  clear(_msgContainer);
  _msgContainer.appendChild(h("div", { class: "skeleton", style: { height: "60px", marginBottom: "8px" } }));
  _msgContainer.appendChild(h("div", { class: "skeleton", style: { height: "40px", width: "70%", marginBottom: "8px" } }));

  try {
    const { messages } = await getMessages(id);
    clear(_msgContainer);
    if (!messages.length) {
      _msgContainer.appendChild(h("div", {
        style: { padding: "40px 20px", textAlign: "center", color: "var(--fg-3)" },
        html: '<div style="font-size:36px;margin-bottom:12px">&#x1F4AC;</div><div style="font-size:14px">Send a message to start the conversation</div>',
      }));
      return;
    }
    for (const m of messages) {
      renderMessage(m);
    }
    _msgContainer.scrollTop = _msgContainer.scrollHeight;
  } catch (e) {
    clear(_msgContainer);
    _msgContainer.appendChild(h("div", { style: { padding: "40px", textAlign: "center", color: "var(--danger)" }, text: "Failed to load messages" }));
  }
}

function renderMessage(m) {
  const role = (m.role || "user").toLowerCase();
  const isUser = role === "user";
  const wrap = h("div", {
    style: {
      padding: "16px 20px",
      borderBottom: "1px solid var(--border)",
      background: isUser ? "transparent" : "var(--bg-1)",
      maxWidth: "100%",
    },
  });
  const label = h("div", {
    style: { display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px", fontSize: "12px", fontWeight: "600", color: isUser ? "var(--fg-2)" : "var(--accent)" },
    text: isUser ? "You" : "Assistant",
  });
  wrap.appendChild(label);

  const content = h("div", {
    style: { fontSize: "14px", lineHeight: "1.7", whiteSpace: "pre-wrap", wordBreak: "break-word" },
  });

  // Render parts
  if (m.parts && m.parts.length) {
    for (const p of m.parts) {
      if (p.type === "text") {
        content.appendChild(h("div", { text: p.text || "" }));
      } else if (p.type === "reasoning") {
        // Collapsible reasoning
        const details = h("details", { style: { marginBottom: "8px", color: "var(--fg-3)", fontSize: "12px" } });
        details.appendChild(h("summary", { text: "Reasoning", style: { cursor: "pointer", marginBottom: "4px" } }));
        details.appendChild(h("div", { text: p.text || "", style: { whiteSpace: "pre-wrap", padding: "8px", background: "var(--bg-2)", borderRadius: "4px" } }));
        content.appendChild(details);
      } else if (p.type === "tool-invocation") {
        const toolWrap = h("div", { style: { margin: "8px 0", padding: "8px 12px", background: "var(--bg-2)", borderRadius: "6px", border: "1px solid var(--border)", fontSize: "12px" } });
        toolWrap.appendChild(h("div", { style: { fontWeight: "600", marginBottom: "4px", color: "var(--fg-2)" }, text: p.toolInvocation && p.toolInvocation.toolName ? p.toolInvocation.toolName : "Tool" }));
        if (p.toolInvocation && p.toolInvocation.state === "result") {
          toolWrap.appendChild(h("div", { text: String((p.toolInvocation.result || "").slice(0, 500)), style: { color: "var(--fg-3)" } }));
        }
        content.appendChild(toolWrap);
      }
    }
  } else if (m.content) {
    // Fallback: plain content string
    if (typeof m.content === "string") {
      content.appendChild(h("div", { text: m.content }));
    } else if (Array.isArray(m.content)) {
      for (const part of m.content) {
        if (typeof part === "string") content.appendChild(h("div", { text: part }));
        else if (part && part.text) content.appendChild(h("div", { text: part.text }));
      }
    }
  }
  wrap.appendChild(content);
  _msgContainer.appendChild(wrap);
}

// ─── chat view ───
function renderChat() {
  clear(_chat);
  if (!_currentSessionId) {
    _chat.appendChild(h("div", {
      style: { display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "var(--fg-3)" },
      html: '<div style="text-align:center"><div style="font-size:48px;margin-bottom:16px">&#x1F44B;</div><div style="font-size:16px;margin-bottom:8px">Welcome to OpenCode</div><div style="font-size:13px">Select a chat or start a new one</div></div>',
    }));
    return;
  }

  // Header
  const hdr = h("div", { style: { display: "flex", alignItems: "center", gap: "10px", padding: "10px 16px", borderBottom: "1px solid var(--border)", background: "var(--bg-1)" } });
  const menuBtn = h("button", { title: "Toggle sidebar", style: { padding: "6px 10px", color: "var(--fg-2)", fontSize: "16px" }, text: "\u2630" });
  menuBtn.addEventListener("click", () => { _sidebarOpen = !_sidebarOpen; _sidebar.style.display = _sidebarOpen ? "" : "none"; });
  _titleEl = h("span", { style: { flex: "1", fontSize: "14px", fontWeight: "600", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }, text: "Loading..." });
  const statusEl = h("span", { style: { fontSize: "11px", color: "var(--fg-3)" } });
  const abortBtn = h("button", { title: "Stop generating", style: { padding: "4px 10px", borderRadius: "4px", background: "var(--danger)", color: "#fff", fontSize: "12px", display: "none" }, text: "Stop" });
  abortBtn.addEventListener("click", () => { if (_abortCtrl) _abortCtrl.abort(); });
  hdr.append(menuBtn, _titleEl, statusEl, abortBtn);
  _chat.appendChild(hdr);

  // Messages container
  _msgContainer = h("div", { style: { flex: "1", overflow: "auto" } });
  _chat.appendChild(_msgContainer);

  // Input area
  const inputArea = h("div", { style: { padding: "12px 16px", borderTop: "1px solid var(--border)", background: "var(--bg-1)" } });

  // Model/Agent selector row
  const selectorRow = h("div", { style: { display: "flex", gap: "8px", marginBottom: "8px", alignItems: "center" } });
  const agentSelect = h("select", { style: { padding: "4px 8px", background: "var(--bg-2)", border: "1px solid var(--border)", borderRadius: "4px", fontSize: "12px", color: "var(--fg-2)" } });
  agentSelect.appendChild(h("option", { value: "", text: "Agent: build" }));
  for (const a of _agents) {
    const opt = h("option", { value: a.id || a, text: a.id || a });
    if ((getSelectedAgent() || "build") === (a.id || a)) opt.selected = true;
    agentSelect.appendChild(opt);
  }
  agentSelect.addEventListener("change", () => setSelectedAgent(agentSelect.value));
  selectorRow.appendChild(agentSelect);
  inputArea.appendChild(selectorRow);

  // Text input
  const inputWrap = h("div", { style: { display: "flex", gap: "8px", alignItems: "flex-end" } });
  _inputBox = h("textarea", {
    placeholder: "Message...",
    rows: "1",
    style: { flex: "1", padding: "10px 14px", background: "var(--bg-2)", border: "1px solid var(--border)", borderRadius: "8px", fontSize: "14px", resize: "none", maxHeight: "180px", lineHeight: "1.5", minHeight: "42px" },
  });

  // Auto-resize textarea
  _inputBox.addEventListener("input", () => {
    _inputBox.style.height = "auto";
    _inputBox.style.height = Math.min(_inputBox.scrollHeight, 180) + "px";
  });

  // Ctrl+Enter to send
  _inputBox.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submitMessage();
    }
  });

  // Restore draft
  if (_currentSessionId) {
    const draft = getDraft(_currentSessionId);
    if (draft) _inputBox.value = draft;
  }

  // Save draft on input
  _inputBox.addEventListener("input", () => {
    if (_currentSessionId) setDraft(_currentSessionId, _inputBox.value);
  });

  const sendBtn = h("button", { title: "Send (Enter)", style: { padding: "10px 16px", background: "var(--accent)", color: "#0a0a0a", borderRadius: "8px", fontWeight: "600", fontSize: "14px", height: "42px" }, text: "Send" });
  sendBtn.addEventListener("click", submitMessage);

  inputWrap.append(_inputBox, sendBtn);
  inputArea.appendChild(inputWrap);
  _chat.appendChild(inputArea);

  // Load title from session data
  loadSessionTitle();
}

async function loadSessionTitle() {
  if (!_currentSessionId || !_titleEl) return;
  try {
    const s = await getSession(_currentSessionId);
    if (s) {
      _titleEl.textContent = s.title || "Untitled";
      // Update sidebar to reflect new title
      renderSessionList();
    }
  } catch {
    _titleEl.textContent = "Chat";
  }
}

async function submitMessage() {
  if (!_inputBox || !_currentSessionId || _sending) return;
  const text = _inputBox.value.trim();
  if (!text) return;

  _sending = true;
  const agent = getSelectedAgent() || "build";
  _inputBox.value = "";
  setDraft(_currentSessionId, "");

  // Append user message optimistically
  renderMessage({ role: "user", parts: [{ type: "text", text }], content: text });
  _msgContainer.scrollTop = _msgContainer.scrollHeight;

  // Create assistant placeholder
  const assistantEl = h("div", {
    style: { padding: "16px 20px", borderBottom: "1px solid var(--border)", background: "var(--bg-1)" },
  });
  assistantEl.appendChild(h("div", { style: { display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px", fontSize: "12px", fontWeight: "600", color: "var(--accent)" }, text: "Assistant" }));
  const contentEl = h("div", { style: { fontSize: "14px", lineHeight: "1.7", whiteSpace: "pre-wrap", wordBreak: "break-word", color: "var(--fg-3)" }, text: "Thinking..." });
  assistantEl.appendChild(contentEl);
  _msgContainer.appendChild(assistantEl);
  _msgContainer.scrollTop = _msgContainer.scrollHeight;

  // Find abort button and show it
  const abortBtn = _chat.querySelector("button[title='Stop generating']");
  if (abortBtn) abortBtn.style.display = "";

  _abortCtrl = new AbortController();

  try {
    const parts = [{ type: "text", text }];
    const resp = await sendMessage(_currentSessionId, parts, { agent, signal: _abortCtrl.signal });
    if (resp && resp.ok && resp.data) {
      // Render full response from resp.data
      clear(contentEl);
      const data = resp.data;
      if (data.parts && data.parts.length) {
        for (const p of data.parts) {
          if (p.type === "text") contentEl.appendChild(h("div", { text: p.text || "" }));
          else if (p.type === "reasoning") {
            const d = h("details", { style: { marginBottom: "8px", color: "var(--fg-3)", fontSize: "12px" } });
            d.appendChild(h("summary", { text: "Reasoning", style: { cursor: "pointer", marginBottom: "4px" } }));
            d.appendChild(h("div", { text: p.text || "", style: { whiteSpace: "pre-wrap", padding: "8px", background: "var(--bg-2)", borderRadius: "4px" } }));
            contentEl.appendChild(d);
          }
        }
      } else if (data.content) {
        if (typeof data.content === "string") contentEl.textContent = data.content;
        else contentEl.textContent = JSON.stringify(data.content);
      }
      contentEl.style.color = "";
    } else if (resp && resp.data) {
      // Error or unexpected format
      clear(contentEl);
      contentEl.textContent = typeof resp.data === "string" ? resp.data : JSON.stringify(resp.data);
      contentEl.style.color = "var(--danger)";
    }
  } catch (e) {
    if (e.name === "AbortError") {
      clear(contentEl);
      contentEl.textContent = "(stopped)";
      contentEl.style.color = "var(--fg-3)";
    } else {
      clear(contentEl);
      contentEl.textContent = "Error: " + (e.message || "Unknown error");
      contentEl.style.color = "var(--danger)";
    }
  } finally {
    _sending = false;
    _abortCtrl = null;
    if (abortBtn) abortBtn.style.display = "none";
    _msgContainer.scrollTop = _msgContainer.scrollHeight;
    // Refresh title in sidebar
    renderSessionList();
  }
}

// ─── session actions ───
async function renameSessionAction(s) {
  const title = await promptInput("Rename chat", "Enter new title", s.title || "");
  if (title && title !== s.title) {
    try {
      await renameSession(s.id, title);
      renderSessionList();
      if (s.id === _currentSessionId && _titleEl) _titleEl.textContent = title;
      snackbar("Renamed");
    } catch (e) {
      snackbar("Rename failed", "error");
    }
  }
}

async function deleteSessionAction(s) {
  const ok = await confirmAction("Delete chat?", `Delete "${s.title || "Untitled"}"? This cannot be undone.`, { okText: "Delete", danger: true });
  if (!ok) return;
  try {
    await deleteSessions([s.id]);
    snackbar("Deleted");
    if (_currentSessionId === s.id) {
      _currentSessionId = null;
      setLastSessionId(null);
      renderChat();
    }
    renderSessionList();
  } catch (e) {
    snackbar("Delete failed", "error");
  }
}

// ─── SSE ───
function startSSE() {
  if (_sse) _sse.close();
  _sse = openEventStream((evt) => {
    const t = evt && evt.type;
    if (t === "session.updated" || t === "session.created") {
      updateSessionFromEvent(evt);
      // Reload title if it's current session
      if (_currentSessionId && evt.data && ((evt.data.id || evt.aggregateID) === _currentSessionId)) {
        loadSessionTitle();
      }
    } else if (t === "session.deleted") {
      const id = evt.data && evt.data.id;
      if (id) {
        const sessions = getFilteredSessions();
        // Just refresh
        refreshSessions().then(() => renderSessionList());
      }
    } else if (t === "message.updated" || t === "message.part.updated") {
      // Could stream partial messages, but for now we do full response
    }
  }, (err) => {
    // SSE error — will auto-retry inside openEventStream
  });
}

// ─── boot ───
export async function mount() {
  _root = document.getElementById("root");
  clear(_root);

  // Add overlay and snackbar containers
  const overlayHost = h("div", { id: "overlay", style: { display: "none" } });
  const snackHost = h("div", { id: "snackbars", style: { position: "fixed", bottom: "16px", right: "16px", zIndex: "9999", display: "flex", flexDirection: "column", gap: "8px" } });

  // Layout
  const layout = h("div", { style: { display: "flex", height: "100vh", width: "100vw" } });

  // Sidebar
  _sidebar = h("div", {
    id: "sidebar",
    style: {
      width: "280px",
      minWidth: "280px",
      background: "var(--bg)",
      borderRight: "1px solid var(--border)",
      display: "flex",
      flexDirection: "column",
      overflow: "hidden",
      transition: "width .15s",
    },
  });

  // Main chat
  _chat = h("div", { id: "chat", style: { flex: "1", display: "flex", flexDirection: "column", minWidth: "0" } });

  layout.append(_sidebar, _chat);
  _root.append(layout, overlayHost, snackHost);

  // Load initial data
  await refreshSessions();
  renderSidebar();
  renderChat();

  // Open last session or show welcome
  const lastId = getLastSessionId();
  const sessions = getFilteredSessions();
  if (lastId && sessions.find(s => s.id === lastId)) {
    await openSession(lastId);
  } else if (sessions.length) {
    // Auto-open the most recent
    await openSession(sessions[0].id);
  }

  // Start SSE
  startSSE();

  // Periodic refresh (every 30s) to catch any missed updates
  setInterval(async () => {
    await refreshSessions();
    renderSessionList();
  }, 30000);

  // Subscribe to store changes
  subscribe(() => {
    renderSessionList();
  });
}
