// OpenCode-Serve SPA
// Provides a ChatGPT-style sidebar with full session management backed by
// opencode's existing /api/session and /api/session/{id}/message APIs.
//
// Architecture:
//   - Vanilla ES modules, no build step
//   - Single-page app that consumes opencode's HTTP API verbatim
//   - localStorage holds UI state only: pinnedIds, archivedIds, drawerCollapsed,
//     lastSessionId, draftMessage. Authoritative session data always comes from
//     /api/session so that nothing is duplicated.
//   - SSE on /api/event streams real-time updates for sessions and messages.
//   - Title auto-generation is sent through opencode's existing session APIs.
//
// All UI state is intentionally local; opencode remains the source of truth
// for session lifecycle.

import { mount } from "./components.js";
import { openOverlay } from "./ui.js";

mount();