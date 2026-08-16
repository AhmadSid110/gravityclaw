import { useEffect, useRef, useState } from "react";
import { controlSocketUrl, getHome } from "./api";
import type { ControlSnapshot, ControlState, PersistedEvent, PresentationState, RunRecord, ToolActivity } from "./types";

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

export function presentationForRun(run: RunRecord, events: PersistedEvent[]): PresentationState {
  const presentation: PresentationState = {
    runId: run.id, status: run.status, assistantText: "", currentTool: null,
    completedTools: [], subagents: [],
  };
  for (const event of events.slice().sort((left, right) => left.sequence - right.sequence)) {
    const payload = event.payload;
    if (event.event_type === "message.delta") {
      presentation.assistantText += typeof payload.text_delta === "string" ? payload.text_delta : "";
    } else if (event.event_type === "tool.started" || event.event_type === "tool.finished" || event.event_type === "tool.failed") {
      const tool: ToolActivity = {
        id: `${run.id}:${event.sequence}`,
        name: String(payload.tool_name ?? payload.tool ?? "Tool activity"),
        state: event.event_type === "tool.started" ? "running" : event.event_type === "tool.finished" ? "finished" : "failed",
        detail: String(payload.tool_info ?? payload.command ?? "Observable tool execution"),
      };
      if (tool.state === "running") presentation.currentTool = tool;
      else {
        presentation.completedTools = [...presentation.completedTools, tool];
        if (presentation.currentTool?.name === tool.name) presentation.currentTool = null;
      }
    } else if (event.event_type === "subagent.updated") {
      const info = payload.subagent_info;
      const label = typeof info === "string" ? info : typeof info === "object" && info !== null ? String((info as Record<string, unknown>).name ?? "Subagent") : "Subagent active";
      if (!presentation.subagents.includes(label)) presentation.subagents = [...presentation.subagents, label];
    } else if (event.event_type === "agent.completed" || event.event_type === "run.completed") {
      presentation.status = "completed";
      if (!presentation.assistantText && typeof payload.response === "string") presentation.assistantText = payload.response;
    } else if (event.event_type === "agent.failed" || event.event_type === "run.failed") presentation.status = "failed";
    else if (event.event_type === "run.cancelled") presentation.status = "cancelled";
    else if (event.event_type === "run.interrupted") presentation.status = "interrupted";
    else if (event.event_type === "run.running") presentation.status = "running";
    else if (event.event_type === "run.queued") presentation.status = "queued";
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
