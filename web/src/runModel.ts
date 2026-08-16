import type { Artifact, PersistedEvent, RunInspection, RunRecord, SubagentNode, ToolActivity } from "./types";

const terminalSubagentStates = new Set(["DONE", "COMPLETED", "ERROR", "FAILED", "CANCELED", "CANCELLED", "INTERRUPTED"]);

export function buildRunInspection(
  run: RunRecord,
  events: PersistedEvent[],
  artifacts: Artifact[] = [],
  context: Record<string, unknown> | null = null,
  capabilities: Record<string, unknown> | null = null,
): RunInspection {
  const ordered = [...events].sort((a, b) => a.sequence - b.sequence || a.id - b.id);
  const eventTimes = new Map(ordered.map((event) => [event.sequence, event.created_at]));
  const tools = new Map<string, ToolActivity>();
  const activeTools = new Map<string, string>();
  const subagents = new Map<string, SubagentNode>();

  for (const event of ordered) {
    const payload = event.payload;
    if (event.event_type === "tool.started" || event.event_type === "tool.finished" || event.event_type === "tool.failed") {
      const name = String(payload.tool_name ?? payload.tool ?? "Tool activity");
      const info = payload.tool_info;
      const detail = describeTool(info, payload.command);
      const softDenied = event.event_type === "tool.failed" && /denied|permission|approval/i.test(detail);
      const existingId = activeTools.get(name);
      const id = existingId ?? `${run.id}:tool:${event.sequence}`;
      const previous = tools.get(id);
      const state: ToolActivity["state"] = event.event_type === "tool.started"
        ? "running"
        : softDenied ? "soft-denied" : event.event_type === "tool.failed" ? "failed" : "finished";
      const durationMs = previous ? Math.max(0, Date.parse(event.created_at) - Date.parse(eventTimes.get(previous.sequence ?? event.sequence) ?? event.created_at)) : undefined;
      tools.set(id, { id, name, state, detail, sequence: previous?.sequence ?? event.sequence, durationMs, output: extractOutput(info) });
      if (event.event_type === "tool.started") activeTools.set(name, id);
      else activeTools.delete(name);
    }

    if (event.event_type === "subagent.updated") {
      const info = payload.subagent_info;
      const entries = typeof info === "object" && info !== null && Array.isArray((info as Record<string, unknown>).subagents)
        ? (info as Record<string, unknown>).subagents as unknown[] : [];
      for (const [index, entry] of entries.entries()) {
        const item = typeof entry === "object" && entry !== null ? entry as Record<string, unknown> : {};
        const conversationId = stringOrNull(item.conversation_id);
        const id = conversationId ?? `${run.id}:subagent:${index}`;
        const rawState = String(item.state ?? payload.state ?? "ACTIVE").toUpperCase();
        const state: SubagentNode["state"] = terminalSubagentStates.has(rawState)
          ? rawState.includes("ERROR") || rawState.includes("FAIL") ? "failed"
            : rawState.includes("CANCEL") ? "cancelled" : "completed"
          : rawState === "QUEUED" ? "queued" : rawState === "ACTIVE" || rawState === "RUNNING" ? "running" : "unknown";
        const previous = subagents.get(id);
        subagents.set(id, {
          id,
          label: String(item.name ?? item.role ?? previous?.label ?? "Subagent"),
          state,
          parentId: stringOrNull(item.parent_conversation_id ?? (info as Record<string, unknown> | null)?.parent_conversation_id) ?? event.conversation_id,
          conversationId,
          tools: Number(item.tool_count ?? previous?.tools ?? 0),
          tokens: typeof item.tokens === "number" ? item.tokens : previous?.tokens ?? null,
          latest: String(item.latest ?? item.output ?? previous?.latest ?? "Observable activity received"),
          sequence: event.sequence,
        });
      }
    }
  }

  return {
    run,
    events: ordered,
    tools: [...tools.values()],
    subagents: [...subagents.values()],
    artifacts,
    context,
    capabilities,
  };
}

export function eventLabel(event: PersistedEvent): string {
  return event.event_type.replaceAll(".", " · ");
}

export function eventIcon(event: PersistedEvent): string {
  if (event.event_type.includes("failed")) return "!";
  if (event.event_type.includes("cancel")) return "■";
  if (event.event_type.includes("tool")) return event.event_type.endsWith("started") ? "⚙" : "✓";
  if (event.event_type.includes("subagent")) return "↳";
  if (event.event_type.includes("completed")) return "✓";
  return "•";
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function describeTool(info: unknown, command: unknown): string {
  if (typeof command === "string" && command) return command;
  if (typeof info === "string") return info;
  if (typeof info === "object" && info !== null) {
    const object = info as Record<string, unknown>;
    const parameters = object.parameters;
    if (typeof parameters === "object" && parameters !== null) {
      const commandLine = (parameters as Record<string, unknown>).CommandLine ?? (parameters as Record<string, unknown>).command;
      if (typeof commandLine === "string") return commandLine;
    }
    const error = object.error;
    if (typeof error === "object" && error !== null && typeof (error as Record<string, unknown>).message === "string") return String((error as Record<string, unknown>).message);
    return JSON.stringify(info);
  }
  return "Observable tool execution";
}

function extractOutput(info: unknown): string | undefined {
  if (typeof info !== "object" || info === null) return undefined;
  const output = (info as Record<string, unknown>).output;
  return typeof output === "string" ? output : undefined;
}
