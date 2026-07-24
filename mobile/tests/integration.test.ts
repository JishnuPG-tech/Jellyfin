/**
 * End-to-end integration test for OpenCode Mobile client
 * Run against a live opencode serve instance
 *
 * Usage: npx tsx mobile/tests/integration.test.ts
 */

const BASE = "http://127.0.0.1:4096";

interface TestResult {
  name: string;
  passed: boolean;
  error?: string;
  duration: number;
}

const results: TestResult[] = [];

async function test(name: string, fn: () => Promise<void>) {
  const start = Date.now();
  try {
    await fn();
    results.push({ name, passed: true, duration: Date.now() - start });
    console.log(`  ✓ ${name} (${Date.now() - start}ms)`);
  } catch (err: any) {
    results.push({
      name,
      passed: false,
      error: err.message,
      duration: Date.now() - start,
    });
    console.log(`  ✗ ${name}: ${err.message}`);
  }
}

function assert(condition: boolean, msg: string) {
  if (!condition) throw new Error(msg);
}

async function api<T = any>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...opts?.headers },
    ...opts,
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ── Tests ──

async function run() {
  console.log("\n=== OpenCode Integration Tests ===\n");

  let sessionId: string;

  // 1. Health
  await test("GET /api/health", async () => {
    const data = await api("/api/health");
    assert(data.healthy === true, "not healthy");
  });

  // 2. Config (root endpoint — /api/config returns HTML)
  await test("GET /config", async () => {
    const data = await api("/config");
    assert(typeof data === "object", "config is not object");
  });

  // 3. List sessions (root returns array)
  await test("GET /session (list)", async () => {
    const data = await api("/session");
    assert(Array.isArray(data), "not array");
  });

  // 4. List sessions via /api/ (returns {data, cursor})
  await test("GET /api/session (list)", async () => {
    const data = await api("/api/session");
    assert(Array.isArray(data.data), "data is not array");
  });

  // 5. Create session
  await test("POST /session (create)", async () => {
    const data = await api("/session", {
      method: "POST",
      body: JSON.stringify({ title: "Integration Test" }),
    });
    assert(data.id, "no id returned");
    sessionId = data.id;
  });

  // 6. Get session (root)
  await test("GET /session/{id}", async () => {
    const data = await api(`/session/${sessionId}`);
    assert(data.id === sessionId, "id mismatch");
  });

  // 7. Get session via /api/ (returns {data: Session})
  await test("GET /api/session/{id}", async () => {
    const data = await api(`/api/session/${sessionId}`);
    assert(data.data?.id === sessionId, "id mismatch in data");
  });

  // 8. List messages (empty)
  await test("GET /session/{id}/message (empty)", async () => {
    const data = await api(`/session/${sessionId}/message`);
    assert(Array.isArray(data), "not array");
  });

  // 9. Send message (requires {parts: [{type:'text', text:'...'}]})
  await test("POST /session/{id}/message (send)", async () => {
    const data = await api(`/session/${sessionId}/message`, {
      method: "POST",
      body: JSON.stringify({
        parts: [{ type: "text", text: "Say exactly: integration test ok" }],
      }),
    });
    assert(data.info || data.parts, "unexpected response shape");
  });

  // 10. SSE event stream (global endpoint)
  await test("GET /event (SSE)", async () => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 4000);

    const res = await fetch(`${BASE}/event`, {
      headers: { Accept: "text/event-stream" },
      signal: controller.signal,
    });

    clearTimeout(timeout);
    assert(res.ok, `HTTP ${res.status}`);

    const reader = res.body!.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let eventCount = 0;

    const readTimeout = setTimeout(() => reader.cancel(), 3000);

    while (eventCount < 1) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (line.startsWith("data: ") || line.startsWith("event: ")) eventCount++;
      }
    }

    clearTimeout(readTimeout);
    reader.cancel();
    assert(eventCount > 0, `no events (got ${eventCount})`);
  });

  // 11. Providers
  await test("GET /api/provider", async () => {
    const data = await api("/api/provider");
    assert(Array.isArray(data.data), "data is not array");
  });

  // 12. Models
  await test("GET /api/model", async () => {
    const data = await api("/api/model");
    assert(Array.isArray(data.data), "data is not array");
  });

  // 13. Agents
  await test("GET /api/agent", async () => {
    const data = await api("/api/agent");
    assert(Array.isArray(data.data), "data is not array");
  });

  // 14. Commands
  await test("GET /api/command", async () => {
    const data = await api("/api/command");
    assert(Array.isArray(data.data), "data is not array");
  });

  // 15. Skills
  await test("GET /api/skill", async () => {
    const data = await api("/api/skill");
    assert(Array.isArray(data.data), "data is not array");
  });

  // 16. File list
  await test("GET /api/fs/list", async () => {
    const data = await api("/api/fs/list?path=.");
    assert(Array.isArray(data.data), "data is not array");
    assert(data.data.length > 0, "empty file list");
  });

  // 17. File content (root endpoint)
  await test("GET /file/content", async () => {
    const data = await api("/file/content?path=package.json");
    assert(data.content || typeof data === "string", "no content");
  });

  // 18. Find files (root endpoint)
  await test("GET /find/file", async () => {
    const data = await api("/find/file?query=package");
    assert(Array.isArray(data), "not array");
  });

  // 19. PTY list
  await test("GET /api/pty", async () => {
    const data = await api("/api/pty");
    assert(Array.isArray(data.data), "data is not array");
  });

  // 20. VCS status
  await test("GET /vcs/status", async () => {
    const data = await api("/vcs/status");
    assert(typeof data === "object", "not object");
  });

  // 21. Delete session
  await test("DELETE /session/{id}", async () => {
    await api(`/session/${sessionId}`, { method: "DELETE" });
  });

  // 22. Verify deleted
  await test("GET /session/{id} after delete", async () => {
    try {
      await api(`/session/${sessionId}`);
      assert(false, "should have thrown");
    } catch {
      // Expected
    }
  });

  // Summary
  console.log("\n=== Results ===\n");
  const passed = results.filter((r) => r.passed).length;
  const failed = results.filter((r) => !r.passed).length;
  console.log(`  ${passed} passed, ${failed} failed out of ${results.length} tests`);

  if (failed > 0) {
    console.log("\n  Failed tests:");
    results
      .filter((r) => !r.passed)
      .forEach((r) => console.log(`    - ${r.name}: ${r.error}`));
  }

  console.log("");
  process.exit(failed > 0 ? 1 : 0);
}

run().catch((err) => {
  console.error("Fatal:", err);
  process.exit(1);
});
