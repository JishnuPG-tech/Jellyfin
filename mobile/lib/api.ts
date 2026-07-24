import { getServerUrl, getAuthHeader, setServerUrl as persistServerUrl, setCredentials, clearAuth, setAuthType, setAuthValue } from "./storage";
import type { AuthType } from "./storage";
import type {
  HealthResponse,
  Session,
  SessionStatus,
  SessionListResponse,
  Message,
  MessagePart,
  OpenCodeConfig,
  Provider,
  Model,
  ModelListResponse,
  FileEntry,
  FileContent,
  FindResult,
  SymbolResult,
  PTY,
  Command,
  Skill,
  Agent,
  DiffEntry,
  VCSStatus,
  PermissionRequest,
  SavedPermission,
  QuestionRequest,
  Todo,
  ServerEvent,
} from "../constants/types";

// ── Base request helper ──

async function request<T>(
  path: string,
  options?: RequestInit
): Promise<T> {
  const base = getServerUrl();
  const authHeaders = getAuthHeader();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...authHeaders,
    ...((options?.headers as Record<string, string>) || {}),
  };

  const res = await fetch(`${base}${path}`, {
    ...options,
    headers,
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `HTTP ${res.status} ${res.statusText}`);
  }

  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return res.json();
  }
  return res.text() as unknown as T;
}

/** Extract data array from { location, data } or { data } response */
function extractData<T>(response: { data?: T[] } | T[]): T[] {
  if (Array.isArray(response)) return response;
  return response.data || [];
}

// ── Server URL / Credentials ──

export function setServerUrl(url: string) {
  persistServerUrl(url);
}

export function setServerCredentials(username: string, password: string) {
  setCredentials(username, password);
}

// ── Health ──

export function getHealth(): Promise<HealthResponse> {
  return request("/api/health");
}

// ── Config ── (root endpoint, not /api/)

export function getConfig(): Promise<OpenCodeConfig> {
  return request("/config");
}

export function patchConfig(config: Partial<OpenCodeConfig>): Promise<void> {
  return request("/config", { method: "PATCH", body: JSON.stringify(config) });
}

// ── Sessions ──

export function getSessions(): Promise<Session[]> {
  return request("/session");
}

export function getActiveSessions(): Promise<Session[]> {
  return request("/session/active");
}

export function getSession(id: string): Promise<Session> {
  return request(`/session/${id}`);
}

/** Get session via /api/ — returns {data: Session} */
export function getSessionApi(id: string): Promise<Session> {
  return request<{ data: Session }>(`/api/session/${id}`).then((r) => r.data);
}

export function createSession(options?: {
  title?: string;
  agent?: string;
  model?: string;
  directory?: string;
}): Promise<Session> {
  return request("/session", {
    method: "POST",
    body: JSON.stringify(options || {}),
  });
}

export function deleteSession(id: string): Promise<void> {
  return request(`/session/${id}`, { method: "DELETE" });
}

export function patchSession(
  id: string,
  data: { title?: string; agent?: string; model?: string }
): Promise<Session> {
  return request(`/session/${id}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export function getSessionStatus(id: string): Promise<SessionStatus> {
  return request(`/session/${id}`);
}

export function getSessionChildren(id: string): Promise<Session[]> {
  return request<Session[]>(`/session/${id}/children`).catch(() => []);
}

export function getSessionTodos(id: string): Promise<Todo[]> {
  return request(`/session/${id}/todo`);
}

// ── Messages ──

export function getMessages(sessionId: string): Promise<Message[]> {
  return request(`/session/${sessionId}/message`);
}

export function getMessage(sessionId: string, messageId: string): Promise<Message> {
  return request(`/session/${sessionId}/message/${messageId}`);
}

export function deleteMessage(sessionId: string, messageId: string): Promise<void> {
  return request(`/session/${sessionId}/message/${messageId}`, {
    method: "DELETE",
  });
}

// ── Send message (triggers SSE response) ──

export interface MessageResponse {
  info: {
    id: string;
    parentID?: string;
    role: string;
    sessionID: string;
    modelID?: string;
    providerID?: string;
    time?: { created: number; completed?: number };
    finish?: string;
  };
  parts: MessagePart[];
}

export function sendMessage(
  sessionId: string,
  content: string
): Promise<MessageResponse> {
  return request(`/session/${sessionId}/message`, {
    method: "POST",
    body: JSON.stringify({
      parts: [{ type: "text", text: content }],
    }),
  });
}

// ── Session actions ──

export function abortSession(sessionId: string): Promise<void> {
  return request(`/session/${sessionId}/abort`, { method: "POST" });
}

export function initSession(sessionId: string): Promise<void> {
  return request(`/session/${sessionId}/init`, { method: "POST" });
}

export function compactSession(sessionId: string): Promise<void> {
  return request(`/session/${sessionId}/compact`, { method: "POST" });
}

export function summarizeSession(sessionId: string): Promise<void> {
  return request(`/session/${sessionId}/summarize`, { method: "POST" });
}

export function shareSession(sessionId: string): Promise<{ url?: string }> {
  return request(`/session/${sessionId}/share`, { method: "POST" });
}

export function forkSession(sessionId: string): Promise<Session> {
  return request(`/session/${sessionId}/fork`, { method: "POST" });
}

// ── Session diff / revert ──

export function getSessionDiff(sessionId: string): Promise<DiffEntry[]> {
  return request(`/session/${sessionId}/diff`);
}

export function revertSessionStage(sessionId: string): Promise<void> {
  return request(`/session/${sessionId}/revert`, { method: "POST" });
}

export function revertSessionClear(sessionId: string): Promise<void> {
  return request(`/session/${sessionId}/unrevert`, { method: "POST" });
}

// ── Session history / context ──

export function getSessionHistory(sessionId: string): Promise<{ messages: Message[] }> {
  return request(`/session/${sessionId}/history`);
}

export function getSessionContext(sessionId: string): Promise<{ files: string[]; capabilities: string[] }> {
  return request(`/session/${sessionId}/context`);
}

// ── Permissions ──

export function getPermissionRequests(): Promise<PermissionRequest[]> {
  return request("/permission");
}

export function getSavedPermissions(): Promise<SavedPermission[]> {
  return request("/permission/saved");
}

export function deleteSavedPermission(id: string): Promise<void> {
  return request(`/permission/saved/${id}`, { method: "DELETE" });
}

export function replyPermission(
  sessionId: string,
  requestId: string,
  allow: boolean
): Promise<void> {
  return request(`/session/${sessionId}/permissions/${requestId}`, {
    method: "POST",
    body: JSON.stringify({ allow }),
  });
}

// ── Questions ──

export function getQuestionRequests(): Promise<QuestionRequest[]> {
  return request("/question");
}

export function getSessionQuestions(sessionId: string): Promise<QuestionRequest[]> {
  return request(`/session/${sessionId}/question`);
}

export function replyQuestion(
  sessionId: string,
  requestId: string,
  answer: string
): Promise<void> {
  return request(`/question/${requestId}/reply`, {
    method: "POST",
    body: JSON.stringify({ answer }),
  });
}

export function rejectQuestion(
  sessionId: string,
  requestId: string
): Promise<void> {
  return request(`/question/${requestId}/reject`, {
    method: "POST",
  });
}

// ── Providers ──

export function getProviders(): Promise<Provider[]> {
  return request<{ data: Provider[] }>("/api/provider").then((r) => r.data);
}

export function getProvider(id: string): Promise<Provider> {
  return request(`/api/provider/${id}`);
}

// ── Models ──

export function getModels(): Promise<Model[]> {
  return request<{ data: Model[] }>("/api/model").then((r) => r.data);
}

// ── Agent / Command / Skill ──

export function getAgents(): Promise<Agent[]> {
  return request<{ data: Agent[] }>("/api/agent").then((r) => r.data);
}

export function getCommands(): Promise<Command[]> {
  return request<{ data: Command[] }>("/api/command").then((r) => r.data);
}

export function getSkills(): Promise<Skill[]> {
  return request<{ data: Skill[] }>("/api/skill").then((r) => r.data);
}

// ── Files ──

export function listFiles(dir: string = "."): Promise<FileEntry[]> {
  return request<{ data: FileEntry[] }>(`/api/fs/list?path=${encodeURIComponent(dir)}`).then((r) => r.data);
}

export function readFile(filePath: string): Promise<FileContent> {
  return request(`/file/content?path=${encodeURIComponent(filePath)}`);
}

export function findFiles(query: string, dir?: string): Promise<FindResult[]> {
  const params = new URLSearchParams({ query });
  if (dir) params.set("path", dir);
  return request(`/find/file?${params.toString()}`);
}

export function findSymbols(query: string): Promise<SymbolResult[]> {
  return request(`/find/symbol?query=${encodeURIComponent(query)}`);
}

// ── VCS ──

export function getVCSStatus(): Promise<VCSStatus> {
  return request("/api/vcs/status");
}

// ── PTY ──

export function getPTYs(): Promise<PTY[]> {
  return request<{ data: PTY[] }>("/api/pty").then((r) => r.data);
}

export function createPTY(shell?: string): Promise<PTY> {
  return request("/api/pty", {
    method: "POST",
    body: JSON.stringify({ shell }),
  });
}

export function getPTY(id: string): Promise<PTY> {
  return request(`/api/pty/${id}`);
}

export function deletePTY(id: string): Promise<void> {
  return request(`/api/pty/${id}`, { method: "DELETE" });
}

export function getPTYConnectToken(id: string): Promise<{ token: string }> {
  return request(`/api/pty/${id}/connect-token`, { method: "POST" });
}

// ── SSE Event Stream (returns raw Response for manual parsing) ──

export async function subscribeEvents(
  sessionId?: string
): Promise<Response> {
  const base = getServerUrl();
  const authHeaders = getAuthHeader();
  const url = sessionId
    ? `${base}/session/${sessionId}/event`
    : `${base}/event`;

  const res = await fetch(url, {
    headers: { Accept: "text/event-stream", ...authHeaders },
  });

  if (!res.ok) {
    throw new Error(`SSE connection failed: ${res.status}`);
  }

  return res;
}

// ── SSE Parser helper ──

export function parseSSELines(
  buffer: string
): { events: ServerEvent[]; remaining: string } {
  const events: ServerEvent[] = [];
  const lines = buffer.split("\n");
  const remaining = lines.pop() || "";

  let eventType = "";
  let eventData = "";

  for (const line of lines) {
    if (line.startsWith("event: ")) {
      eventType = line.slice(7).trim();
    } else if (line.startsWith("data: ")) {
      eventData = line.slice(6);
    } else if (line === "") {
      if (eventType && eventData) {
        try {
          events.push(JSON.parse(eventData) as ServerEvent);
        } catch {
          // skip malformed
        }
      }
      eventType = "";
      eventData = "";
    }
  }

  return { events, remaining };
}
