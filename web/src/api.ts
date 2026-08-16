import type { Artifact, CapabilityState, ContextPreview, Conversation, ConversationDetail, ControlSnapshot, ContextManifest, IdentityDocument, JournalRecord, MemoryRecord, MemoryUsage, PersistedEvent, RunRecord, ScheduleRecord } from "./types";

const jsonHeaders = { "Content-Type": "application/json" };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: "include", ...init });
  if (!response.ok) {
    const detail = await response.text().catch(() => "request failed");
    throw new Error(`${response.status}: ${detail || response.statusText}`);
  }
  return response.json() as Promise<T>;
}

export async function getSession(): Promise<{ authenticated: boolean }> {
  return request("/auth/session");
}

export async function login(token: string): Promise<void> {
  await request("/auth/session", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ token }),
  });
}

export async function logout(): Promise<void> {
  await request("/auth/session", { method: "DELETE" });
}

export async function getHome(): Promise<ControlSnapshot> {
  return request("/api/v1/control/home");
}

export async function getRuns(): Promise<RunRecord[]> {
  return request("/api/v1/runs");
}

export async function getConversations(): Promise<Conversation[]> {
  return request("/api/v1/conversations");
}

export async function getConversation(conversationId: string): Promise<ConversationDetail> {
  return request(`/api/v1/conversations/${encodeURIComponent(conversationId)}`);
}

export async function getWorkspaces(): Promise<Array<{ id: string; name: string; path: string }>> {
  return request("/api/v1/workspaces");
}

export async function createConversation(workspaceId: string, title?: string): Promise<Conversation> {
  return request("/conversations", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ workspace_id: workspaceId, channel: "web", title }),
  });
}

export async function submitRun(conversationId: string, prompt: string): Promise<RunRecord> {
  return request(`/conversations/${encodeURIComponent(conversationId)}/runs`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ prompt, context_profile: "chat" }),
  });
}

export async function cancelRun(run: RunRecord): Promise<RunRecord> {
  return request(`/runs/${encodeURIComponent(run.id)}/cancel`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ expected_version: run.version }),
  });
}

export async function getTimeline(runId: string): Promise<{ run: RunRecord; events: PersistedEvent[] }> {
  return request(`/api/v1/runs/${encodeURIComponent(runId)}/timeline`);
}

export async function getRunArtifacts(runId: string): Promise<Artifact[]> {
  return request(`/api/v1/runs/${encodeURIComponent(runId)}/artifacts`);
}

export async function getArtifact(artifactId: string): Promise<Artifact> {
  return request(`/api/v1/artifacts/${encodeURIComponent(artifactId)}`);
}

export async function getRunContext(runId: string): Promise<Record<string, unknown>> {
  return request(`/api/v1/runs/${encodeURIComponent(runId)}/context`);
}

export async function getRunCapabilities(runId: string): Promise<Record<string, unknown>> {
  return request(`/api/v1/runs/${encodeURIComponent(runId)}/capabilities`);
}

export async function getIdentity(): Promise<IdentityDocument[]> {
  return request("/api/v1/identity");
}

export async function getIdentityHistory(name: string): Promise<IdentityDocument[]> {
  return request(`/api/v1/identity/${encodeURIComponent(name)}/history`);
}

export async function updateIdentity(name: string, content: string, expectedVersion: number): Promise<IdentityDocument> {
  return request(`/api/v1/identity/${encodeURIComponent(name)}`, {
    method: "PUT", headers: jsonHeaders, body: JSON.stringify({ content, expected_version: expectedVersion }),
  });
}

export async function getMemories(kind?: string): Promise<MemoryRecord[]> {
  const query = kind ? `?kind=${encodeURIComponent(kind)}` : "";
  return request(`/api/v1/memories${query}`);
}

export async function searchMemories(query: string): Promise<MemoryRecord[]> {
  return request(`/api/v1/memories/search?q=${encodeURIComponent(query)}`);
}

export async function getMemory(id: string): Promise<MemoryUsage> {
  return request(`/api/v1/memories/${encodeURIComponent(id)}`);
}

export async function getJournals(): Promise<JournalRecord[]> {
  return request("/api/v1/journals");
}

export async function getJournal(date: string): Promise<JournalRecord> {
  return request(`/api/v1/journals/${encodeURIComponent(date)}`);
}

export async function updateJournal(date: string, content: string, expectedSha256: string): Promise<JournalRecord> {
  return request(`/api/v1/journals/${encodeURIComponent(date)}`, {
    method: "PUT", headers: jsonHeaders, body: JSON.stringify({ content, expected_sha256: expectedSha256 }),
  });
}

export async function previewContext(task: string, profile: string, conversationId?: string): Promise<ContextPreview> {
  return request("/api/v1/context/preview", {
    method: "POST", headers: jsonHeaders,
    body: JSON.stringify({ task, profile, conversation_id: conversationId }),
  });
}

export async function getRunContextManifest(runId: string): Promise<ContextManifest> {
  return request(`/api/v1/runs/${encodeURIComponent(runId)}/context`);
}

export async function getAutomations(): Promise<ScheduleRecord[]> {
  return request("/api/v1/automations");
}

export async function getAutomation(id: string): Promise<ScheduleRecord> {
  return request(`/api/v1/automations/${encodeURIComponent(id)}`);
}

export async function createAutomation(value: Omit<ScheduleRecord, "id" | "enabled" | "generation" | "version" | "next_run_at" | "last_run_at" | "created_at" | "updated_at" | "deleted_at" | "triggers"> & { start_at?: string | null }): Promise<ScheduleRecord> {
  return request("/api/v1/automations", { method: "POST", headers: jsonHeaders, body: JSON.stringify(value) });
}

export async function updateAutomation(value: ScheduleRecord): Promise<ScheduleRecord> {
  return request(`/api/v1/automations/${encodeURIComponent(value.id)}`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify({ ...value, expected_version: value.version }) });
}

export async function setAutomationEnabled(value: ScheduleRecord, enabled: boolean): Promise<ScheduleRecord> {
  return request(`/api/v1/automations/${encodeURIComponent(value.id)}/${enabled ? "enable" : "disable"}`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ expected_version: value.version }) });
}

export async function runAutomationNow(id: string, requestId: string): Promise<{ trigger: Record<string, unknown>; run: RunRecord | null }> {
  return request(`/api/v1/automations/${encodeURIComponent(id)}/run-now`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ request_id: requestId }) });
}

export async function getCapabilities(workspaceId?: string, profile = "coding"): Promise<CapabilityState> {
  const query = new URLSearchParams({ profile });
  if (workspaceId) query.set("workspace_id", workspaceId);
  return request(`/api/v1/capabilities?${query.toString()}`);
}

export async function setCapabilityEnabled(type: "skills" | "mcp", id: string, enabled: boolean, expectedUpdatedAt: string): Promise<unknown> {
  return request(`/api/v1/capabilities/${type}/${encodeURIComponent(id)}/${enabled ? "enable" : "disable"}`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ expected_updated_at: expectedUpdatedAt }) });
}

export async function checkMcpHealth(id: string): Promise<unknown> {
  return request(`/api/v1/capabilities/mcp/${encodeURIComponent(id)}/health`, { method: "POST", headers: jsonHeaders, body: "{}" });
}

export function controlSocketUrl(cursor: number): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const base = `${protocol}//${window.location.host}/ws/control`;
  return `${base}?after=${encodeURIComponent(cursor)}`;
}
