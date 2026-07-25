/* http.js — thin fetch helpers around opencode's API */

const H = {
  json: { "content-type": "application/json", accept: "application/json" },
};

export async function api(path, opts = {}) {
  const r = await fetch(path, {
    method: opts.method || "GET",
    headers: { ...H.json, ...(opts.headers || {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
  });
  const text = await r.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  return { ok: r.ok, status: r.status, data, headers: r.headers };
}

export async function listSessions(workspace) {
  const q = workspace ? `?directory=${encodeURIComponent(workspace)}` : "";
  const r = await api(`/api/session${q}`);
  return r.ok ? (r.data && r.data.data) || [] : [];
}

export async function getSession(id) {
  const r = await api(`/api/session/${encodeURIComponent(id)}`);
  return r.ok ? r.data : null;
}

export async function createSession(body = {}) {
  const r = await api("/api/session", { method: "POST", body });
  return r.ok ? r.data : null;
}

export async function deleteSessions(ids) {
  if (!ids || !ids.length) return { ok: true };
  const r = await api("/api/session/delete", { method: "POST", body: { ids } });
  return r;
}

export async function renameSession(id, title) {
  try {
    const r = await api(`/api/session/${id}`, {
      method: "PATCH",
      body: { title },
    });
    if (r.ok) return r.data;
  } catch {}
  // Fallback for older opencode builds that use body title
  const r = await api(`/api/session/${id}`, {
    method: "PATCH",
    body: { title },
  });
  return r.ok ? r.data : null;
}

export async function shareSession(id) {
  const r = await api(`/api/session/${id}/share`, { method: "POST" });
  return r.ok ? r.data : null;
}

export async function initSession(id) {
  const r = await api(`/api/session/${id}/init`, {
    method: "POST",
    body: { messageID: "" },
  });
  return r.ok ? r.data : null;
}

export async function getMessages(id, cursor, signal) {
  const q = new URLSearchParams();
  if (cursor) q.set("cursor", cursor);
  const qstr = q.toString();
  const r = await fetch(`/api/session/${encodeURIComponent(id)}/message${qstr ? "?" + qstr : ""}`, {
    headers: { accept: "application/json" },
    signal,
  });
  if (!r.ok) return { messages: [], hasMore: false };
  const json = await r.json();
  return {
    messages: (json && json.data) || [],
    cursor: json && json.cursor,
  };
}

export async function sendMessage(id, parts, opts = {}) {
  return api(`/api/session/${encodeURIComponent(id)}/message`, {
    method: "POST",
    body: { parts, agent: opts.agent || "build" },
    signal: opts.signal,
  });
}

export async function sessionStatus(id) {
  const r = await api(`/api/session/${encodeURIComponent(id)}/status`);
  return r.ok ? r.data : null;
}

export async function abortSession(id) {
  return api(`/api/session/${encodeURIComponent(id)}/abort`, {
    method: "POST",
  });
}

export async function abortSessionStatus(id) {
  return api(`/api/session/${encodeURIComponent(id)}/abort`, {
    method: "POST",
    body: {},
  });
}

export async function listProviders() {
  const r = await api("/api/provider");
  return r.ok ? (r.data && r.data.data) || [] : [];
}

export async function listAgents() {
  const r = await api("/api/agent");
  return r.ok ? (r.data && r.data.data) || [] : [];
}

export function openEventStream(onEvent, onError, signal) {
  // opencode's SSE endpoint is /api/event; it streams session lifecycle and
  // message progress events. We keep the connection alive and rebind handlers
  // on disconnect so transient network blips do not break the UI.
  let es = null;
  let retry = 0;
  let stopped = false;

  async function connect() {
    if (stopped) return;
    try {
      es = new EventSource("/api/event");
      es.onmessage = (e) => {
        retry = 0;
        try {
          const data = JSON.parse(e.data);
          onEvent && onEvent(data);
        } catch (err) {
          onError && onError(err);
        }
      };
      es.onerror = () => {
        if (es) {
          es.close();
          es = null;
        }
        if (stopped) return;
        retry = Math.min(retry + 1, 6);
        const delay = 500 * Math.pow(2, retry);
        setTimeout(connect, delay);
        onError && onError(new Error("sse disconnected"));
      };
    } catch (err) {
      onError && onError(err);
      setTimeout(connect, 1000);
    }
  }

  connect();

  return {
    close() {
      stopped = true;
      if (es) {
        es.close();
        es = null;
      }
    },
  };
}

export function partText(p) {
  if (!p) return "";
  if (p.type === "text") return p.text || "";
  if (p.type === "reasoning") return p.text || "";
  return "";
}
