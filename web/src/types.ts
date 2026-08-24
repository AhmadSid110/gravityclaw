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
  requested_model?: string | null;
  resolved_model?: string | null;
  agy_version?: string | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  version: number;
}

export interface ConversationModelPolicy {
  mode: "default" | "explicit";
  model: string | null;
  global_default: string | null;
}

export interface Conversation {
  id: string;
  workspace_id: string;
  channel: string;
  channel_key: string | null;
  title: string | null;
  kind?: "main" | "normal";
  archived_at?: string | null;
  agy_conversation_id: string | null;
  model_override?: string | null;
  model_policy?: ConversationModelPolicy;
  created_at: string;
  updated_at: string;
}

export interface ConversationSearchResult {
  conversation_id: string;
  title: string;
  kind: string;
  message_id: string;
  role: string;
  content: string;
  created_at: string;
}

export interface Message {
  id: string;
  conversation_id: string;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  created_at: string;
  source_run_id: string | null;
  attachments?: AttachmentRecord[];
}

export interface AttachmentRecord {
  id: string;
  workspace_id: string;
  conversation_id: string;
  message_id: string | null;
  filename: string;
  mime_type: string;
  kind: "image" | "audio" | "video" | "document" | "archive" | "other";
  size_bytes: number;
  sha256: string;
  source: "web" | "telegram" | "api" | "agent";
  width: number | null;
  height: number | null;
  duration_ms: number | null;
  state: "queued" | "ready" | "failed";
  created_at: string;
}

export interface ConversationDetail {
  conversation: Conversation;
  messages: Message[];
  runs: RunRecord[];
}

export type ActivityKind = "command" | "file" | "edit" | "search" | "subagent" | "other";

export interface NormalizedActivity {
  id: string;
  kind: ActivityKind;
  tool: string;
  state: "queued" | "running" | "finished" | "failed" | "cancelled" | "soft-denied";
  title: string;
  detail: string;
  command?: string;
  cwd?: string;
  path?: string;
  lines?: string;
  query?: string;
  output?: string;
  error?: string;
  durationSeconds?: number;
  sequence: number;
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


export interface ProgressStep {
  key: string;
  label: string;
  state: "pending" | "active" | "completed" | "failed";
  started_at?: string | null;
  completed_at?: string | null;
  detail?: string | null;
}

export interface ProgressCounters {
  tool_calls: number;
  commands: number;
  files_read: number;
  files_modified: number;
  output_lines: number;
  output_bytes: number;
}

export interface ProgressSnapshot {
  run_id: string;
  status: RunStatus;
  current_label?: string | null;
  current_detail?: string | null;
  active_operation_id?: string | null;
  active_operation_kind?: string | null;
  started_at: string;
  last_activity_at: string;
  last_output_at?: string | null;
  last_progress_at?: string | null;
  completed_steps: ProgressStep[];
  active_step?: ProgressStep | null;
  pending_steps: ProgressStep[];
  recent_output_tail: string[];
  counters: ProgressCounters;
  version: number;
}

export interface TelemetryEvent {
  id?: number;
  event_id: string;
  run_id: string;
  type: string;
  timestamp: string;
  source: string;
  operation_id?: string | null;
  parent_operation_id?: string | null;
  sequence: number;
  tool?: string | null;
  data: Record<string, any>;
}

export interface PresentationState {
  runId: string;
  status: RunStatus;
  assistantText: string;
  currentTool: ToolActivity | null;
  completedTools: ToolActivity[];
  currentActivity: NormalizedActivity | null;
  completedActivities: NormalizedActivity[];
  subagents: string[];
  currentTaskSummary?: string;
  progress?: ProgressSnapshot | null;
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

export interface WorkspaceRecord {
  id: string;
  name: string;
  path: string;
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

export interface MemoryRevision {
  id: string;
  memory_id: string;
  revision: number;
  previous_content: string;
  new_content: string;
  superseded_at: string;
  source_run_id: string | null;
  source_conversation_id: string | null;
  reason: string;
}

export interface CuratorStatus {
  mode: "manual" | "assisted" | "automatic";
  enabled: boolean;
  status: string;
  stats: {
    curated_memories: number;
    episodic_memories: number;
    total_revisions: number;
    pending_candidates: number;
    daily_journals: number;
  };
}

export interface ConsolidationReport {
  started_at: string;
  completed_at: string;
  journals_scanned: number;
  entries_analyzed: number;
  candidates_discovered: number;
  memories_promoted: number;
  memories_superseded: number;
  summary: string;
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

export interface ModelCapability {
  id: string;
  label: string;
}

export interface ModelCatalog {
  models: ModelCapability[];
  default_model: string | null;
  agy_version: string;
  binary?: string;
  source?: string;
  effort_levels?: string[];
}

export interface ConversationEffort {
  conversation_id: string;
  effort: string | null;
}

export interface UsageSummary {
  period_days: number;
  total_runs: number;
  completed_runs: number;
  total_tokens_used: number;
  total_budget_tokens: number;
  models: Array<{ model: string; runs: number; tokens: number }>;
}

export interface AgyQuotaPool {
  pool: string;
  remaining_fraction: number | null;
  remaining_percent: number | null;
  reset_time: string | null;
  models: Array<{ id: string; label: string }>;
}

export interface AgyQuotaPlan {
  name: string;
  tier_id: string | null;
  available_prompt_credits: number | null;
  available_flow_credits: number | null;
}

export interface AgyQuota {
  available: boolean;
  error?: string;
  plan?: AgyQuotaPlan;
  pools?: AgyQuotaPool[];
  models?: Array<{
    id: string;
    label: string;
    remaining_fraction: number | null;
    remaining_percent: number | null;
    reset_time: string | null;
  }>;
  window?: string;
  note?: string;
}

export interface ConversationModel {
  conversation_id: string;
  model_policy: "default" | "explicit";
  requested_model: string | null;
  resolved_model: string | null;
  global_default_model: string | null;
}

export interface ContextStatus {
  run_id: string | null;
  state: "current" | "last" | "none";
  status?: RunStatus;
  model?: string | null;
  context_profile?: string | null;
  used_tokens: number;
  budget_tokens: number;
  percent: number;
  generation_reserve: number;
  last_compaction: string | null;
  messages_compacted: number;
  compactions_count?: number;
  conversation_total_tokens?: number;
  breakdown: Record<string, number>;
  conversation: { total?: number; summary?: number; recent?: number };
  conversation_turns?: {
    total: number;
    summary_range: [number, number] | null;
    recent_range: [number, number] | null;
    watermark_turn: number;
  } | null;
  watermark_turn?: number | null;
  memory_items: Array<{ label: string; tokens: number; confidence: number | null; included: boolean }>;
  memory_excluded?: number;
  recent: Array<{
    run_id: string;
    status: RunStatus;
    state: "current" | "last";
    used_tokens: number;
    budget_tokens: number;
    percent: number;
    created_at: string;
  }>;
  manifest?: ContextManifest;
}

export type TriggerState = "PENDING" | "CLAIMED" | "DISPATCHED" | "RUNNING" | "COMPLETED" | "SKIPPED" | "MISSED" | "FAILED" | "CANCELLED";

export interface TriggerRecord {
  id: string;
  execution_key: string;
  schedule_id: string;
  generation: number;
  scheduled_for: string;
  state: TriggerState;
  run_id: string | null;
  decision_reason: string | null;
  attempt_count: number;
  created_at: string;
  claimed_at: string | null;
  dispatched_at: string | null;
  finished_at: string | null;
  updated_at: string;
}

export interface ScheduleRecord {
  id: string;
  name: string;
  enabled: boolean;
  trigger_type: "one_shot" | "interval" | "cron" | "heartbeat";
  expression: string;
  timezone: string;
  prompt: string;
  context_profile: string;
  workspace_id: string;
  conversation_policy: "new" | "resume";
  concurrency_policy: "SKIP" | "QUEUE" | "REPLACE";
  misfire_policy: "MISFIRE_SKIP" | "MISFIRE_RUN_ONCE" | "MISFIRE_CATCH_UP";
  misfire_grace_seconds: number;
  notification_policy: "silent" | "actionable";
  notification_channel: string | null;
  notification_chat_id: string | null;
  generation: number;
  version: number;
  next_run_at: string | null;
  last_run_at: string | null;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
  triggers?: TriggerRecord[];
}

export interface SkillRecord {
  id: string;
  name: string;
  path: string;
  scope: "global" | "workspace";
  workspace_id: string | null;
  enabled: boolean;
  profiles: string[];
  sha256: string;
  version: string;
  validation_state: string;
  validation_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface MCPRecord {
  id: string;
  name: string;
  transport: string;
  command: string | null;
  url: string | null;
  args: string[];
  env_refs: Record<string, string>;
  enabled: boolean;
  scope: "global" | "workspace";
  workspace_id: string | null;
  config_hash: string;
  health_state: "UNKNOWN" | "HEALTHY" | "DEGRADED" | "UNAVAILABLE" | "MISCONFIGURED";
  health_error: string | null;
  last_checked_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface CapabilitySnapshotRecord {
  run_id: string;
  workspace_id: string;
  profile: string;
  manifest: Record<string, unknown>;
  manifest_hash: string;
  created_at: string;
}

export interface CapabilityState {
  workspace: { id: string; name: string; path: string } | null;
  profile: string;
  isolation: Record<string, string>;
  skills: SkillRecord[];
  mcp: MCPRecord[];
  bindings: Array<Record<string, unknown>>;
  snapshots: CapabilitySnapshotRecord[];
}


// ─── Learning Studio types ──────────────────────────────────────────────────

export interface LearningOverview {
  enabled: boolean;
  mode?: "suggest" | "automatic" | "off";
  trust_mode: string;
  learning_engine_status?: "active" | "idle" | "healthy";
  skill_registry_status?: string;
  memory_index_status?: string;
  last_scan_at?: string | null;
  stats: {
    memories: number;
    curated_memories?: number;
    episodic_memories?: number;
    memory_candidates?: number;
    skills: number;
    pending_proposals: number;
    daily_journals?: number;
    total_messages?: number;
    success_rate: number | null;
    corrections: number;
  };
  curator: {
    enabled: boolean;
    schedule: string;
    timezone: string;
    last_run_at: string | null;
    last_report: Record<string, unknown> | null;
  };
}

export interface MemoryCandidate {
  id: string;
  key: string;
  namespace: "agent" | "user";
  category: "project decision" | "user preference" | "architecture rule" | "operational fact";
  content: string;
  confidence: number;
  source_run_id: string | null;
  source_conversation_id: string | null;
  status: "pending_approval" | "applied" | "rejected";
  reviewer_model: string;
  created_at: string;
}

export interface LearningEvent {
  id: number;
  actor: string;
  action: string;
  resource_type: string;
  resource_id: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface SkillProposal {
  id: string;
  skill_id: string | null;
  skill_name: string;
  operation: string;
  description: string;
  reason: string;
  confidence: number;
  content: string;
  before: string | null;
  base_revision: number | null;
  source_run_id: string | null;
  review_model: string | null;
  status: "pending" | "approved" | "rejected" | "expired" | "conflict";
  status_reason: string | null;
  created_at: string;
  resolved_at: string | null;
}

export interface LearnedSkill {
  skill_id: string;
  name: string;
  description: string;
  path: string;
  owner: "user" | "agent" | "bundled";
  state: "active" | "stale" | "archived";
  trust: "unreviewed" | "approved";
  revision: number;
  pinned: boolean;
  created_at: string;
  updated_at: string;
  content?: string;
  stats?: {
    matched: number;
    selected: number;
    loaded: number;
    executed: number;
    successful: number;
    failed: number;
    corrected: number;
    success_rate: number | null;
  };
}

export interface SkillRevision {
  id: string;
  skill_id: string;
  revision: number;
  parent_revision: number | null;
  operation: string;
  source_run_id: string | null;
  proposal_id: string | null;
  model: string | null;
  reason: string;
  created_at: string;
}

export interface SkillRunEvent {
  run_id: string;
  event: string;
  created_at: string;
}

export interface LearningConfig {
  enabled: boolean;
  memory_approval_required: boolean;
  reviewer: {
    enabled: boolean;
    provider: string;
    model: string;
    fallback_to_primary: boolean;
    max_input_tokens: number;
    max_output_tokens: number;
    max_retries: number;
  };
  skills: {
    trust_mode: string;
    min_confidence: number;
    create_approval_required: boolean;
    modify_approval_required: boolean;
  };
  ingestion: {
    small_source_token_limit: number;
    chunk_tokens: number;
    max_chunks: number;
  };
  curator: {
    enabled: boolean;
    schedule: string;
    timezone: string;
    min_idle_hours: number;
    stale_after_days: number;
    archive_after_days: number;
    minimum_invocations: number;
    utility_stale_threshold: number;
    utility_archive_threshold: number;
  };
  notifications: {
    mode: string;
  };
}

export interface LearnResponse {
  request_id: string;
  status: "success" | "duplicate" | "failed" | "pending_approval" | "disabled";
  message: string;
  proposal_id?: string;
  skill_name?: string;
  source_type?: string;
  chunks_processed?: number;
  warnings?: string[];
}

// ─── Journey Graph types ────────────────────────────────────────────────────

export type JourneyNodeKind = "skill" | "revision" | "proposal" | "run";

export type JourneyEdgeRelation =
  | "produces"
  | "evolves_to"
  | "triggers_creation"
  | "triggers_improvement"
  | "approved_as"
  | "generates_proposal"
  | "targets"
  | "used_in"
  | "validates"
  | "fails_with"
  | "corrects"
  | "proposes_change";

export interface JourneyNode {
  id: string;
  kind: JourneyNodeKind;
  label: string;
  // Common metadata
  created_at?: string;
  // Skill-specific
  description?: string;
  state?: string;
  revision?: number;
  trust?: string;
  owner?: string;
  // Revision-specific
  skill_id?: string;
  parent_revision?: number | null;
  operation?: string;
  reason?: string;
  // Proposal-specific
  skill_name?: string;
  status?: string;
  confidence?: number;
  resolved_at?: string | null;
  // Run-specific
  run_id?: string;
}

export interface JourneyEdge {
  source: string;
  target: string;
  relation: JourneyEdgeRelation;
}

export interface JourneyGraph {
  nodes: JourneyNode[];
  edges: JourneyEdge[];
  stats: {
    total_nodes: number;
    total_edges: number;
    by_kind: Record<string, number>;
  };
}

export type GoalStatus = "active" | "paused" | "completed" | "cancelled" | "failed";
export type GoalVerdict = "continue" | "done" | "failed" | "paused";

export interface AcceptanceCriterion {
  type: "command" | "file_exists" | "test";
  description?: string;
  command?: string;
  path?: string;
  passed?: boolean;
  detail?: string;
}

export interface GoalRecord {
  id: string;
  conversation_id: string;
  objective: string;
  acceptance: AcceptanceCriterion[];
  status: GoalStatus;
  max_turns: number;
  turns_used: number;
  current_step: string | null;
  last_run_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface GoalEvaluation {
  id: string;
  goal_id: string;
  run_id: string | null;
  turn_number: number;
  verdict: GoalVerdict;
  reason: string | null;
  acceptance_state: AcceptanceCriterion[];
  created_at: string;
}

// ─── Context Transparency types (Phase 4C) ──────────────────────────────────

export type TokenSource = "provider" | "tokenizer" | "estimated";

export interface ContextSegment {
  kind: string;
  tokens: number;
}

export interface ContextSkillEntry {
  skill_id: string;
  name: string;
  revision?: number;
  tokens: number;
  sha256?: string;
}

export interface ContextMemoryEntry {
  id: string;
  namespace: string;
  tokens: number;
  label?: string;
  confidence?: number | null;
}

export interface ContextTransformation {
  label: string;
  tokens_before: number;
  tokens_after: number;
}

export interface ContextSnapshot {
  run_id: string;
  model: string;
  context_limit: number;
  input_tokens: number;
  output_tokens: number | null;
  token_source: TokenSource;
  segments: ContextSegment[];
  skills: ContextSkillEntry[];
  memories: ContextMemoryEntry[];
  transformations: ContextTransformation[] | null;
  conversation_tokens: number | null;
  last_invocation_tokens: number | null;
  created_at: string;
  // Computed enrichments
  usage_ratio: number;
  remaining_tokens: number;
  run_status: RunStatus;
  is_final: boolean;
  is_estimated: boolean;
}

// ─── TaskFlow & Kanban Types ──────────────────────────────────────────

export type TaskFlowView = "board" | "timeline" | "dependencies" | "activity";
export type InspectorTab = "overview" | "activity" | "runs" | "comments" | "artifacts";
export type DensityMode = "comfortable" | "compact";

export type TaskFlowStatus =
  | "QUEUED"
  | "RUNNING"
  | "WAITING"
  | "BLOCKED"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED";

export type FlowTaskStatus =
  | "TRIAGE"
  | "TODO"
  | "READY"
  | "RUNNING"
  | "BLOCKED"
  | "DONE"
  | "FAILED"
  | "CANCELLED"
  | "ARCHIVED";

export type TaskPriority = "LOW" | "MEDIUM" | "HIGH" | "URGENT";

export type BlockReason =
  | "dependency"
  | "needs_user_input"
  | "missing_capability"
  | "transient_failure"
  | "external_service"
  | "review_required";

export interface TaskFlowStats {
  total_tasks: number;
  done_tasks: number;
  running_tasks: number;
  ready_tasks: number;
  todo_tasks: number;
  triage_tasks: number;
  blocked_tasks: number;
  failed_tasks: number;
}

export interface TaskFlow {
  id: string;
  title: string;
  objective: string;
  status: TaskFlowStatus;
  workspace_id: string;
  context_profile: string;
  state_json: Record<string, any>;
  version: number;
  created_at: string;
  updated_at: string;
  stats?: TaskFlowStats;
}

export interface TaskAttempt {
  id: string;
  task_id: string;
  run_id: string;
  attempt_no: number;
  started_at: string;
  finished_at: string | null;
  outcome: string | null;
  summary: string | null;
}

export interface TaskComment {
  id: string;
  task_id: string;
  author_type: "user" | "agent" | "system";
  author_id: string;
  body: string;
  created_at: string;
}

export interface TaskClaim {
  task_id: string;
  owner: string;
  lease_until: string;
  heartbeat_at: string;
  heartbeat_message: string | null;
}

export interface FlowTask {
  id: string;
  flow_id: string;
  title: string;
  body: string;
  acceptance_criteria: Array<string | { text?: string; criterion?: string }>;
  status: FlowTaskStatus;
  assignee_profile: string;
  priority: TaskPriority;
  workspace_id: string;
  idempotency_key: string | null;
  max_attempts: number;
  block_reason: BlockReason | null;
  block_detail: string | null;
  block_recurrence_count: number;
  version: number;
  created_at: string;
  updated_at: string;
  parent_ids: string[];
  child_ids: string[];
  attempt_count?: number;
  latest_attempt?: TaskAttempt | null;
  comment_count?: number;
  claim?: TaskClaim | null;
}

export interface TaskHandoffItem {
  parent_task: FlowTask;
  comments: TaskComment[];
}
