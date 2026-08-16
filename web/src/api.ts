import type { ControlSnapshot, PersistedEvent, RunRecord } from "./types";

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

export async function getTimeline(runId: string): Promise<{ run: RunRecord; events: PersistedEvent[] }> {
  return request(`/api/v1/runs/${encodeURIComponent(runId)}/timeline`);
}

export function controlSocketUrl(cursor: number): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const base = `${protocol}//${window.location.host}/ws/control`;
  return `${base}?after=${encodeURIComponent(cursor)}`;
}
