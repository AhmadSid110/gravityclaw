import { useEffect, useRef, useState } from "react";
import { controlSocketUrl, getHome } from "./api";
import type { ActivityKind, ControlSnapshot, ControlState, NormalizedActivity, PersistedEvent, PresentationState, RunRecord, ToolActivity } from "./types";

const MAX_ACTIVITY = 120;

function reduceRun(runs: RunRecord[], event: PersistedEvent): RunRecord[] {
  if (!event.event_type.startsWith("run.")) return runs;
  const status = event.event_type.slice(4) as RunRecord["status"];
  if (!["queued", "running", "completed", "failed", "cancelled", "interrupted", "orphaned"].includes(status)) {
    return runs;
  }
  return runs.map((run) => run.id === event.run_id ? {
    ...run,
    status,
    error: typeof event.payload.error === "string" ? event.payload.error : run.error,
    version: run.version + 1,
  } : run);
}

function initialState(): ControlState {
  return {
    snapshot: null,
    activeRuns: [],
    activity: [],
    cursor: 0,
    connection: "offline",
    error: null,
  };
}

export function normalizeToolActivity(event: PersistedEvent): NormalizedActivity {
  const payload = event.payload ?? {};
  const toolName = String(payload.tool_name ?? payload.tool ?? "tool");
  const toolInfo = (typeof payload.tool_info === "object" && payload.tool_info !== null) ? payload.tool_info as Record<string, unknown> : {};
  const params = (typeof toolInfo.parameters === "object" && toolInfo.parameters !== null) ? toolInfo.parameters as Record<string, unknown> : {};
  const output = typeof toolInfo.output === "string" ? toolInfo.output : typeof payload.output === "string" ? payload.output : undefined;
  const errorObj = toolInfo.error ?? payload.error;
  const error = typeof errorObj === "string" ? errorObj : typeof errorObj === "object" && errorObj !== null ? String((errorObj as Record<string, unknown>).message ?? JSON.stringify(errorObj)) : undefined;
  const durationSeconds = typeof payload.duration_seconds === "number" ? payload.duration_seconds : undefined;
  const state: NormalizedActivity["state"] = event.event_type === "tool.started" ? "running" : event.event_type === "tool.finished" ? "finished" : "failed";

  let kind: ActivityKind = "other";
  const explicitTitle = params.toolAction ? String(params.toolAction) : params.toolSummary ? String(params.toolSummary) : undefined;
  let title = explicitTitle || toolName;
  let detail = "";
  let command: string | undefined;
  let cwd: string | undefined;
  let path: string | undefined;
  let lines: string | undefined;
  let query: string | undefined;

  if (toolName === "run_command") {
    kind = "command";
    command = String(params.CommandLine ?? "");
    cwd = params.Cwd ? String(params.Cwd) : undefined;
    if (!explicitTitle) {
      const trimmed = command.trim();
      const firstWord = trimmed.split(/\s+/)[0] || "";
      if (trimmed.startsWith("find ") || trimmed.includes("find /")) {
        title = "Searching filesystem";
      } else if (trimmed.startsWith("pytest") || trimmed.includes("pytest")) {
        title = "Running backend test suite";
      } else if (trimmed.startsWith("npm ") || trimmed.includes("vite")) {
        title = "Building frontend assets";
      } else if (trimmed.startsWith("curl ") || trimmed.startsWith("wget ")) {
        title = "Testing network endpoint";
      } else if (trimmed.startsWith("systemctl ") || trimmed.startsWith("service ")) {
        title = "Managing system service";
      } else if (trimmed.startsWith("git ")) {
        title = `Git ${trimmed.split(/\s+/)[1] || "operation"}`;
      } else {
        title = `Execute ${firstWord || "command"}`;
      }
    }
    detail = command;
  } else if (toolName === "view_file" || toolName === "read_file" || toolName === "read_url_content") {
    kind = "file";
    path = String(params.AbsolutePath ?? params.Url ?? params.TargetFile ?? "");
    const startLine = params.StartLine;
    const endLine = params.EndLine;
    lines = (startLine !== undefined && endLine !== undefined) ? `Lines ${startLine}–${endLine}` : undefined;
    const fileName = path.split("/").pop() || path;
    if (!explicitTitle) {
      title = `Read ${fileName}`;
    }
    detail = `${path}${lines ? ` (${lines})` : ""}`;
  } else if (toolName === "replace_file_content" || toolName === "write_to_file" || toolName === "edit_file") {
    kind = "edit";
    path = String(params.TargetFile ?? params.AbsolutePath ?? "");
    const fileName = path.split("/").pop() || path;
    const actionDesc = String(params.Description || params.Instruction || (toolName === "write_to_file" ? "Created file" : "Modified file"));
    if (!explicitTitle) {
      title = `${toolName === "write_to_file" ? "Create" : "Update"} ${fileName}`;
    }
    detail = `${path} · ${actionDesc}`;
  } else if (toolName === "grep_search" || toolName === "find_by_name" || toolName === "search_web") {
    kind = "search";
    query = String(params.Query ?? params.Pattern ?? params.query ?? "");
    if (!explicitTitle) {
      title = query ? `Search "${query.length > 28 ? query.slice(0, 28) + "…" : query}"` : "Search repository";
    }
    detail = params.SearchPath ? `in ${String(params.SearchPath)}` : query;
  } else if (toolName === "invoke_subagent" || toolName === "send_message") {
    kind = "subagent";
    if (!explicitTitle) {
      title = params.Role ? String(params.Role) : "Subagent task";
    }
    detail = String(params.Prompt ?? params.Message ?? "");
  } else {
    const firstParam = Object.entries(params).find(([k]) => !["toolAction", "toolSummary"].includes(k));
    detail = firstParam ? `${firstParam[0]}: ${String(firstParam[1])}` : title;
  }

  return {
    id: `${event.run_id}:${event.sequence}`,
    kind,
    tool: toolName,
    state,
    title,
    detail,
    command,
    cwd,
    path,
    lines,
    query,
    output,
    error,
    durationSeconds,
    sequence: event.sequence,
  };
}

export function presentationForRun(run: RunRecord, events: PersistedEvent[]): PresentationState {
  const presentation: PresentationState = {
    runId: run.id,
    status: run.status,
    assistantText: "",
    currentTool: null,
    completedTools: [],
    currentActivity: null,
    completedActivities: [],
    subagents: [],
    currentTaskSummary: typeof run.request?.prompt === "string" ? run.request.prompt : undefined,
    progress: null,
  };

  const outputTail: string[] = [];

  for (const event of events.slice().sort((left, right) => left.sequence - right.sequence)) {
    const payload = event.payload ?? {};
    if (event.event_type === "message.delta") {
      presentation.assistantText += typeof payload.text_delta === "string" ? payload.text_delta : "";
    } else if (event.event_type === "tool.started" || event.event_type === "tool.finished" || event.event_type === "tool.failed") {
      const normalized = normalizeToolActivity(event);
      const legacyTool: ToolActivity = {
        id: normalized.id,
        name: normalized.tool,
        state: normalized.state,
        detail: normalized.detail || normalized.title,
        output: normalized.output,
        durationMs: normalized.durationSeconds ? Math.round(normalized.durationSeconds * 1000) : undefined,
        sequence: normalized.sequence,
      };

      if (normalized.state === "running") {
        presentation.currentTool = legacyTool;
        presentation.currentActivity = normalized;
      } else {
        presentation.completedTools = [...presentation.completedTools.filter(t => t.id !== legacyTool.id), legacyTool];
        presentation.completedActivities = [...presentation.completedActivities.filter(a => a.id !== normalized.id), normalized];
        if (presentation.currentTool?.name === legacyTool.name || presentation.currentActivity?.id === normalized.id) {
          presentation.currentTool = null;
          presentation.currentActivity = null;
        }
      }
    } else if (event.event_type === "process.output" || event.event_type === "ssh.output") {
      const text = String(payload.text ?? "");
      if (text.trim()) {
        outputTail.push(text.trim());
        if (outputTail.length > 15) outputTail.shift();
      }
    } else if (event.event_type === "subagent.updated") {
      const info = payload.subagent_info;
      const label = typeof info === "string" ? info : typeof info === "object" && info !== null ? String((info as Record<string, unknown>).name ?? "Subagent") : "Subagent active";
      if (!presentation.subagents.includes(label)) presentation.subagents = [...presentation.subagents, label];
    } else if (event.event_type === "agent.completed" || event.event_type === "run.completed") {
      presentation.status = "completed";
      if (!presentation.assistantText && typeof payload.response === "string") presentation.assistantText = payload.response;
    } else if (event.event_type === "agent.failed" || event.event_type === "run.failed") {
      presentation.status = "failed";
    } else if (event.event_type === "run.cancelled") presentation.status = "cancelled";
    else if (event.event_type === "run.interrupted") presentation.status = "interrupted";
    else if (event.event_type === "run.running") presentation.status = "running";
    else if (event.event_type === "run.queued") presentation.status = "queued";
  }

  if (outputTail.length > 0) {
    if (!presentation.progress) {
      presentation.progress = {
        run_id: run.id,
        status: run.status,
        started_at: run.created_at,
        last_activity_at: events[events.length - 1]?.created_at || run.created_at,
        completed_steps: [],
        pending_steps: [],
        recent_output_tail: outputTail,
        counters: { tool_calls: presentation.completedTools.length, commands: 0, files_read: 0, files_modified: 0, output_lines: outputTail.length, output_bytes: 0 },
        version: events.length,
      };
    } else {
      presentation.progress.recent_output_tail = outputTail;
    }
  }

  return presentation;
}

export class ControlReplay {
  private socket: WebSocket | null = null;
  private reconnectTimer: number | undefined;
  private stopped = false;
  private listeners = new Set<(state: ControlState) => void>();
  private state: ControlState = initialState();

  subscribe(listener: (state: ControlState) => void): () => void {
    this.listeners.add(listener);
    listener(this.state);
    return () => this.listeners.delete(listener);
  }

  getState(): ControlState { return this.state; }

  private publish(next: Partial<ControlState>): void {
    this.state = { ...this.state, ...next };
    for (const listener of this.listeners) listener(this.state);
  }

  async start(): Promise<void> {
    this.stopped = false;
    try {
      const snapshot = await getHome();
      const snapshotActivity = snapshot.activity?.map((item) => item.event) ?? [];
      const mergedActivity = [...this.state.activity, ...snapshotActivity]
        .filter((event, index, all) => all.findIndex((candidate) => candidate.id === event.id) === index)
        .sort((left, right) => left.id - right.id)
        .slice(-MAX_ACTIVITY);
      this.publish({
        snapshot,
        activeRuns: snapshot.active_runs,
        activity: mergedActivity,
        cursor: this.state.cursor || snapshot.activity?.at(-1)?.cursor || 0,
        connection: "connecting",
        error: null,
      });
      this.connect();
    } catch (error) {
      this.publish({ connection: "offline", error: error instanceof Error ? error.message : "Unable to load control state" });
      this.scheduleReconnect();
    }
  }

  stop(): void {
    this.stopped = true;
    if (this.reconnectTimer !== undefined) window.clearTimeout(this.reconnectTimer);
    this.socket?.close();
    this.socket = null;
  }

  private connect(): void {
    if (this.stopped) return;
    this.publish({ connection: "connecting" });
    const socket = new WebSocket(controlSocketUrl(this.state.cursor));
    this.socket = socket;
    socket.onopen = () => this.publish({ connection: "connected", error: null });
    socket.onmessage = (message) => this.receive(message.data);
    socket.onerror = () => this.publish({ error: "Live connection interrupted" });
    socket.onclose = () => {
      this.socket = null;
      if (!this.stopped) {
        this.publish({ connection: "reconnecting" });
        this.scheduleReconnect();
      }
    };
  }

  private receive(raw: string): void {
    try {
      const message = JSON.parse(raw) as {
        type: string;
        cursor?: number;
        state?: ControlSnapshot;
        event?: PersistedEvent;
      };
      if (message.type === "control.snapshot" && message.state) {
        this.publish({ snapshot: message.state, activeRuns: message.state.active_runs });
        return;
      }
      if (message.type !== "control.event" || !message.event || message.cursor === undefined) return;
      if (message.cursor <= this.state.cursor) return;
      const event = message.event;
      const activeRuns = reduceRun(this.state.activeRuns, event);
      this.publish({
        cursor: message.cursor,
        activeRuns,
        activity: [...this.state.activity, event].slice(-MAX_ACTIVITY),
      });
    } catch {
      this.publish({ error: "Received an invalid control event" });
    }
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.reconnectTimer !== undefined) return;
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = undefined;
      void this.start();
    }, 1500);
  }
}

export function useControlReplay(enabled = true): ControlState {
  const replay = useRef<ControlReplay | null>(null);
  if (!replay.current) replay.current = new ControlReplay();
  const [state, setState] = useState<ControlState>(replay.current.getState());
  useEffect(() => {
    if (!enabled) return;
    const unsubscribe = replay.current!.subscribe(setState);
    void replay.current!.start();
    return () => {
      unsubscribe();
      replay.current!.stop();
    };
  }, [enabled]);
  return state;
}
