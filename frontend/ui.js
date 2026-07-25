/* ui.js — lightweight DOM helpers. No external runtime. */

export function h(tag, props = {}, children = []) {
  const el = document.createElement(tag);
  for (const k in props) {
    const v = props[k];
    if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on") && typeof v === "function")
      el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v != null) el.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    el.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

export function el(id) {
  return document.getElementById(id);
}

let _uid = 0;
export function uid(prefix = "id") {
  _uid += 1;
  return `${prefix}-${_uid}`;
}

export function debounce(fn, ms = 200) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

export function formatTimeAgo(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  const now = Date.now();
  const diff = now - ts;
  const s = Math.floor(diff / 1000);
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  const day = Math.floor(h / 24);
  if (day === 0) return "today";
  if (day === 1) return "yesterday";
  if (day < 7) return `${day}d`;
  if (day < 30) return `${Math.floor(day / 7)}w`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function groupByDate(sessions) {
  const now = Date.now();
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const yesterday = today.getTime() - 86400000;
  const week = today.getTime() - 7 * 86400000;
  const month = today.getTime() - 30 * 86400000;

  const groups = { Today: [], Yesterday: [], "Previous 7 Days": [], "Previous 30 Days": [], Older: [] };
  for (const s of sessions) {
    const updated = s.time && s.time.updated ? s.time.updated : 0;
    if (updated >= today.getTime()) groups.Today.push(s);
    else if (updated >= yesterday) groups.Yesterday.push(s);
    else if (updated >= week) groups["Previous 7 Days"].push(s);
    else if (updated >= month) groups["Previous 30 Days"].push(s);
    else groups.Older.push(s);
  }
  for (const k in groups) {
    groups[k].sort((a, b) => {
      const ua = a.time && a.time.updated ? a.time.updated : 0;
      const ub = b.time && b.time.updated ? b.time.updated : 0;
      return ub - ua;
    });
  }
  return groups;
}

export function pickPreview(s) {
  // The session list itself rarely carries last-message content; we use title
  // as a fallback preview. When caller already has a preview, prefer that.
  return s.__preview || s.title || "New chat";
}

export function setOverlay(node) {
  const host = document.getElementById("overlay");
  if (!host) return;
  while (host.firstChild) host.removeChild(host.firstChild);
  host.appendChild(node || document.createTextNode(""));
  host.style.display = node ? "flex" : "none";
}

export function openOverlay(build, onClose) {
  const host = document.createElement("div");
  host.className = "modal-backdrop fade-in";
  Object.assign(host.style, {
    position: "fixed",
    inset: "0",
    background: "rgba(0,0,0,0.55)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: "9999",
  });
  const close = () => {
    document.body.removeChild(host);
    document.removeEventListener("keydown", esc);
    onClose && onClose();
  };
  const esc = (e) => {
    if (e.key === "Escape") close();
  };
  host.addEventListener("click", (e) => {
    if (e.target === host) close();
  });
  document.addEventListener("keydown", esc);
  document.body.appendChild(host);
  build(host, close);
  return close;
}

export function snackbar(text, kind = "info") {
  const host = document.getElementById("snackbars") || (() => {
    const n = document.createElement("div");
    n.id = "snackbars";
    Object.assign(n.style, {
      position: "fixed",
      bottom: "16px",
      right: "16px",
      zIndex: "9999",
      display: "flex",
      flexDirection: "column",
      gap: "8px",
    });
    document.body.appendChild(n);
    return n;
  })();
  const node = document.createElement("div");
  node.textContent = text;
  Object.assign(node.style, {
    padding: "8px 14px",
    background: kind === "error" ? "#3a1a1a" : kind === "warn" ? "#3a2e1a" : "#1a2a23",
    border: `1px solid ${kind === "error" ? "#5a2a2a" : kind === "warn" ? "#5a4a1a" : "#1f3a2c"}`,
    borderRadius: "6px",
    color: "#ececec",
    fontSize: "13px",
    boxShadow: "var(--shadow)",
    animation: "fade .2s ease-out",
  });
  host.appendChild(node);
  setTimeout(() => {
    node.style.opacity = "0";
    setTimeout(() => node.remove(), 300);
  }, 2600);
}

export function promptInput(title, placeholder = "", initial = "") {
  return new Promise((resolve) => {
    openOverlay((host, close) => {
      const wrap = document.createElement("div");
      Object.assign(wrap.style, {
        background: "#171717",
        border: "1px solid var(--border-hi)",
        borderRadius: "10px",
        padding: "20px",
        width: "420px",
        maxWidth: "90vw",
        display: "flex",
        flexDirection: "column",
        gap: "12px",
        boxShadow: "var(--shadow)",
      });
      const t = document.createElement("div");
      t.textContent = title;
      t.style.fontSize = "15px";
      t.style.fontWeight = "600";
      wrap.appendChild(t);
      const input = document.createElement("input");
      input.value = initial || "";
      input.placeholder = placeholder;
      Object.assign(input.style, {
        padding: "10px 12px",
        background: "#0a0a0a",
        border: "1px solid var(--border)",
        borderRadius: "6px",
        fontSize: "14px",
        width: "100%",
      });
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          close();
          resolve(input.value.trim() || null);
        }
        if (e.key === "Escape") {
          close();
          resolve(null);
        }
      });
      wrap.appendChild(input);
      const row = document.createElement("div");
      Object.assign(row.style, { display: "flex", justifyContent: "flex-end", gap: "8px" });
      const cancel = document.createElement("button");
      cancel.textContent = "Cancel";
      Object.assign(cancel.style, {
        padding: "8px 14px",
        borderRadius: "6px",
        color: "var(--fg-2)",
      });
      cancel.addEventListener("click", () => { close(); resolve(null); });
      const ok = document.createElement("button");
      ok.textContent = "OK";
      ok.className = "btn-primary";
      Object.assign(ok.style, {
        padding: "8px 14px",
        borderRadius: "6px",
        background: "var(--accent)",
        color: "#0a0a0a",
        fontWeight: "600",
      });
      ok.addEventListener("click", () => { close(); resolve(input.value.trim() || null); });
      row.append(cancel, ok);
      wrap.appendChild(row);
      host.appendChild(wrap);
      setTimeout(() => input.focus(), 50);
    });
  });
}

export function confirmAction(title, body, opts = {}) {
  return new Promise((resolve) => {
    openOverlay((host, close) => {
      const wrap = document.createElement("div");
      Object.assign(wrap.style, {
        background: "#171717",
        border: "1px solid var(--border-hi)",
        borderRadius: "10px",
        padding: "20px",
        width: "420px",
        maxWidth: "90vw",
        display: "flex",
        flexDirection: "column",
        gap: "12px",
      });
      const t = document.createElement("div");
      t.textContent = title;
      t.style.fontSize = "15px";
      t.style.fontWeight = "600";
      wrap.appendChild(t);
      const p = document.createElement("div");
      p.textContent = body;
      Object.assign(p.style, { color: "var(--fg-2)", fontSize: "13px", lineHeight: "1.5" });
      wrap.appendChild(p);
      const row = document.createElement("div");
      Object.assign(row.style, { display: "flex", justifyContent: "flex-end", gap: "8px" });
      const cancel = document.createElement("button");
      cancel.textContent = opts.cancelText || "Cancel";
      Object.assign(cancel.style, { padding: "8px 14px", color: "var(--fg-2)" });
      cancel.addEventListener("click", () => { close(); resolve(false); });
      const ok = document.createElement("button");
      ok.textContent = opts.okText || "Confirm";
      Object.assign(ok.style, {
        padding: "8px 14px",
        borderRadius: "6px",
        background: opts.danger ? "var(--danger)" : "var(--accent)",
        color: "#0a0a0a",
        fontWeight: "600",
      });
      ok.addEventListener("click", () => { close(); resolve(true); });
      row.append(cancel, ok);
      wrap.appendChild(row);
      host.appendChild(wrap);
    });
  });
}
