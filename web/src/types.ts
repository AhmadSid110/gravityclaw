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
