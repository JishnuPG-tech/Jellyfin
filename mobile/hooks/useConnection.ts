import { useState, useEffect, useCallback, useRef } from "react";
import {
  getConnectionManager,
  type ConnectionState,
} from "../lib/connection-manager";
import type { ServerEvent } from "../constants/types";

export interface UseConnectionReturn {
  state: ConnectionState;
  connect: (sessionId?: string) => void;
  disconnect: () => void;
  switchSession: (sessionId: string | null) => void;
  isConnected: boolean;
}

export function useConnection(sessionId?: string): UseConnectionReturn {
  const [state, setState] = useState<ConnectionState>("disconnected");
  const manager = useRef(getConnectionManager());

  useEffect(() => {
    const unsub = manager.current.onStateChange(setState);
    return unsub;
  }, []);

  // Auto-connect on mount
  useEffect(() => {
    manager.current.connect(sessionId);
    return () => {
      // Don't disconnect on unmount — keep connection alive
    };
  }, [sessionId]);

  const connect = useCallback(
    (sid?: string) => manager.current.connect(sid ?? sessionId),
    [sessionId]
  );

  const disconnect = useCallback(() => manager.current.disconnect(), []);

  const switchSession = useCallback(
    (sid: string | null) => manager.current.switchSession(sid),
    []
  );

  return {
    state,
    connect,
    disconnect,
    switchSession,
    isConnected: state === "connected",
  };
}

export function useConnectionEvents(
  onEvent: (event: ServerEvent) => void
) {
  const callbackRef = useRef(onEvent);
  callbackRef.current = onEvent;

  useEffect(() => {
    const manager = getConnectionManager();
    const unsub = manager.onEvent((event) => callbackRef.current(event));
    return unsub;
  }, []);
}
