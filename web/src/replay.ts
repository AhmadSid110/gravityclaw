import { useEffect, useRef, useState } from "react";
import { controlSocketUrl, getHome } from "./api";
import type { ControlSnapshot, ControlState, PersistedEvent, RunRecord } from "./types";

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
      this.publish({
        snapshot,
        activeRuns: snapshot.active_runs,
        activity: snapshot.activity?.map((item) => item.event).slice(-MAX_ACTIVITY) ?? [],
        cursor: snapshot.activity?.at(-1)?.cursor ?? this.state.cursor,
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
