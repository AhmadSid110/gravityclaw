import { useEffect, useState } from "react";
import { cancelRun, getArtifact, getRunArtifacts, getRunCapabilities, getRunContext, getTimeline } from "./api";
import { buildRunInspection, eventIcon, eventLabel } from "./runModel";
import type { Artifact, ControlState, PersistedEvent, RunInspection, RunRecord } from "./types";

type InspectorTab = "Overview" | "Timeline" | "Tools" | "Subagents" | "Context" | "Capabilities" | "Artifacts" | "Events";

export function RichRunInspector({ run, state, onClose }: { run: RunRecord; state: ControlState; onClose: () => void }) {
  const [tab, setTab] = useState<InspectorTab>("Overview");
  const [currentRun, setCurrentRun] = useState(run);
  const [events, setEvents] = useState<PersistedEvent[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [context, setContext] = useState<Record<string, unknown> | null>(null);
  const [capabilities, setCapabilities] = useState<Record<string, unknown> | null>(null);
  const [hasMoreEvents, setHasMoreEvents] = useState(false);
  const [loadingMoreEvents, setLoadingMoreEvents] = useState(false);
  const [raw, setRaw] = useState(false);
  const [expandedTool, setExpandedTool] = useState<string | null>(null);
  const [selectedArtifact, setSelectedArtifact] = useState<Artifact | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inspection = buildRunInspection(currentRun, events, artifacts, context, capabilities);

  useEffect(() => {
    setCurrentRun(run);
    setEvents([]);
    setHasMoreEvents(false);
    setLoadingMoreEvents(false);
    setSelectedArtifact(null);
    setError(null);
    void Promise.all([getRunArtifacts(run.id), getRunContext(run.id), getRunCapabilities(run.id)]).then(([nextArtifacts, nextContext, nextCapabilities]) => {
      setArtifacts(nextArtifacts);
      setContext(nextContext);
      setCapabilities(nextCapabilities);
    }).catch(() => {
      setArtifacts([]);
      setContext(null);
      setCapabilities(null);
    });
  }, [run.id]);

  useEffect(() => {
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void getTimeline(run.id).then((value) => {
        if (cancelled) return;
        setCurrentRun(value.run);
        setEvents((current) => mergeEvents(current, value.events));
        setHasMoreEvents(value.has_more);
      }).catch((reason) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "Unable to load run timeline");
      });
    }, 80);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [run.id, state.activity.at(-1)?.id]);

  async function stop() {
    try { setCurrentRun(await cancelRun(currentRun)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to stop run"); }
  }

  async function openArtifact(artifact: Artifact) {
    try { setSelectedArtifact(await getArtifact(artifact.id)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to load artifact"); }
  }

  async function loadMoreEvents() {
    if (loadingMoreEvents || !hasMoreEvents) return;
    const after = events.at(-1)?.sequence ?? 0;
    setLoadingMoreEvents(true);
    try {
      const value = await getTimeline(run.id, after);
      setEvents((current) => mergeEvents(current, value.events));
      setHasMoreEvents(value.has_more);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to load more timeline events");
    } finally {
      setLoadingMoreEvents(false);
    }
  }

  const tabs: InspectorTab[] = ["Overview", "Timeline", "Tools", "Subagents", "Context", "Capabilities", "Artifacts", "Events"];
  return <aside className="inspector-panel rich-inspector">
    <div className="inspector-header"><div><div className="eyebrow">RUN INSPECTOR</div><strong>{currentRun.id.slice(0, 12)}</strong></div><button className="icon-button" onClick={onClose} aria-label="Close inspector">×</button></div>
    <div className="rich-run-summary"><StatusBadge status={currentRun.status} large /><h2>{String(currentRun.request.prompt ?? "Untitled run")}</h2><div className="run-summary-grid"><Detail label="Workspace" value="gravityclaw" /><Detail label="Trigger" value={String(currentRun.request.trigger ?? "Web")} /><Detail label="Requested model" value={String(currentRun.requested_model ?? currentRun.request.requested_model ?? "Default")} /><Detail label="Resolved model" value={String(currentRun.resolved_model ?? currentRun.request.resolved_model ?? "AGY runtime default")} /><Detail label="AGY version" value={String(currentRun.agy_version ?? currentRun.request.agy_version ?? "unknown")} /><Detail label="Started" value={formatTimestamp(currentRun.started_at ?? currentRun.created_at)} /><Detail label="Duration" value={runDuration(currentRun)} /><Detail label="Conversation" value={currentRun.conversation_id.slice(0, 12)} /><Detail label="Worker" value={currentRun.worker_id?.slice(0, 18) ?? "Not assigned"} /></div>{currentRun.status === "running" || currentRun.status === "queued" ? <button className="danger-button" onClick={() => void stop()}>■ Stop run</button> : <div className="run-terminal-note">Run {currentRun.status}. Historical state is preserved.</div>}{error && <div className="inline-error" role="alert">{error}</div>}</div>
    <div className="inspector-tabs rich-tabs" role="tablist" aria-label="Run inspection sections">{tabs.map((item) => <button key={item} role="tab" aria-selected={tab === item} className={tab === item ? "active" : ""} onClick={() => setTab(item)}>{item}</button>)}</div>
    <div className="rich-inspector-body">
      {tab === "Overview" && <RunOverview inspection={inspection} onTab={setTab} />}
      {tab === "Timeline" && <RunTimeline inspection={inspection} hasMore={hasMoreEvents} loadingMore={loadingMoreEvents} onLoadMore={() => void loadMoreEvents()} />}
      {tab === "Tools" && <ToolList inspection={inspection} expandedTool={expandedTool} onToggle={setExpandedTool} />}
      {tab === "Subagents" && <SubagentTree inspection={inspection} />}
      {tab === "Context" && <ManifestPanel title="Context manifest" value={context} empty="Context manifest is not available for this run." />}
      {tab === "Capabilities" && <ManifestPanel title="Capability snapshot" value={capabilities} empty="Capability snapshot is not available for this run." />}
      {tab === "Artifacts" && <ArtifactList artifacts={artifacts} selected={selectedArtifact} onOpen={(artifact) => void openArtifact(artifact)} />}
      {tab === "Events" && <EventInspector events={inspection.events} raw={raw} onToggle={() => setRaw(!raw)} hasMore={hasMoreEvents} loadingMore={loadingMoreEvents} onLoadMore={() => void loadMoreEvents()} />}
    </div>
  </aside>;
}

function RunOverview({ inspection, onTab }: { inspection: RunInspection; onTab: (tab: InspectorTab) => void }) { return <div className="run-overview"><div className="overview-metric"><span>Observable events</span><strong>{inspection.events.length}</strong></div><div className="overview-metric"><span>Tools</span><strong>{inspection.tools.length}</strong></div><div className="overview-metric"><span>Subagents</span><strong>{inspection.subagents.length}</strong></div><div className="overview-metric"><span>Artifacts</span><strong>{inspection.artifacts.length}</strong></div><div className="inspector-section"><div className="section-label">INSPECT</div><button className="inspector-link" onClick={() => onTab("Timeline")}>Open unified timeline <span>→</span></button><button className="inspector-link" onClick={() => onTab("Context")}>Context manifest <span>→</span></button><button className="inspector-link" onClick={() => onTab("Capabilities")}>Capability snapshot <span>→</span></button><button className="inspector-link" onClick={() => onTab("Events")}>Deep event view <span>→</span></button></div></div>; }

function RunTimeline({ inspection, hasMore, loadingMore, onLoadMore }: { inspection: RunInspection; hasMore: boolean; loadingMore: boolean; onLoadMore: () => void }) { return <div className="run-timeline">{hasMore && <LoadMoreEvents loading={loadingMore} onClick={onLoadMore} />}{inspection.events.map((event) => <div className="run-timeline-row" key={event.id}><span className="timeline-time">{formatTimestamp(event.created_at, true)}</span><span className="timeline-icon">{eventIcon(event)}</span><div><strong>{eventLabel(event)}</strong><small>sequence {event.sequence}</small></div></div>)}{inspection.events.length === 0 && <EmptyState icon="◎" title="No events" detail="Persisted execution events will appear here." />}{hasMore && <LoadMoreEvents loading={loadingMore} onClick={onLoadMore} />}</div>; }

function ToolList({ inspection, expandedTool, onToggle }: { inspection: RunInspection; expandedTool: string | null; onToggle: (id: string | null) => void }) { return <div className="tool-list">{inspection.tools.map((tool) => <article className={`tool-card ${tool.state}`} key={tool.id}><button className="tool-card-head" onClick={() => onToggle(expandedTool === tool.id ? null : tool.id)}><span className="tool-state-icon">{tool.state === "running" ? "⚙" : tool.state === "finished" ? "✓" : tool.state === "soft-denied" ? "⊘" : "!"}</span><span><strong>{tool.name}</strong><small>{tool.state.replace("-", " ")} · {tool.durationMs ? formatDurationMs(tool.durationMs) : "observable activity"}</small></span><span className="tool-chevron">{expandedTool === tool.id ? "⌃" : "⌄"}</span></button>{expandedTool === tool.id && <div className="tool-card-detail"><p>{tool.detail}</p>{tool.output && <pre>{tool.output}</pre>}<small>Sequence {tool.sequence}</small></div>}</article>)}{inspection.tools.length === 0 && <EmptyState icon="⚙" title="No tools recorded" detail="Tool activity will appear when this run uses a capability." />}</div>; }

function SubagentTree({ inspection }: { inspection: RunInspection }) { return <div className="subagent-tree"><div className="agent-tree-root"><span>✦</span><strong>Main Agent</strong><small>{inspection.subagents.length} child agents</small></div>{inspection.subagents.map((agent) => <article className={`subagent-node ${agent.state}`} key={agent.id}><span className="tree-branch">└─</span><span className="subagent-state">{agent.state === "running" ? "◉" : agent.state === "completed" ? "✓" : agent.state === "failed" ? "!" : agent.state === "cancelled" ? "■" : "○"}</span><div><strong>{agent.label}</strong><small>{agent.state} · {agent.tools} tools{agent.tokens ? ` · ${agent.tokens.toLocaleString()} tokens` : ""}</small><p>{agent.latest}</p></div></article>)}{inspection.subagents.length === 0 && <EmptyState icon="↳" title="No subagents recorded" detail="Native AGY subagent activity will appear here." />}</div>; }

function ManifestPanel({ title, value, empty }: { title: string; value: Record<string, unknown> | null; empty: string }) { return value ? <div className="manifest-panel"><div className="section-label">IMMUTABLE SNAPSHOT</div><h3>{title}</h3><pre>{JSON.stringify(value, null, 2)}</pre></div> : <EmptyState icon="◎" title={title} detail={empty} />; }

function ArtifactList({ artifacts, selected, onOpen }: { artifacts: Artifact[]; selected: Artifact | null; onOpen: (artifact: Artifact) => void }) { return <div className="artifact-list">{artifacts.map((artifact) => <article className="artifact-card" key={artifact.id}><div><span className="artifact-kind">{artifact.kind}</span><strong>{artifact.id.slice(0, 12)}</strong><small>{formatBytes(artifact.characters)} · sha {artifact.sha256.slice(0, 10)}</small></div><p>{artifact.summary || artifact.excerpt || "Bounded artifact reference"}</p><button className="text-link" onClick={() => onOpen(artifact)}>{selected?.id === artifact.id ? "Loaded" : "Preview"} →</button>{selected?.id === artifact.id && <pre className="artifact-preview">{selected.content ?? selected.excerpt}</pre>}</article>)}{artifacts.length === 0 && <EmptyState icon="◇" title="No artifacts" detail="Large outputs and bounded references will appear here." />}</div>; }

function EventInspector({ events, raw, onToggle, hasMore, loadingMore, onLoadMore }: { events: PersistedEvent[]; raw: boolean; onToggle: () => void; hasMore: boolean; loadingMore: boolean; onLoadMore: () => void }) { return <div className="event-inspector"><div className="event-view-toggle"><button className={!raw ? "active" : ""} onClick={onToggle}>Normalized</button><button className={raw ? "active" : ""} onClick={onToggle}>Raw</button></div>{hasMore && <LoadMoreEvents loading={loadingMore} onClick={onLoadMore} />}{events.map((event) => <details className="event-detail" key={event.id}><summary><span>{eventIcon(event)}</span><strong>#{event.id} {eventLabel(event)}</strong><small>{formatTimestamp(event.created_at, true)}</small></summary><pre>{JSON.stringify(raw ? event.raw ?? event.payload : event.payload, null, 2)}</pre></details>)}{events.length === 0 && <EmptyState icon="◎" title="No events" detail="The event stream is empty for this run." />}</div>; }

function LoadMoreEvents({ loading, onClick }: { loading: boolean; onClick: () => void }) { return <button className="load-more-events" onClick={onClick} disabled={loading}>{loading ? "Loading events…" : "Load more events"}</button>; }

function StatusBadge({ status, large = false }: { status: string; large?: boolean }) { const labels: Record<string, string> = { running: "Running", queued: "Queued", completed: "Completed", failed: "Failed", cancelled: "Cancelled", interrupted: "Interrupted", orphaned: "Orphaned" }; const icons: Record<string, string> = { running: "◉", queued: "◌", completed: "✓", failed: "!", cancelled: "■", interrupted: "!", orphaned: "!" }; const label = labels[status] ?? status; return <span className={`status-badge ${status} ${large ? "large" : ""}`} role="status" aria-label={`Run status: ${label}`}><span className="status-symbol" aria-hidden="true">{icons[status] ?? "•"}</span><span className="status-dot" aria-hidden="true" />{label}</span>; }
function Detail({ label, value }: { label: string; value: string }) { return <div className="detail-row"><span>{label}</span><strong>{value}</strong></div>; }
function EmptyState({ icon, title, detail }: { icon: string; title: string; detail: string }) { return <div className="empty-state"><span className="empty-icon">{icon}</span><strong>{title}</strong><span>{detail}</span></div>; }
function formatTimestamp(value: string | null, seconds = false): string { if (!value) return "—"; const date = new Date(value); return Number.isNaN(date.getTime()) ? "—" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}) }); }
function runDuration(run: RunRecord): string { const start = Date.parse(run.started_at ?? run.created_at); const end = Date.parse(run.finished_at ?? new Date().toISOString()); return Number.isNaN(start) || Number.isNaN(end) ? "—" : formatDurationMs(Math.max(0, end - start)); }
function formatDurationMs(milliseconds: number): string { const seconds = Math.round(milliseconds / 1000); return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`; }
function formatBytes(characters: number): string { return characters < 1024 ? `${characters} B` : characters < 1024 * 1024 ? `${(characters / 1024).toFixed(1)} KB` : `${(characters / (1024 * 1024)).toFixed(1)} MB`; }
function mergeEvents(current: PersistedEvent[], incoming: PersistedEvent[]): PersistedEvent[] { const byId = new Map(current.map((event) => [event.id, event])); for (const event of incoming) byId.set(event.id, event); return [...byId.values()].sort((left, right) => left.sequence - right.sequence || left.id - right.id); }
