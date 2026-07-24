import { getHealth, subscribeEvents, parseSSELines } from "./api";
import { getServerUrl, getCredentials } from "./storage";
import type { ServerEvent } from "../constants/types";

export type ConnectionState =
  | "disconnected"
  | "connecting"
  | "connected"
  | "reconnecting"
  | "error";

export type ConnectionListener = (state: ConnectionState) => void;
export type EventListener = (event: ServerEvent) => void;

interface ConnectionManagerOptions {
  /** Max reconnect attempts before giving up (default: Infinity) */
  maxRetries?: number;
  /** Base delay in ms for exponential backoff (default: 1000) */
  baseDelay?: number;
  /** Max delay in ms (default: 30000) */
  maxDelay?: number;
  /** Health check interval in ms (default: 15000) */
  heartbeatInterval?: number;
  /** Timeout for health check in ms (default: 5000) */
  healthTimeout?: number;
}

export class ConnectionManager {
  private state: ConnectionState = "disconnected";
  private retryCount = 0;
  private maxRetries: number;
  private baseDelay: number;
  private maxDelay: number;
  private heartbeatMs: number;
  private healthTimeout: number;

  private stateListeners: Set<ConnectionListener> = new Set();
  private eventListeners: Set<EventListener> = new Set();

  private heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private abortController: AbortController | null = null;
  private sseBuffer = "";

  /** Currently active SSE session subscription (null = global events) */
  private activeSessionId: string | null = null;

  constructor(options?: ConnectionManagerOptions) {
    this.maxRetries = options?.maxRetries ?? Infinity;
    this.baseDelay = options?.baseDelay ?? 1000;
    this.maxDelay = options?.maxDelay ?? 30000;
    this.heartbeatMs = options?.heartbeatInterval ?? 15000;
    this.healthTimeout = options?.healthTimeout ?? 5000;
  }

  // ── State ──

  getState(): ConnectionState {
    return this.state;
  }

  isConnected(): boolean {
    return this.state === "connected";
  }

  // ── Listeners ──

  onStateChange(listener: ConnectionListener): () => void {
    this.stateListeners.add(listener);
    return () => this.stateListeners.delete(listener);
  }

  onEvent(listener: EventListener): () => void {
    this.eventListeners.add(listener);
    return () => this.eventListeners.delete(listener);
  }

  private setState(newState: ConnectionState) {
    if (this.state === newState) return;
    this.state = newState;
    this.stateListeners.forEach((fn) => fn(newState));
  }

  private emitEvent(event: ServerEvent) {
    this.eventListeners.forEach((fn) => fn(event));
  }

  // ── Connect ──

  async connect(sessionId?: string) {
    if (this.state === "connected" || this.state === "connecting") return;

    this.activeSessionId = sessionId ?? null;
    this.setState("connecting");

    try {
      await this.checkHealth();
      this.retryCount = 0;
      this.setState("connected");
      this.startHeartbeat();
      this.connectSSE();
    } catch {
      this.setState("error");
      this.scheduleReconnect();
    }
  }

  disconnect() {
    this.stopHeartbeat();
    this.stopReconnect();
    this.abortController?.abort();
    this.abortController = null;
    this.sseBuffer = "";
    this.retryCount = 0;
    this.setState("disconnected");
  }

  // ── Health check ──

  private async checkHealth(): Promise<void> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.healthTimeout);

    try {
      const result = await Promise.race([
        getHealth(),
        new Promise<never>((_, reject) => {
          controller.signal.addEventListener("abort", () =>
            reject(new Error("Health check timeout"))
          );
        }),
      ]);

      clearTimeout(timeout);

      if (!result.healthy) {
        throw new Error("Server reports unhealthy");
      }
    } catch (err) {
      clearTimeout(timeout);
      throw err;
    }
  }

  // ── Heartbeat ──

  private startHeartbeat() {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(async () => {
      try {
        await this.checkHealth();
        // If we were in error state, recover
        if (this.state === "error") {
          this.retryCount = 0;
          this.setState("connected");
          this.connectSSE();
        }
      } catch {
        if (this.state === "connected") {
          this.setState("error");
          this.scheduleReconnect();
        }
      }
    }, this.heartbeatMs);
  }

  private stopHeartbeat() {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  // ── SSE ──

  private connectSSE() {
    this.abortController?.abort();
    this.abortController = new AbortController();
    this.sseBuffer = "";

    const doConnect = async () => {
      try {
        const response = await subscribeEvents(this.activeSessionId ?? undefined);
        const reader = response.body?.getReader();
        if (!reader) throw new Error("No readable stream");

        const decoder = new TextDecoder();

        while (true) {
          if (this.abortController?.signal.aborted) break;

          const { done, value } = await reader.read();
          if (done) break;

          this.sseBuffer += decoder.decode(value, { stream: true });
          const { events, remaining } = parseSSELines(this.sseBuffer);
          this.sseBuffer = remaining;

          for (const event of events) {
            this.emitEvent(event);
          }
        }

        // SSE ended normally — try reconnect
        if (this.state === "connected") {
          this.setState("error");
          this.scheduleReconnect();
        }
      } catch (err) {
        if (
          this.abortController?.signal.aborted ||
          this.state === "disconnected"
        ) {
          return;
        }

        console.warn("[ConnectionManager] SSE error:", err);
        if (this.state === "connected") {
          this.setState("error");
          this.scheduleReconnect();
        }
      }
    };

    doConnect();
  }

  // ── Reconnect with exponential backoff ──

  private scheduleReconnect() {
    if (this.retryCount >= this.maxRetries) {
      this.setState("disconnected");
      return;
    }

    this.setState("reconnecting");

    const delay = Math.min(
      this.baseDelay * Math.pow(2, this.retryCount),
      this.maxDelay
    );
    this.retryCount++;

    this.reconnectTimer = setTimeout(() => {
      this.connectSSE();
      // Also check health
      this.checkHealth()
        .then(() => {
          this.retryCount = 0;
          this.setState("connected");
        })
        .catch(() => {
          // Will retry again
        });
    }, delay);
  }

  private stopReconnect() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  // ── Switch session subscription ──

  switchSession(sessionId: string | null) {
    if (this.activeSessionId === sessionId) return;
    this.activeSessionId = sessionId;
    if (this.state === "connected") {
      this.connectSSE();
    }
  }
}

// ── Singleton ──

let instance: ConnectionManager | null = null;

export function getConnectionManager(options?: ConnectionManagerOptions): ConnectionManager {
  if (!instance) {
    instance = new ConnectionManager(options);
  }
  return instance;
}
