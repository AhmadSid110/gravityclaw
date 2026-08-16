import { useEffect, useState } from "react";
import { cancelRun, createConversation, getConversation, getConversations, getRuns, getSession, getTimeline, getWorkspaces, login, logout, submitRun } from "./api";
import { presentationForRun, useControlReplay } from "./replay";
import { RichRunInspector } from "./RunInspector";
import { MemoryStudio } from "./MemoryStudio";
import { ContextStudio } from "./ContextStudio";
import { AutomationsStudio } from "./AutomationsStudio";
import { CapabilitiesStudio } from "./CapabilitiesStudio";
import type { Conversation, ConversationDetail, ControlState, Message, PersistedEvent, RunRecord } from "./types";

const navItems = [
  ["home", "⌂", "Home"],
  ["conversations", "◌", "Conversations"],
  ["runs", "ϟ", "Runs"],
  ["memory", "✦", "Memory"],
  ["context", "◎", "Context"],
  ["workspaces", "◇", "Workspaces"],
  ["automations", "◷", "Automations"],
  ["capabilities", "⊙", "Capabilities"],
  ["channels", "◉", "Channels"],
] as const;

type View = typeof navItems[number][0] | "usage" | "settings";

function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  useEffect(() => {
    void getSession().then((result) => setAuthenticated(result.authenticated)).catch(() => setAuthenticated(false));
  }, []);

  if (authenticated === null) return <div className="boot-screen"><div className="brand-mark">✦</div><span>Connecting to GravityClaw…</span></div>;
  if (!authenticated) return <Login onSuccess={() => setAuthenticated(true)} error={authError} setError={setAuthError} />;
  return <Console onLogout={async () => { await logout(); setAuthenticated(false); }} />;
}

function Login({ onSuccess, error, setError }: { onSuccess: () => void; error: string | null; setError: (value: string | null) => void }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try { await login(token); onSuccess(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Authentication failed"); }
    finally { setBusy(false); }
  }
  return <main className="login-screen">
    <div className="login-card">
      <div className="brand-lockup"><div className="brand-mark">✦</div><div><strong>GravityClaw</strong><span>Control Center</span></div></div>
      <div className="eyebrow">PRIVATE CONTROL PLANE</div>
      <h1>Welcome back.</h1>
      <p className="muted">Enter the control token to create a secure browser session. It will never be stored in local browser storage.</p>
      <form onSubmit={submit} className="stack">
        <label className="field-label" htmlFor="control-token">Control token</label>
        <input id="control-token" className="text-input" type="password" autoComplete="off" value={token} onChange={(event) => setToken(event.target.value)} placeholder="Paste token" autoFocus />
        {error && <div className="inline-error">{error}</div>}
        <button className="primary-button" disabled={busy || !token}>{busy ? "Authenticating…" : "Open control center"}<span>↗</span></button>
      </form>
      <div className="secure-note"><span>⌁</span> Session secured with an HttpOnly cookie</div>
    </div>
  </main>;
}

function Console({ onLogout }: { onLogout: () => Promise<void> }) {
  const [view, setView] = useState<View>("home");
  const [mobileNav, setMobileNav] = useState(false);
  const [focus, setFocus] = useState(false);
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  const [selectedRun, setSelectedRun] = useState<RunRecord | null>(null);
  const state = useControlReplay();
  const title = navItems.find(([id]) => id === view)?.[2] ?? (view === "usage" ? "Usage" : view === "settings" ? "Settings" : "Home");
  return <div className={`app ${theme}`}>
    <aside className={`sidebar ${mobileNav ? "open" : ""}`}>
      <div className="brand-lockup sidebar-brand"><div className="brand-mark">✦</div><div><strong>GravityClaw</strong><span>Control Center</span></div></div>
      <div className="workspace-switcher"><div className="workspace-icon">G</div><div><strong>GravityClaw</strong><span>Personal agent</span></div><span className="chevron">⌄</span></div>
      <nav className="primary-nav" aria-label="Primary navigation">
        {navItems.map(([id, icon, label]) => <button key={id} className={`nav-item ${view === id ? "active" : ""}`} onClick={() => { setView(id); setMobileNav(false); }}><span className="nav-icon">{icon}</span><span>{label}</span>{id === "runs" && state.activeRuns.length > 0 && <em>{state.activeRuns.length}</em>}</button>)}
      </nav>
      <div className="nav-divider" />
      <div className="section-label">SYSTEM</div>
      <button className={`nav-item ${view === "usage" ? "active" : ""}`} onClick={() => { setView("usage"); setMobileNav(false); }}><span className="nav-icon">◒</span><span>Usage</span></button>
      <button className={`nav-item ${view === "settings" ? "active" : ""}`} onClick={() => { setView("settings"); setMobileNav(false); }}><span className="nav-icon">⚙</span><span>Settings</span></button>
      <div className="sidebar-bottom"><div className="connection-line"><span className={`status-dot ${state.connection === "connected" ? "green" : "amber"}`} />{state.connection === "connected" ? "Live" : state.connection}</div><button className="user-chip" onClick={() => void onLogout()}><span className="avatar">A</span><span>Ahmad</span><span>⌄</span></button></div>
    </aside>
    {mobileNav && <button className="scrim" aria-label="Close navigation" onClick={() => setMobileNav(false)} />}
    <main className="main-shell">
      <header className="topbar"><button className="mobile-menu" onClick={() => setMobileNav(true)} aria-label="Open navigation">☰</button><div className="breadcrumb"><span>GravityClaw</span><span className="crumb-separator">/</span><strong>{title}</strong></div><div className="topbar-actions"><button className="palette-hint" onClick={() => setFocus(!focus)} aria-label="Open command palette"><span>⌘</span><span>K</span></button><button className="icon-button" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} aria-label="Toggle theme">{theme === "dark" ? "☼" : "☾"}</button><span className="system-health"><span className="status-dot green" /> Healthy</span></div></header>
      <div className="content-shell">
        {view === "home" && <Home state={state} onRunSelect={setSelectedRun} onOpenRuns={() => setView("runs")} />}
        {view === "conversations" && <ConversationWorkspace state={state} focus={focus} onToggleFocus={() => setFocus(!focus)} onRunSelect={setSelectedRun} />}
        {view === "runs" && <Runs state={state} onRunSelect={setSelectedRun} />}
        {view === "memory" && <MemoryStudio />}
        {view === "context" && <ContextStudio />}
        {view === "automations" && <AutomationsStudio />}
        {view === "capabilities" && <CapabilitiesStudio />}
        {view !== "home" && view !== "runs" && view !== "conversations" && view !== "memory" && view !== "context" && view !== "automations" && view !== "capabilities" && <ComingSoon title={title} />}
      </div>
      {selectedRun && <RichRunInspector run={selectedRun} state={state} onClose={() => setSelectedRun(null)} />}
      <button className="command-fab" onClick={() => setFocus(!focus)} aria-label="Open command palette">⌘K</button>
    </main>
    <div className={`focus-toast ${focus ? "show" : ""}`}><strong>Command palette</strong><span>Global actions arrive with M8B.7</span><button onClick={() => setFocus(false)}>×</button></div>
  </div>;
}

function Home({ state, onRunSelect, onOpenRuns }: { state: ControlState; onRunSelect: (run: RunRecord) => void; onOpenRuns: () => void }) {
  const snapshot = state.snapshot;
  const active = state.activeRuns.filter((run) => run.status === "running");
  const queued = state.activeRuns.filter((run) => run.status === "queued");
  return <div className="page fade-in"><div className="page-heading"><div><div className="eyebrow">SUNDAY · AUGUST 16, 2026</div><h1>Good evening, Ahmad <span className="wave">✦</span></h1><p className="muted">GravityClaw is keeping watch.</p></div><div className="live-pill"><span className="status-dot green" /> {state.connection === "connected" ? "Live" : "Reconnecting"}</div></div>
    <section className="stat-grid"><StatCard label="Active runs" value={String(active.length)} detail={active.length ? "Agent activity in progress" : "Nothing running"} accent="blue" /><StatCard label="Queued" value={String(queued.length)} detail={queued.length ? "Waiting for a conversation lock" : "Queue is clear"} accent="violet" /><StatCard label="Next heartbeat" value={nextHeartbeat(snapshot)} detail="Autonomous check" accent="amber" /></section>
    <div className="content-grid"><section className="panel active-panel"><PanelHeader title="Active now" link={active.length ? "View all runs" : undefined} onLink={onOpenRuns} />{active.length === 0 && <EmptyState icon="◌" title="GravityClaw is quiet" detail="Start a conversation when you have something to build, inspect, or untangle." />}{active.map((run) => <RunRow key={run.id} run={run} onClick={() => onRunSelect(run)} />)}</section><section className="panel"><PanelHeader title="Up next" /><div className="schedule-row"><span className="schedule-icon">◷</span><div><strong>Main heartbeat</strong><span>Next evaluation in 18m</span></div><span className="schedule-state">Enabled</span></div><div className="schedule-row"><span className="schedule-icon subdued">◌</span><div><strong>Weekly dependency review</strong><span>Monday · 09:00 · gravityclaw</span></div><span className="schedule-state muted-text">Tomorrow</span></div></section></div>
    <section className="panel activity-panel"><PanelHeader title="Recent activity" link="Open runs" onLink={onOpenRuns} /><div className="activity-list">{state.activity.slice(-8).reverse().map((event) => <ActivityRow key={event.id} event={event} />)}</div></section>
  </div>;
}

function Runs({ state, onRunSelect }: { state: ControlState; onRunSelect: (run: RunRecord) => void }) {
  const [runs, setRuns] = useState<RunRecord[]>(state.activeRuns);
  useEffect(() => {
    let cancelled = false;
    void getRuns().then((items) => { if (!cancelled) setRuns(items); }).catch(() => undefined);
    return () => { cancelled = true; };
  }, []);
  useEffect(() => {
    setRuns((current) => {
      const byId = new Map(current.map((run) => [run.id, run]));
      for (const active of state.activeRuns) byId.set(active.id, active);
      return [...byId.values()].sort((left, right) => right.created_at.localeCompare(left.created_at));
    });
  }, [state.activeRuns]);
  return <div className="page fade-in"><div className="page-heading"><div><div className="eyebrow">OPERATIONS</div><h1>Runs</h1><p className="muted">Every execution, one durable timeline.</p></div><button className="secondary-button">⌕ <span>Search runs</span></button></div><div className="filter-bar"><button className="filter active">All <span>⌄</span></button><button className="filter">Workspace <span>⌄</span></button><button className="filter">State <span>⌄</span></button><span className="filter-count">{runs.length} visible</span></div><section className="panel runs-table"><div className="table-head"><span>STATE</span><span>TASK</span><span>WORKSPACE</span><span>VERSION</span><span>TIME</span></div>{runs.length === 0 && <EmptyState icon="ϟ" title="No runs yet" detail="Your execution history will appear here." />}{runs.map((run) => <button className="table-row" key={run.id} onClick={() => onRunSelect(run)}><span><StatusBadge status={run.status} /></span><span className="task-cell"><strong>{String(run.request.prompt ?? "Untitled run")}</strong><small>{run.id.slice(0, 8)} · {run.request.context_profile ?? "chat"}</small></span><span className="workspace-cell">gravityclaw</span><span className="mono">v{run.version}</span><span className="muted-text">{formatTime(run.created_at)}</span></button>)}</section></div>;
}

function ConversationWorkspace({ state, focus, onToggleFocus, onRunSelect }: { state: ControlState; focus: boolean; onToggleFocus: () => void; onRunSelect: (run: RunRecord) => void }) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [timeline, setTimeline] = useState<PersistedEvent[]>([]);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getConversations().then((items) => {
      if (cancelled) return;
      setConversations(items);
      setSelectedId((current) => current ?? items[0]?.id ?? null);
      setLoading(false);
    }).catch((reason) => { if (!cancelled) { setError(reason instanceof Error ? reason.message : "Unable to load conversations"); setLoading(false); } });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!selectedId) { setDetail(null); return; }
    let cancelled = false;
    setLoading(true);
    void getConversation(selectedId).then((value) => { if (!cancelled) { setDetail(value); setLoading(false); } }).catch((reason) => { if (!cancelled) { setError(reason instanceof Error ? reason.message : "Unable to open conversation"); setLoading(false); } });
    return () => { cancelled = true; };
  }, [selectedId]);

  const run = detail?.runs.slice().reverse().find((item) => item.status === "running" || item.status === "queued") ?? detail?.runs.at(-1) ?? null;
  useEffect(() => {
    if (!run) { setTimeline([]); return; }
    let cancelled = false;
    void getTimeline(run.id).then((value) => { if (!cancelled) setTimeline(value.events); }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [run?.id]);

  useEffect(() => {
    if (!detail) return;
    const relevant = state.activity.some((event) => detail.runs.some((item) => item.id === event.run_id) && ["run.completed", "run.failed", "run.cancelled", "run.interrupted"].includes(event.event_type));
    if (!relevant) return;
    const timer = window.setTimeout(() => { void getConversation(detail.conversation.id).then(setDetail).catch(() => undefined); }, 180);
    return () => window.clearTimeout(timer);
  }, [state.activity, selectedId]);

  async function newConversation() {
    setError(null);
    try {
      const workspaces = await getWorkspaces();
      if (!workspaces[0]) throw new Error("Create a workspace before starting a conversation.");
      const conversation = await createConversation(workspaces[0].id);
      setConversations((items) => [conversation, ...items]);
      setSelectedId(conversation.id);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to create conversation"); }
  }

  async function send() {
    const prompt = draft.trim();
    if (!prompt || !detail || sending) return;
    setSending(true); setError(null);
    try {
      const submitted = await submitRun(detail.conversation.id, prompt);
      const optimistic: Message = { id: `local:${submitted.id}`, conversation_id: detail.conversation.id, role: "user", content: prompt, created_at: new Date().toISOString(), source_run_id: submitted.id };
      setDetail((current) => current ? { ...current, messages: [...current.messages, optimistic], runs: [...current.runs, submitted] } : current);
      setDraft("");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to send message"); }
    finally { setSending(false); }
  }

  const liveEvents = [...timeline, ...state.activity.filter((event) => event.run_id === run?.id)]
    .filter((event, index, all) => all.findIndex((candidate) => candidate.id === event.id) === index);
  const presentation = run ? presentationForRun(run, liveEvents) : null;
  const title = detail?.conversation.title || detail?.messages.find((item) => item.role === "user")?.content.slice(0, 34) || "New conversation";
  return <div className={`conversation-workspace ${focus ? "focus-mode" : "inspect-mode"}`}>
    {!focus && <aside className="conversation-nav"><div className="conversation-nav-head"><div><div className="eyebrow">INBOX</div><h2>Conversations</h2></div><button className="new-conversation" onClick={() => void newConversation()} aria-label="New conversation">+</button></div><div className="conversation-list">{loading && conversations.length === 0 && <div className="list-loading">Loading conversations…</div>}{conversations.map((conversation) => <button key={conversation.id} className={`conversation-item ${selectedId === conversation.id ? "selected" : ""}`} onClick={() => setSelectedId(conversation.id)}><span className="conversation-avatar">{(conversation.title || conversation.channel || "G").slice(0, 1).toUpperCase()}</span><span className="conversation-item-copy"><strong>{conversation.title || "Untitled conversation"}</strong><small>{conversation.channel} · {formatTime(conversation.updated_at)}</small></span>{state.activeRuns.some((item) => item.conversation_id === conversation.id && item.status === "running") && <span className="status-dot blue" />}</button>)}{!loading && conversations.length === 0 && <EmptyState icon="◌" title="No conversations" detail="Start one with the plus button." />}</div></aside>}
    <section className="conversation-main"><div className="conversation-header"><div><div className="eyebrow">{detail?.conversation.channel ? `${detail.conversation.channel.toUpperCase()} · CONVERSATION` : "CONVERSATION"}</div><h1>{title}</h1></div><div className="conversation-header-actions"><button className={`mode-toggle ${focus ? "active" : ""}`} onClick={onToggleFocus}>{focus ? "Focus" : "Inspect"}</button>{run && <StatusBadge status={presentation?.status ?? run.status} />}</div></div>{error && <div className="inline-error workspace-error">{error}</div>}{!detail && !loading && <EmptyState icon="◌" title="Choose a conversation" detail="Your durable conversations will appear here." />}{detail && <div className="message-scroll"><div className="message-list">{detail.messages.map((message) => <MessageCard key={message.id} message={message} />)}{run && presentation && (presentation.assistantText || presentation.currentTool || presentation.completedTools.length || presentation.subagents.length || run.status === "queued") && <LiveRunBlock run={run} presentation={presentation} />}</div></div>}<div className="composer-wrap"><div className="composer"><textarea value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} placeholder="Ask GravityClaw…" rows={1} disabled={!detail || sending} /><div className="composer-footer"><span className="composer-context">Workspace: <strong>gravityclaw</strong>{run?.status === "running" && <em> · follow-up will queue</em>}</span><button className="send-button" onClick={() => void send()} disabled={!draft.trim() || !detail || sending}>{sending ? "…" : "Send ↑"}</button></div></div></div></section>
    {!focus && <aside className="workspace-inspector"><div className="inspector-mini-head"><div className="eyebrow">INSPECTOR</div><span className="status-dot green" /></div>{run ? <><StatusBadge status={presentation?.status ?? run.status} large /><h2 className="inspector-run-title">{String(run.request.prompt ?? title)}</h2><Detail label="Workspace" value="gravityclaw" /><Detail label="Context" value={String(run.request.context_profile ?? "chat")} /><Detail label="Run version" value={`v${run.version}`} /><button className="secondary-button inspector-open" onClick={() => onRunSelect(run)}>Open full inspector →</button>{run.status === "running" && <button className="danger-button" onClick={() => void cancelRun(run).catch((reason) => setError(reason instanceof Error ? reason.message : "Unable to stop run"))}>■ Stop run</button>}<div className="inspector-section"><div className="section-label">LIVE ACTIVITY</div>{presentation?.currentTool && <ActivityChip icon="⚙" text={presentation.currentTool.name} active />}{presentation?.subagents.map((agent) => <ActivityChip key={agent} icon="↳" text={agent} />)}{presentation?.completedTools.slice(-3).map((tool) => <ActivityChip key={tool.id} icon="✓" text={tool.name} />)}</div></> : <EmptyState icon="◎" title="No active run" detail="Run details will appear here while GravityClaw works." />}</aside>}
  </div>;
}

function MessageCard({ message }: { message: Message }) { const user = message.role === "user"; return <article className={`message-card ${user ? "user" : "assistant"}`}><div className={`message-avatar ${user ? "user-avatar" : "agent-avatar"}`}>{user ? "A" : "✦"}</div><div className="message-content"><div className="message-meta"><strong>{user ? "You" : "GravityClaw"}</strong><span>{formatTime(message.created_at)}{message.source_run_id && <span className="message-source"> · {message.source_run_id.slice(0, 8)}</span>}</span></div><p>{message.content}</p></div></article>; }
function LiveRunBlock({ run, presentation }: { run: RunRecord; presentation: ReturnType<typeof presentationForRun> }) { return <article className="live-run-block"><div className="live-run-header"><StatusBadge status={presentation.status} /><span>{presentation.status === "queued" ? "Waiting for the current run to finish" : "Observable activity"}</span></div>{presentation.completedTools.map((tool) => <ActivityChip key={tool.id} icon="✓" text={tool.name} />)}{presentation.currentTool && <ActivityChip icon="⚙" text={`${presentation.currentTool.name} · running`} active detail={presentation.currentTool.detail} />}{presentation.subagents.map((agent) => <ActivityChip key={agent} icon="↳" text={agent} />)}{presentation.assistantText && <p className="streaming-text">{presentation.assistantText}<span className="streaming-cursor">▋</span></p>}{run.status !== "running" && !presentation.assistantText && <div className="system-note">Run {run.status}. GravityClaw preserved the conversation state.</div>}</article>; }
function ActivityChip({ icon, text, active = false, detail }: { icon: string; text: string; active?: boolean; detail?: string }) { return <div className={`activity-chip ${active ? "active" : ""}`}><span>{icon}</span><div><strong>{text}</strong>{detail && <small>{detail}</small>}</div></div>; }

function StatCard({ label, value, detail, accent }: { label: string; value: string; detail: string; accent: string }) { return <div className={`stat-card ${accent}`}><div className="stat-label">{label}<span className="stat-glyph">✦</span></div><strong>{value}</strong><span>{detail}</span></div>; }
function PanelHeader({ title, link, onLink = () => undefined }: { title: string; link?: string; onLink?: () => void }) { return <div className="panel-header"><h2>{title}</h2>{link && <button className="text-link" onClick={onLink}>{link} →</button>}</div>; }
function RunRow({ run, onClick }: { run: RunRecord; onClick: () => void }) { return <button className="run-row" onClick={onClick}><StatusBadge status={run.status} /><div><strong>{String(run.request.prompt ?? "Untitled run")}</strong><span>{run.request.context_profile ?? "chat"} · {formatTime(run.created_at)}</span></div><span className="row-arrow">→</span></button>; }
function ActivityRow({ event }: { event: PersistedEvent }) { const label = event.event_type.replaceAll(".", " · "); return <div className="activity-row"><span className={`activity-icon ${event.event_type.includes("failed") ? "error" : event.event_type.includes("running") ? "active" : ""}`}>{event.event_type.includes("completed") ? "✓" : event.event_type.includes("failed") ? "!" : event.event_type.includes("tool") ? "⚙" : "•"}</span><div><strong>{label}</strong><span>{event.run_id.slice(0, 8)} · {formatTime(event.created_at)}</span></div><span className="activity-cursor">#{event.id}</span></div>; }
function StatusBadge({ status, large = false }: { status: string; large?: boolean }) { const labels: Record<string, string> = { running: "Running", queued: "Queued", completed: "Completed", failed: "Failed", cancelled: "Cancelled", interrupted: "Interrupted", orphaned: "Orphaned" }; return <span className={`status-badge ${status} ${large ? "large" : ""}`}><span className="status-dot" />{labels[status] ?? status}</span>; }
function Detail({ label, value }: { label: string; value: string }) { return <div className="detail-row"><span>{label}</span><strong>{value}</strong></div>; }
function EmptyState({ icon, title, detail }: { icon: string; title: string; detail: string }) { return <div className="empty-state"><span className="empty-icon">{icon}</span><strong>{title}</strong><span>{detail}</span></div>; }
function ComingSoon({ title }: { title: string }) { return <div className="page centered fade-in"><div className="placeholder-icon">✦</div><div className="eyebrow">M8B FOUNDATION</div><h1>{title}</h1><p className="muted">This surface is connected to the control plane and scheduled for the next M8B slice.</p></div>; }
function nextHeartbeat(snapshot: ControlState["snapshot"]): string { const schedule = snapshot?.next_schedules.find((item) => item.trigger_type === "heartbeat"); return schedule ? "18m" : "—"; }
function formatTime(value: string): string { const date = new Date(value); return Number.isNaN(date.getTime()) ? "now" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }

export default App;
