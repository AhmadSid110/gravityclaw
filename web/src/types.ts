export type RunStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted"
  | "orphaned";

export interface RunRecord {
  id: string;
  conversation_id: string;
  status: RunStatus;
  backend: string;
  backend_conversation_id: string | null;
  worker_id: string | null;
  request: { prompt?: string; context_profile?: string; [key: string]: unknown };
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  version: number;
}

export interface Conversation {
  id: string;
  workspace_id: string;
  channel: string;
  channel_key: string | null;
  title: string | null;
  agy_conversation_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface Message {
  id: string;
  conversation_id: string;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  created_at: string;
  source_run_id: string | null;
}

export interface ConversationDetail {
  conversation: Conversation;
  messages: Message[];
  runs: RunRecord[];
}

export interface ToolActivity {
  id: string;
  name: string;
  state: "queued" | "running" | "finished" | "failed" | "cancelled" | "soft-denied";
  detail: string;
  sequence?: number;
  durationMs?: number;
  output?: string;
}

export interface PresentationState {
  runId: string;
  status: RunStatus;
  assistantText: string;
  currentTool: ToolActivity | null;
  completedTools: ToolActivity[];
  subagents: string[];
}

export interface Artifact {
  id: string;
  run_id: string;
  conversation_id: string;
  kind: string;
  content: string | null;
  excerpt: string;
  summary: string;
  sha256: string;
  characters: number;
  relevance: number;
  created_at: string;
}

export interface SubagentNode {
  id: string;
  label: string;
  state: "queued" | "running" | "completed" | "failed" | "cancelled" | "unknown";
  parentId: string | null;
  conversationId: string | null;
  tools: number;
  tokens: number | null;
  latest: string;
  sequence: number;
}

export interface RunInspection {
  run: RunRecord;
  events: PersistedEvent[];
  tools: ToolActivity[];
  subagents: SubagentNode[];
  artifacts: Artifact[];
  context: Record<string, unknown> | null;
  capabilities: Record<string, unknown> | null;
}

export interface PersistedEvent {
  id: number;
  run_id: string;
  sequence: number;
  event_type: string;
  conversation_id: string | null;
  payload: Record<string, unknown>;
  raw: Record<string, unknown> | null;
  created_at: string;
}

export interface ControlSnapshot {
  api_version: string;
  health: { status: string; mode: string; telegram: { enabled: boolean }; auth: { enabled: boolean } };
  counts: { runs: number; active_runs: number; queued_runs: number; schedules: number };
  active_runs: RunRecord[];
  next_schedules: Array<Record<string, unknown>>;
  activity?: Array<{ cursor: number; event: PersistedEvent }>;
}

export interface ControlState {
  snapshot: ControlSnapshot | null;
  activeRuns: RunRecord[];
  activity: PersistedEvent[];
  cursor: number;
  connection: "connecting" | "connected" | "reconnecting" | "offline";
  error: string | null;
}

export interface IdentityDocument {
  name: string;
  content: string;
  sha256: string;
  version: number;
  updated_at?: string;
}

export interface MemoryRecord {
  id: string;
  kind: "episodic" | "curated" | "fact";
  content: string;
  source: string;
  source_conversation_id: string | null;
  confidence: number;
  created_at: string;
  updated_at: string;
  rank?: number;
}

export interface MemoryUsage extends MemoryRecord {
  usage: Array<Record<string, unknown>>;
}

export interface JournalRecord {
  date: string;
  name?: string;
  content?: string;
  characters?: number;
  sha256: string;
  updated_at?: string;
}

export interface ContextManifest {
  version?: number;
  profile?: string;
  lifecycle?: string;
  characters?: number;
  estimated_tokens?: number;
  budget_tokens?: number;
  prompt_sha256?: string;
  identity_fingerprint?: string;
  context_fingerprint?: string;
  included_sources?: string[];
  omitted_sources?: string[];
  invalidated_sources?: string[];
  sources?: Array<{
    label: string;
    category: string;
    trust: string;
    tier: number;
    priority: number;
    estimated_tokens: number;
    sha256: string | null;
    provenance: string | null;
    confidence: number | null;
    included: boolean;
    exclusion_reason: string | null;
  }>;
  [key: string]: unknown;
}

export interface ContextPreview {
  manifest: ContextManifest;
  preview: boolean;
  prompt_characters: number;
}
