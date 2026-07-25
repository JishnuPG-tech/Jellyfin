/* store.js — session state + localStorage UI metadata */

import { listSessions, renameSession as apiRename, deleteSessions as apiDelete } from "./http.js";

const LS_KEY = "oc_ui_meta";
const LS_LAST = "oc_last_session";

// UI-only metadata that lives in localStorage
let _meta = {};
try {
  _meta = JSON.parse(localStorage.getItem(LS_KEY) || "{}");
} catch {
  _meta = {};
}

function saveMeta() {
  localStorage.setItem(LS_KEY, JSON.stringify(_meta));
}

export function getPinnedIds() { return new Set(_meta.pinned || []); }
export function getArchivedIds() { return new Set(_meta.archived || []); }
export function getFavorites() { return new Set(_meta.favorites || []); }
export function getLastSessionId() { return _meta.lastSessionId || localStorage.getItem(LS_LAST) || null; }
export function setLastSessionId(id) { _meta.lastSessionId = id; localStorage.setItem(LS_LAST, id || ""); saveMeta(); }
export function getDrawerCollapsed() { return !!_meta.drawerCollapsed; }
export function setDrawerCollapsed(v) { _meta.drawerCollapsed = v; saveMeta(); }
export function getSelectedModel() { return _meta.selectedModel || null; }
export function setSelectedModel(v) { _meta.selectedModel = v; saveMeta(); }
export function getSelectedAgent() { return _meta.selectedAgent || null; }
export function setSelectedAgent(v) { _meta.selectedAgent = v; saveMeta(); }
export function getDraft(sessionId) { return (_meta.drafts || {})[sessionId] || ""; }
export function setDraft(sessionId, text) {
  if (!_meta.drafts) _meta.drafts = {};
  if (text) _meta.drafts[sessionId] = text;
  else delete _meta.drafts[sessionId];
  saveMeta();
}

export function togglePin(id) {
  const s = getPinnedIds();
  if (s.has(id)) s.delete(id); else s.add(id);
  _meta.pinned = [...s];
  saveMeta();
  return s.has(id);
}

export function toggleArchive(id) {
  const s = getArchivedIds();
  if (s.has(id)) s.delete(id); else s.add(id);
  _meta.archived = [...s];
  saveMeta();
  return s.has(id);
}

export function toggleFavorite(id) {
  const s = getFavorites();
  if (s.has(id)) s.delete(id); else s.add(id);
  _meta.favorites = [...s];
  saveMeta();
  return s.has(id);
}

// Live session list
let _sessions = [];
let _listeners = new Set();
let _loading = false;
let _error = null;

export function getSessions() { return _sessions; }
export function subscribe(fn) { _listeners.add(fn); return () => _listeners.delete(fn); }
function emit() { for (const fn of _listeners) fn(_sessions); }

export function getFilteredSessions({ search = "", showArchived = false } = {}) {
  const pinned = getPinnedIds();
  const archived = getArchivedIds();
  let list = _sessions;

  if (!showArchived) {
    list = list.filter(s => !archived.has(s.id));
  }

  if (search) {
    const q = search.toLowerCase();
    list = list.filter(s => (s.title || "").toLowerCase().includes(q));
  }

  // Sort: pinned first, then by updated time
  list = [...list].sort((a, b) => {
    const pa = pinned.has(a.id) ? 1 : 0;
    const pb = pinned.has(b.id) ? 1 : 0;
    if (pa !== pb) return pb - pa;
    const ua = (a.time && a.time.updated) || 0;
    const ub = (b.time && b.time.updated) || 0;
    return ub - ua;
  });

  return list;
}

export async function refreshSessions() {
  _loading = true;
  _error = null;
  try {
    _sessions = await listSessions();
  } catch (e) {
    _error = e;
  }
  _loading = false;
  emit();
  return _sessions;
}

export async function renameSession(id, title) {
  await apiRename(id, title);
  const s = _sessions.find(x => x.id === id);
  if (s) s.title = title;
  emit();
}

export async function deleteSession(id) {
  await apiDelete([id]);
  _sessions = _sessions.filter(s => s.id !== id);
  emit();
}

export function upsertSessionFromEvent(evt) {
  if (!evt || !evt.data) return;
  const d = evt.data;
  const existing = _sessions.find(s => s.id === d.id);
  if (existing) {
    Object.assign(existing, d);
  } else {
    _sessions.unshift(d);
  }
  emit();
}

export function updateSessionFromEvent(evt) {
  if (!evt || !evt.data) return;
  const id = (evt.data && evt.data.id) || (evt.aggregateID);
  if (!id) return;
  const s = _sessions.find(x => x.id === id);
  if (s) {
    // Merge data from event (session.updated events carry full info)
    const info = evt.data.info || evt.data;
    if (info) Object.assign(s, info);
  }
  emit();
}

// Title auto-generation
let _titleTimers = {};
export function scheduleTitleGeneration(sessionId, userMessage) {
  if (_titleTimers[sessionId]) return;
  // Fire-and-forget: send a small message to trigger title, then check back
  // Actually we just let opencode's own title system handle this.
  // If the title is still "New session", we can let the user rename.
}
