// Types derived from real OpenCode serve OpenAPI spec (v1.18.3)
// Endpoint prefix: /api/

// ── Health ──
export interface HealthResponse {
  healthy: boolean;
  version: string;
}

// ── Config ──
export interface OpenCodeConfig {
  $schema?: string;
  theme?: string;
  provider?: Record<string, ProviderConfig>;
  model?: Record<string, string>;
  command?: Record<string, string>;
  plugin?: Record<string, unknown>;
  mcp?: Record<string, unknown>;
  share?: unknown;
  experimental?: unknown;
}

export interface ProviderConfig {
  api?: string;
  apiKey?: string;
  models?: Record<string, unknown>;
  options?: Record<string, unknown>;
}

// ── Provider ──
export interface Provider {
  id: string;
  name: string;
  api?: string;
  auth?: ProviderAuth;
  models?: Model[];
  env?: Record<string, string>;
}

export interface ProviderAuth {
  type: "api_key" | "oauth";
  api_key?: { env?: string; header?: string };
  oauth?: { authorize_url: string; token_url: string; scopes: string[] };
}

// ── Model ──
export interface Model {
  id: string;
  name: string;
  providerID: string;
  context?: number;
  maxTokens?: number;
  reasoning?: boolean;
  config?: Record<string, unknown>;
}

// ── Session ──
export interface Session {
  id: string;
  parentID?: string;
  title: string;
  agent?: string;
  model?: string;
  status: "idle" | "busy" | "error";
  directory: string;
  createdAt: string;
  updatedAt: string;
}

export interface SessionStatus {
  busy: boolean;
}

/** Paginated list response from /api/session */
export interface SessionListResponse {
  data: Session[];
  cursor?: {
    previous?: string;
    next?: string;
  };
}

/** Model list response from /api/model */
export interface ModelListResponse {
  location?: {
    directory?: string;
    project?: { id: string; directory: string };
  };
  data: Model[];
}

// ── Message ──
export interface Message {
  id: string;
  sessionID: string;
  role: "user" | "assistant" | "tool" | "system";
  parts: MessagePart[];
  createdAt: string;
}

export type MessagePart =
  | TextPart
  | ToolPart
  | StepPart
  | ReasoningPart
  | SourcePart;

export interface TextPart {
  type: "text";
  text: string;
}

export interface ToolPart {
  type: "tool";
  tool: string;
  state: "call" | "result" | "error";
  callID: string;
  input?: Record<string, unknown>;
  output?: string;
  title?: string;
  metadata?: Record<string, unknown>;
}

export interface StepPart {
  type: "step";
  name: string;
  state: "pending" | "running" | "completed" | "error";
}

export interface ReasoningPart {
  type: "reasoning";
  text: string;
}

export interface SourcePart {
  type: "source";
  source: string;
  title?: string;
}

// ── Events (SSE) ──
export type ServerEvent =
  | SessionCreatedEvent
  | SessionUpdatedEvent
  | SessionDeletedEvent
  | MessageCreatedEvent
  | MessageUpdatedEvent
  | MessagePartUpdatedEvent
  | MessagePartDeletedEvent
  | PermissionRequestedEvent
  | PermissionUpdatedEvent
  | QuestionAskedEvent
  | QuestionAnsweredEvent
  | FileChangedEvent
  | TodoUpdatedEvent
  | SyncEvent
  | ErrorEvent;

export interface SessionCreatedEvent {
  type: "session.created";
  properties: Session;
}

export interface SessionUpdatedEvent {
  type: "session.updated";
  properties: Partial<Session> & { id: string };
}

export interface SessionDeletedEvent {
  type: "session.deleted";
  properties: { id: string };
}

export interface MessageCreatedEvent {
  type: "message.created";
  properties: Message;
}

export interface MessageUpdatedEvent {
  type: "message.updated";
  properties: Partial<Message> & { id: string };
}

export interface MessagePartUpdatedEvent {
  type: "message.part.updated";
  properties: {
    messageID: string;
    part: MessagePart;
    sessionID: string;
  };
}

export interface MessagePartDeletedEvent {
  type: "message.part.deleted";
  properties: {
    messageID: string;
    partID: string;
    sessionID: string;
  };
}

export interface PermissionRequestedEvent {
  type: "permission.requested";
  properties: PermissionRequest;
}

export interface PermissionUpdatedEvent {
  type: "permission.updated";
  properties: PermissionRequest;
}

export interface QuestionAskedEvent {
  type: "question.asked";
  properties: QuestionRequest;
}

export interface QuestionAnsweredEvent {
  type: "question.answered";
  properties: QuestionRequest;
}

export interface FileChangedEvent {
  type: "file.changed";
  properties: { path: string; status: string };
}

export interface TodoUpdatedEvent {
  type: "todo.updated";
  properties: { sessionID: string; todos: Todo[] };
}

export interface SyncEvent {
  type: "sync";
  properties: Record<string, unknown>;
}

export interface ErrorEvent {
  type: "error";
  properties: { message: string; code?: string };
}

// ── Permission ──
export interface PermissionRequest {
  id: string;
  sessionID: string;
  messageID?: string;
  partID?: string;
  permission: string;
  description?: string;
  state: "pending" | "approved" | "denied";
  createdAt: string;
}

export interface SavedPermission {
  id: string;
  permission: string;
  pattern?: string;
  allow: boolean;
}

// ── Question ──
export interface QuestionRequest {
  id: string;
  sessionID: string;
  question: string;
  options?: string[];
  state: "pending" | "answered" | "rejected";
  answer?: string;
  createdAt: string;
}

// ── Todo ──
export interface Todo {
  id: string;
  content: string;
  status: "pending" | "in_progress" | "completed";
  metadata?: Record<string, unknown>;
}

// ── File ──
export interface FileEntry {
  name: string;
  path: string;
  type: "file" | "directory";
  size?: number;
  modified?: string;
}

export interface FileContent {
  content: string;
  encoding?: string;
}

export interface FileStatus {
  path: string;
  status: string;
}

// ── Find/Search ──
export interface FindResult {
  file: string;
  line: number;
  column: number;
  match: string;
  context: string;
}

export interface SymbolResult {
  name: string;
  kind: string;
  file: string;
  line: number;
  column: number;
}

// ── PTY ──
export interface PTY {
  id: string;
  shell: string;
  cwd: string;
  pid?: number;
  createdAt: string;
}

// ── Command ──
export interface Command {
  name: string;
  description?: string;
  category?: string;
}

// ── Skill ──
export interface Skill {
  name: string;
  description?: string;
  location?: string;
}

// ── Agent ──
export interface Agent {
  name: string;
  description?: string;
  tools?: string[];
}

// ── Diff ──
export interface DiffEntry {
  path: string;
  status: string;
  additions: number;
  deletions: number;
  hunks?: DiffHunk[];
}

export interface DiffHunk {
  oldStart: number;
  newStart: number;
  oldLines: number;
  newLines: number;
  content: string;
}

// ── VCS ──
export interface VCSStatus {
  branch: string;
  dirty: boolean;
  ahead: number;
  behind: number;
}

// ── Context helpers ──
export interface SessionContext {
  files: string[];
  capabilities: string[];
}

export interface SessionHistory {
  messages: Message[];
}

// ── Theme ──
export type ThemeName = "tokyo" | "dracula" | "nord" | "gruvbox" | "onedark" | "catppuccin";

export interface Theme {
  name: ThemeName;
  label: string;
  colors: {
    bg: string;
    bgSecondary: string;
    bgTertiary: string;
    surface: string;
    border: string;
    text: string;
    textSecondary: string;
    textMuted: string;
    primary: string;
    primaryText: string;
    secondary: string;
    danger: string;
    success: string;
    warning: string;
    accent: string;
  };
}
