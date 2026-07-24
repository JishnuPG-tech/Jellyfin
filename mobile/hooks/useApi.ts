import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import * as api from "../lib/api";

// ── Health ──

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: api.getHealth,
    refetchInterval: 10000,
    retry: 3,
  });
}

// ── Config ──

export function useConfig() {
  return useQuery({
    queryKey: ["config"],
    queryFn: api.getConfig,
  });
}

export function usePatchConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.patchConfig,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["config"] }),
  });
}

// ── Sessions ──

export function useSessions() {
  return useQuery({
    queryKey: ["sessions"],
    queryFn: api.getSessions,
  });
}

export function useActiveSessions() {
  return useQuery({
    queryKey: ["sessions", "active"],
    queryFn: api.getActiveSessions,
  });
}

export function useSession(id: string) {
  return useQuery({
    queryKey: ["session", id],
    queryFn: () => api.getSession(id),
    enabled: !!id,
  });
}

export function useCreateSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (options?: { title?: string; agent?: string; model?: string; directory?: string }) =>
      api.createSession(options),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sessions"] }),
  });
}

export function useDeleteSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deleteSession(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sessions"] }),
  });
}

export function usePatchSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: { title?: string; agent?: string; model?: string } }) =>
      api.patchSession(id, data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sessions"] }),
  });
}

export function useSessionTodos(sessionId: string) {
  return useQuery({
    queryKey: ["todos", sessionId],
    queryFn: () => api.getSessionTodos(sessionId),
    enabled: !!sessionId,
  });
}

// ── Messages ──

export function useMessages(sessionId: string) {
  return useQuery({
    queryKey: ["messages", sessionId],
    queryFn: () => api.getMessages(sessionId),
    enabled: !!sessionId,
  });
}

export function useSendMessage() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ sessionId, content }: { sessionId: string; content: string }) =>
      api.sendMessage(sessionId, content),
    onSuccess: (_, vars) => {
      qc.invalidateQueries({ queryKey: ["messages", vars.sessionId] });
      qc.invalidateQueries({ queryKey: ["sessions"] });
    },
  });
}

export function useAbortSession() {
  return useMutation({
    mutationFn: (sessionId: string) => api.abortSession(sessionId),
  });
}

export function useCompactSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (sessionId: string) => api.compactSession(sessionId),
    onSuccess: (_, sessionId) => {
      qc.invalidateQueries({ queryKey: ["messages", sessionId] });
    },
  });
}

export function useShareSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (sessionId: string) => api.shareSession(sessionId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sessions"] }),
  });
}

export function useForkSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (sessionId: string) => api.forkSession(sessionId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sessions"] }),
  });
}

// ── Session diff / revert ──

export function useSessionDiff(sessionId: string) {
  return useQuery({
    queryKey: ["diff", sessionId],
    queryFn: () => api.getSessionDiff(sessionId),
    enabled: !!sessionId,
  });
}

// ── Permissions ──

export function usePermissionRequests() {
  return useQuery({
    queryKey: ["permissions", "requests"],
    queryFn: api.getPermissionRequests,
    refetchInterval: 5000,
  });
}

export function useSavedPermissions() {
  return useQuery({
    queryKey: ["permissions", "saved"],
    queryFn: api.getSavedPermissions,
  });
}

export function useReplyPermission() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      sessionId,
      requestId,
      allow,
    }: {
      sessionId: string;
      requestId: string;
      allow: boolean;
    }) => api.replyPermission(sessionId, requestId, allow),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["permissions"] });
    },
  });
}

// ── Questions ──

export function useQuestionRequests() {
  return useQuery({
    queryKey: ["questions", "requests"],
    queryFn: api.getQuestionRequests,
    refetchInterval: 5000,
  });
}

export function useReplyQuestion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      sessionId,
      requestId,
      answer,
    }: {
      sessionId: string;
      requestId: string;
      answer: string;
    }) => api.replyQuestion(sessionId, requestId, answer),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["questions"] });
    },
  });
}

// ── Providers / Models ──

export function useProviders() {
  return useQuery({
    queryKey: ["providers"],
    queryFn: api.getProviders,
  });
}

export function useModels() {
  return useQuery({
    queryKey: ["models"],
    queryFn: api.getModels,
  });
}

// ── Agent / Command / Skill ──

export function useAgents() {
  return useQuery({
    queryKey: ["agents"],
    queryFn: api.getAgents,
  });
}

export function useCommands() {
  return useQuery({
    queryKey: ["commands"],
    queryFn: api.getCommands,
  });
}

export function useSkills() {
  return useQuery({
    queryKey: ["skills"],
    queryFn: api.getSkills,
  });
}

// ── Files ──

export function useListFiles(dir: string = ".") {
  return useQuery({
    queryKey: ["files", dir],
    queryFn: () => api.listFiles(dir),
  });
}

export function useFileContent(path: string) {
  return useQuery({
    queryKey: ["fileContent", path],
    queryFn: () => api.readFile(path),
    enabled: !!path,
  });
}

export function useFindFiles(query: string, dir?: string) {
  return useQuery({
    queryKey: ["findFiles", query, dir],
    queryFn: () => api.findFiles(query, dir),
    enabled: query.length > 0,
  });
}

export function useFindSymbols(query: string) {
  return useQuery({
    queryKey: ["findSymbols", query],
    queryFn: () => api.findSymbols(query),
    enabled: query.length > 0,
  });
}

// ── VCS ──

export function useVCSStatus() {
  return useQuery({
    queryKey: ["vcs"],
    queryFn: api.getVCSStatus,
    refetchInterval: 15000,
  });
}

// ── PTY ──

export function usePTYs() {
  return useQuery({
    queryKey: ["pty"],
    queryFn: api.getPTYs,
  });
}

export function useCreatePTY() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (shell?: string) => api.createPTY(shell),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["pty"] }),
  });
}

export function useDeletePTY() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deletePTY(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["pty"] }),
  });
}
