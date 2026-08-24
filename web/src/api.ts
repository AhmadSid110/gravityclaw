import type {
  ProgressSnapshot,
  TelemetryEvent,
  Artifact,
  AgyQuota,
  AttachmentRecord,
  CapabilityState,
  ContextPreview,
  ContextSnapshot,
  ContextStatus,
  Conversation,
  ConversationDetail,
  ConversationEffort,
  ConversationModel,
  ConversationSearchResult,
  ControlSnapshot,
  ContextManifest,
  FlowTask,
  GoalEvaluation,
  GoalRecord,
  IdentityDocument,
  JournalRecord,
  MemoryCandidate,
  MemoryRecord,
  MemoryRevision,
  CuratorStatus,
  ConsolidationReport,
  MemoryUsage,
  ModelCatalog,
  PersistedEvent,
  RunRecord,
  ScheduleRecord,
  TaskAttempt,
  TaskComment,
  TaskFlow,
  TaskHandoffItem,
  UsageSummary,
} from "./types";

const jsonHeaders = { "Content-Type": "application/json" };

// Derive the mount point from the emitted asset URL so the console works at
// both `/` and behind a reverse proxy such as `/gravityclaw/`.
const assetPath = new URL(import.meta.url).pathname;
const assetRoot = assetPath.match(/^(.*)\/assets\//)?.[1] ?? "";
const applicationBasePath = assetRoot ? `${assetRoot}/` : "/";

function applicationPath(path: string): string {
  return `${applicationBasePath}${path.replace(/^\/+/, "")}`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(applicationPath(path), { credentials: "include", ...init });
  if (!response.ok) {
    const detail = await response.text().catch(() => "request failed");
    throw new Error(`${response.status}: ${detail || response.statusText}`);
  }
  return response.json() as Promise<T>;
}

async function requestOptional<T>(path: string): Promise<T | null> {
  const response = await fetch(applicationPath(path), { credentials: "include" });
  if (response.status === 204 || response.status === 404) return null;
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

export async function getModels(): Promise<ModelCatalog> {
  return request("/api/v1/models");
}

export async function getConversationModel(conversationId: string): Promise<ConversationModel> {
  return request(`/api/v1/conversations/${encodeURIComponent(conversationId)}/model`);
}

export async function setConversationModel(conversationId: string, model: string | null): Promise<ConversationModel> {
  return request(`/api/v1/conversations/${encodeURIComponent(conversationId)}/model`, {
    method: "PUT",
    headers: jsonHeaders,
    body: JSON.stringify({ model }),
  });
}

export async function getConversationEffort(conversationId: string): Promise<ConversationEffort> {
  return request(`/api/v1/conversations/${encodeURIComponent(conversationId)}/effort`);
}

export async function setConversationEffort(conversationId: string, effort: string | null): Promise<ConversationEffort> {
  return request(`/api/v1/conversations/${encodeURIComponent(conversationId)}/effort`, {
    method: "PUT",
    headers: jsonHeaders,
    body: JSON.stringify({ effort }),
  });
}

export async function getUsage(days = 30): Promise<UsageSummary> {
  return request(`/api/v1/usage?days=${days}`);
}

export async function getAgyQuota(): Promise<AgyQuota> {
  return request("/api/v1/quota");
}

export async function getContextStatus(conversationId: string): Promise<ContextStatus> {
  return request(`/api/v1/conversations/${encodeURIComponent(conversationId)}/context-status`);
}

export async function compactConversation(
  conversationId: string,
  keepRecentTurns = 8,
): Promise<{
  summary_id: string;
  version: number;
  message_count: number;
  messages_compacted_this_run: number;
  content: string;
  context_status: ContextStatus;
}> {
  return request(`/api/v1/conversations/${encodeURIComponent(conversationId)}/compact`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ keep_recent_turns: keepRecentTurns }),
  });
}

export async function getWorkspaces(): Promise<Array<{ id: string; name: string; path: string }>> {
  return request("/api/v1/workspaces");
}

export async function createConversation(workspaceId: string, title?: string, kind: "main" | "normal" = "normal"): Promise<Conversation> {
  return request("/api/v1/conversations", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ workspace_id: workspaceId, channel: "web", title, kind }),
  });
}

export async function updateConversation(conversationId: string, updates: { title?: string; model_override?: string | null }): Promise<Conversation> {
  return request(`/api/v1/conversations/${encodeURIComponent(conversationId)}`, {
    method: "PATCH",
    headers: jsonHeaders,
    body: JSON.stringify(updates),
  });
}

export async function archiveConversation(conversationId: string): Promise<void> {
  await request(`/api/v1/conversations/${encodeURIComponent(conversationId)}`, {
    method: "DELETE",
    headers: jsonHeaders,
  });
}

export async function deleteConversationPermanent(conversationId: string): Promise<void> {
  await request(`/api/v1/conversations/${encodeURIComponent(conversationId)}?permanent=true`, {
    method: "DELETE",
    headers: jsonHeaders,
  });
}

export async function restoreConversation(conversationId: string): Promise<Conversation> {
  const result = await request<{ restored: boolean; conversation: Conversation }>(
    `/api/v1/conversations/${encodeURIComponent(conversationId)}/restore`,
    {
      method: "POST",
      headers: jsonHeaders,
    }
  );
  return result.conversation;
}

export async function getArchivedConversations(): Promise<Conversation[]> {
  return request("/api/v1/conversations?include_archived=true");
}

export async function searchConversations(query: string, workspaceId?: string): Promise<ConversationSearchResult[]> {
  const params = new URLSearchParams({ q: query });
  if (workspaceId) params.set("workspace_id", workspaceId);
  return request(`/api/v1/conversations/search?${params.toString()}`);
}

export async function submitRun(conversationId: string, prompt: string, attachmentIds?: string[]): Promise<RunRecord> {
  const body: Record<string, unknown> = { prompt, context_profile: "chat", allow_all: true, print_timeout: "120m" };
  if (attachmentIds && attachmentIds.length > 0) {
    body.attachment_ids = attachmentIds;
  }
  return request(`/conversations/${encodeURIComponent(conversationId)}/runs`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify(body),
  });
}

export async function uploadAttachment(conversationId: string, file: File): Promise<AttachmentRecord> {
  const formData = new FormData();
  formData.append("file", file);
  const response = await fetch(
    applicationPath(`/api/v1/conversations/${encodeURIComponent(conversationId)}/attachments`),
    { method: "POST", body: formData, credentials: "include" },
  );
  if (!response.ok) {
    const detail = await response.text().catch(() => "upload failed");
    throw new Error(`${response.status}: ${detail || response.statusText}`);
  }
  return response.json() as Promise<AttachmentRecord>;
}

export async function getMessageAttachments(messageId: string): Promise<AttachmentRecord[]> {
  return request(`/api/v1/messages/${encodeURIComponent(messageId)}/attachments`);
}

export function attachmentDownloadUrl(attachmentId: string): string {
  return applicationPath(`/api/v1/attachments/${encodeURIComponent(attachmentId)}/download`);
}

export async function cancelRun(run: RunRecord): Promise<RunRecord> {
  return request(`/runs/${encodeURIComponent(run.id)}/cancel`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ expected_version: run.version }),
  });
}

export async function getTimeline(runId: string, after = 0, limit = 1000): Promise<{ run: RunRecord; events: PersistedEvent[]; has_more: boolean; next_after: number }> {
  return request(`/api/v1/runs/${encodeURIComponent(runId)}/timeline?after=${after}&limit=${limit}`);
}

export async function getRunArtifacts(runId: string): Promise<Artifact[]> {
  return request(`/api/v1/runs/${encodeURIComponent(runId)}/artifacts`);
}

export async function getArtifact(artifactId: string): Promise<Artifact> {
  return request(`/api/v1/artifacts/${encodeURIComponent(artifactId)}`);
}

export async function getRunContext(runId: string): Promise<Record<string, unknown> | null> {
  return requestOptional(`/api/v1/runs/${encodeURIComponent(runId)}/context`);
}

export async function getRunCapabilities(runId: string): Promise<Record<string, unknown> | null> {
  return requestOptional(`/api/v1/runs/${encodeURIComponent(runId)}/capabilities`);
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
  const base = `${protocol}//${window.location.host}${applicationPath("ws/control")}`;
  return `${base}?after=${encodeURIComponent(cursor)}`;
}


// Goals
export async function getGoals(conversationId?: string, status?: string): Promise<GoalRecord[]> {
  const query = new URLSearchParams();
  if (conversationId) query.set("conversation_id", conversationId);
  if (status) query.set("status", status);
  const qs = query.toString();
  return request(`/api/v1/goals${qs ? `?${qs}` : ""}`);
}

export async function createGoal(conversationId: string, objective: string, options?: { acceptance?: GoalRecord["acceptance"]; max_turns?: number }): Promise<GoalRecord> {
  return request("/api/v1/goals", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ conversation_id: conversationId, objective, ...options }),
  });
}

export async function getGoal(goalId: string): Promise<GoalRecord> {
  return request(`/api/v1/goals/${encodeURIComponent(goalId)}`);
}

export async function pauseGoal(goalId: string): Promise<GoalRecord> {
  return request(`/api/v1/goals/${encodeURIComponent(goalId)}/pause`, { method: "POST", headers: jsonHeaders, body: "{}" });
}

export async function resumeGoal(goalId: string): Promise<GoalRecord> {
  return request(`/api/v1/goals/${encodeURIComponent(goalId)}/resume`, { method: "POST", headers: jsonHeaders, body: "{}" });
}

export async function cancelGoal(goalId: string): Promise<GoalRecord> {
  return request(`/api/v1/goals/${encodeURIComponent(goalId)}/cancel`, { method: "POST", headers: jsonHeaders, body: "{}" });
}

export async function completeGoal(goalId: string): Promise<GoalRecord> {
  return request(`/api/v1/goals/${encodeURIComponent(goalId)}/complete`, { method: "POST", headers: jsonHeaders, body: "{}" });
}

export async function getGoalEvaluations(goalId: string): Promise<GoalEvaluation[]> {
  return request(`/api/v1/goals/${encodeURIComponent(goalId)}/evaluations`);
}



// ─── Learning Studio API ─────────────────────────────────────────────────────

import type { LearningOverview, LearningEvent, SkillProposal, LearnedSkill, SkillRevision, SkillRunEvent, LearningConfig, LearnResponse, JourneyGraph } from "./types";

export async function getLearningOverview(): Promise<LearningOverview> {
  return request("/api/learning/overview");
}

export async function getLearningEvents(limit = 50, after = 0): Promise<LearningEvent[]> {
  return request(`/api/learning/events?limit=${limit}&after=${after}`);
}

export async function getLearningProposals(status?: string): Promise<SkillProposal[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  return request(`/api/learning/proposals${query}`);
}

export async function getLearningProposal(id: string): Promise<SkillProposal> {
  return request(`/api/learning/proposals/${encodeURIComponent(id)}`);
}

export async function approveProposal(id: string, reason?: string): Promise<SkillProposal> {
  return request(`/api/learning/proposals/${encodeURIComponent(id)}/approve`, {
    method: "POST", headers: jsonHeaders, body: JSON.stringify({ reason: reason ?? null }),
  });
}

export async function rejectProposal(id: string, reason?: string): Promise<SkillProposal> {
  return request(`/api/learning/proposals/${encodeURIComponent(id)}/reject`, {
    method: "POST", headers: jsonHeaders, body: JSON.stringify({ reason: reason ?? null }),
  });
}

export async function getLearningSkills(state?: string, owner?: string): Promise<LearnedSkill[]> {
  const query = new URLSearchParams();
  if (state) query.set("state", state);
  if (owner) query.set("owner", owner);
  const qs = query.toString();
  return request(`/api/learning/skills${qs ? `?${qs}` : ""}`);
}

export async function getLearningSkill(id: string): Promise<LearnedSkill> {
  return request(`/api/learning/skills/${encodeURIComponent(id)}`);
}

export async function getLearningSkillRevisions(id: string): Promise<SkillRevision[]> {
  return request(`/api/learning/skills/${encodeURIComponent(id)}/revisions`);
}

export async function getLearningSkillRuns(id: string): Promise<SkillRunEvent[]> {
  return request(`/api/learning/skills/${encodeURIComponent(id)}/runs`);
}

export async function pinSkill(id: string): Promise<LearnedSkill> {
  return request(`/api/learning/skills/${encodeURIComponent(id)}/pin`, { method: "POST", headers: jsonHeaders, body: "{}" });
}

export async function unpinSkill(id: string): Promise<LearnedSkill> {
  return request(`/api/learning/skills/${encodeURIComponent(id)}/unpin`, { method: "POST", headers: jsonHeaders, body: "{}" });
}

export async function archiveSkill(id: string): Promise<LearnedSkill> {
  return request(`/api/learning/skills/${encodeURIComponent(id)}/archive`, { method: "POST", headers: jsonHeaders, body: "{}" });
}

export async function restoreSkill(id: string): Promise<LearnedSkill> {
  return request(`/api/learning/skills/${encodeURIComponent(id)}/restore`, { method: "POST", headers: jsonHeaders, body: "{}" });
}

export async function rollbackSkill(id: string, targetRevision: number, reason?: string): Promise<LearnedSkill> {
  return request(`/api/learning/skills/${encodeURIComponent(id)}/rollback`, {
    method: "POST", headers: jsonHeaders, body: JSON.stringify({ target_revision: targetRevision, reason: reason ?? "" }),
  });
}

export async function getLearningMemory(limit = 200): Promise<Array<Record<string, unknown>>> {
  return request(`/api/learning/memory?limit=${limit}`);
}

export async function updateLearningMemory(id: string, content: string): Promise<Record<string, unknown>> {
  return request(`/api/learning/memory/${encodeURIComponent(id)}`, {
    method: "PATCH", headers: jsonHeaders, body: JSON.stringify({ content }),
  });
}

export async function deleteLearningMemory(id: string): Promise<{ deleted: boolean; id: string }> {
  return request(`/api/learning/memory/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function getLearningSettings(): Promise<LearningConfig> {
  return request("/api/learning/settings");
}

export async function updateLearningSettings(settings: Record<string, unknown>): Promise<Record<string, unknown>> {
  return request("/api/learning/settings", {
    method: "PATCH", headers: jsonHeaders, body: JSON.stringify(settings),
  });
}

export async function submitLearn(source: string, options?: { skill_name?: string; force_new?: boolean }): Promise<LearnResponse> {
  return request("/api/learning/learn", {
    method: "POST", headers: jsonHeaders, body: JSON.stringify({ source, ...options }),
  });
}

export async function getMemoryCandidates(status?: string, limit = 50): Promise<MemoryCandidate[]> {
  const query = status ? `?status=${encodeURIComponent(status)}&limit=${limit}` : `?limit=${limit}`;
  return request(`/api/learning/memory-candidates${query}`);
}

export async function promoteMemoryCandidate(candidateId: string): Promise<{ promoted: boolean; memory_id: string; content: string }> {
  return request(`/api/learning/memory-candidates/${encodeURIComponent(candidateId)}/promote`, {
    method: "POST", headers: jsonHeaders,
  });
}

export async function dismissMemoryCandidate(candidateId: string): Promise<{ dismissed: boolean }> {
  return request(`/api/learning/memory-candidates/${encodeURIComponent(candidateId)}/dismiss`, {
    method: "POST", headers: jsonHeaders,
  });
}

export async function scanMemoryCandidates(): Promise<{ status: string; candidates_discovered: number }> {
  return request("/api/learning/memory-candidates/scan", {
    method: "POST", headers: jsonHeaders,
  });
}

// ─── Journey Graph API ───────────────────────────────────────────────────────

export async function getLearningJourney(skillId?: string): Promise<JourneyGraph> {
  const query = skillId ? `?skill_id=${encodeURIComponent(skillId)}` : "";
  return request(`/api/learning/journey${query}`);
}

// ─── Context Transparency API (Phase 4C) ────────────────────────────────────

export async function getRunContextSnapshot(runId: string): Promise<ContextSnapshot | null> {
  return requestOptional(`/api/v1/runs/${encodeURIComponent(runId)}/context-snapshot`);
}

export async function saveRunContextSnapshot(runId: string, snapshot: Record<string, unknown>): Promise<{ status: string; run_id: string }> {
  return request(`/api/v1/runs/${encodeURIComponent(runId)}/context-snapshot`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify(snapshot),
  });
}

// ─── TaskFlow & Kanban API ──────────────────────────────────────────────────

export async function listTaskFlows(workspaceId?: string, status?: string): Promise<TaskFlow[]> {
  const params = new URLSearchParams();
  if (workspaceId) params.append("workspace_id", workspaceId);
  if (status) params.append("status", status);
  const query = params.toString() ? `?${params.toString()}` : "";
  return request(`/api/taskflows${query}`);
}

export async function getTaskFlow(flowId: string): Promise<TaskFlow> {
  return request(`/api/taskflows/${encodeURIComponent(flowId)}`);
}

export async function createTaskFlow(payload: {
  title: string;
  objective: string;
  workspace_id: string;
  context_profile?: string;
  state_json?: Record<string, any>;
}): Promise<TaskFlow> {
  return request("/api/taskflows", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify(payload),
  });
}

export async function updateTaskFlow(
  flowId: string,
  payload: {
    title?: string;
    objective?: string;
    status?: string;
    context_profile?: string;
    state_json?: Record<string, any>;
    expected_version?: number;
  }
): Promise<TaskFlow> {
  return request(`/api/taskflows/${encodeURIComponent(flowId)}`, {
    method: "PATCH",
    headers: jsonHeaders,
    body: JSON.stringify(payload),
  });
}

export async function deleteTaskFlow(flowId: string): Promise<{ deleted: boolean }> {
  return request(`/api/taskflows/${encodeURIComponent(flowId)}`, {
    method: "DELETE",
  });
}

export async function dispatchTaskFlow(flowId: string): Promise<Record<string, any>> {
  return request(`/api/taskflows/${encodeURIComponent(flowId)}/dispatch`, {
    method: "POST",
    headers: jsonHeaders,
  });
}

export async function listFlowTasks(flowId: string, status?: string): Promise<FlowTask[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  return request(`/api/taskflows/${encodeURIComponent(flowId)}/tasks${query}`);
}

export async function createFlowTask(
  flowId: string,
  payload: {
    title: string;
    body?: string;
    workspace_id?: string;
    acceptance_criteria?: Array<string | { text?: string; criterion?: string }>;
    priority?: string;
    assignee_profile?: string;
    idempotency_key?: string;
    max_attempts?: number;
    parent_ids?: string[];
  }
): Promise<FlowTask> {
  return request(`/api/taskflows/${encodeURIComponent(flowId)}/tasks`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ flow_id: flowId, ...payload }),
  });
}

export async function getFlowTask(taskId: string): Promise<FlowTask> {
  return request(`/api/flow-tasks/${encodeURIComponent(taskId)}`);
}

export async function updateFlowTask(
  taskId: string,
  payload: {
    title?: string;
    body?: string;
    acceptance_criteria?: Array<string | { text?: string; criterion?: string }>;
    status?: string;
    priority?: string;
    assignee_profile?: string;
    max_attempts?: number;
    expected_version?: number;
  }
): Promise<FlowTask> {
  return request(`/api/flow-tasks/${encodeURIComponent(taskId)}`, {
    method: "PATCH",
    headers: jsonHeaders,
    body: JSON.stringify(payload),
  });
}

export async function deleteFlowTask(taskId: string): Promise<{ deleted: boolean }> {
  return request(`/api/flow-tasks/${encodeURIComponent(taskId)}`, {
    method: "DELETE",
  });
}

export async function addTaskDependency(
  taskId: string,
  parentTaskId: string
): Promise<FlowTask> {
  return request(`/api/flow-tasks/${encodeURIComponent(taskId)}/dependencies`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ parent_task_id: parentTaskId }),
  });
}

export async function removeTaskDependency(
  taskId: string,
  parentTaskId: string
): Promise<FlowTask> {
  return request(
    `/api/flow-tasks/${encodeURIComponent(taskId)}/dependencies/${encodeURIComponent(parentTaskId)}`,
    {
      method: "DELETE",
    }
  );
}

export async function listTaskComments(taskId: string): Promise<TaskComment[]> {
  return request(`/api/flow-tasks/${encodeURIComponent(taskId)}/comments`);
}

export async function addTaskComment(
  taskId: string,
  body: string,
  authorType: "user" | "agent" | "system" = "user",
  authorId: string = "user"
): Promise<TaskComment> {
  return request(`/api/flow-tasks/${encodeURIComponent(taskId)}/comments`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ body, author_type: authorType, author_id: authorId }),
  });
}

export async function blockTask(
  taskId: string,
  reason: string,
  detail?: string
): Promise<FlowTask> {
  return request(`/api/flow-tasks/${encodeURIComponent(taskId)}/block`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ reason, detail }),
  });
}

export async function unblockTask(taskId: string, comment?: string): Promise<FlowTask> {
  return request(`/api/flow-tasks/${encodeURIComponent(taskId)}/unblock`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ comment }),
  });
}

export async function retryTask(taskId: string, comment?: string): Promise<FlowTask> {
  return request(`/api/flow-tasks/${encodeURIComponent(taskId)}/retry`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ comment }),
  });
}

export async function listTaskAttempts(taskId: string): Promise<TaskAttempt[]> {
  return request(`/api/flow-tasks/${encodeURIComponent(taskId)}/attempts`);
}

export async function getTaskHandoffs(taskId: string): Promise<TaskHandoffItem[]> {
  return request(`/api/flow-tasks/${encodeURIComponent(taskId)}/handoffs`);
}

export async function getCuratorStatus(): Promise<CuratorStatus> {
  return request(`/api/memory/curator/status`);
}

export async function updateCuratorSettings(
  mode: "manual" | "assisted" | "automatic"
): Promise<{ status: string; mode: string }> {
  return request(`/api/memory/curator/settings`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ mode }),
  });
}

export async function consolidateJournals(
  daysBack: number = 7
): Promise<ConsolidationReport> {
  return request(`/api/memory/curator/consolidate?days_back=${daysBack}`, {
    method: "POST",
  });
}

export async function rememberExplicit(
  content: string,
  category: string = "user_preference",
  conversationId?: string,
  runId?: string
): Promise<Record<string, unknown>> {
  return request(`/api/memory/remember`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({
      content,
      category,
      conversation_id: conversationId,
      run_id: runId,
    }),
  });
}

export async function getMemoryRevisions(
  memoryId?: string
): Promise<MemoryRevision[]> {
  const path = memoryId
    ? `/api/memory/${encodeURIComponent(memoryId)}/revisions`
    : `/api/memory/revisions`;
  return request(path);
}

export async function getRunProgress(runId: string): Promise<ProgressSnapshot> {
  return request<ProgressSnapshot>(`api/v1/runs/${runId}/progress`);
}

export async function getRunTelemetry(runId: string, sinceSequence = 0): Promise<TelemetryEvent[]> {
  return request<TelemetryEvent[]>(`api/v1/runs/${runId}/telemetry?since_sequence=${sinceSequence}`);
}
