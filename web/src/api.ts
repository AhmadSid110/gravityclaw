import type { Conversation, ConversationDetail, ControlSnapshot, PersistedEvent, RunRecord } from "./types";

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

export function controlSocketUrl(cursor: number): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const base = `${protocol}//${window.location.host}/ws/control`;
  return `${base}?after=${encodeURIComponent(cursor)}`;
}
