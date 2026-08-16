import { useEffect, useState } from "react";
import { getSession, login, logout } from "./api";
import { useControlReplay } from "./replay";
import type { ControlState, PersistedEvent, RunRecord } from "./types";

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

type View = typeof navItems[number][0];

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
  const title = navItems.find(([id]) => id === view)?.[2] ?? "Home";
  return <div className={`app ${theme}`}>
    <aside className={`sidebar ${mobileNav ? "open" : ""}`}>
      <div className="brand-lockup sidebar-brand"><div className="brand-mark">✦</div><div><strong>GravityClaw</strong><span>Control Center</span></div></div>
      <div className="workspace-switcher"><div className="workspace-icon">G</div><div><strong>GravityClaw</strong><span>Personal agent</span></div><span className="chevron">⌄</span></div>
      <nav className="primary-nav" aria-label="Primary navigation">
        {navItems.map(([id, icon, label]) => <button key={id} className={`nav-item ${view === id ? "active" : ""}`} onClick={() => { setView(id); setMobileNav(false); }}><span className="nav-icon">{icon}</span><span>{label}</span>{id === "runs" && state.activeRuns.length > 0 && <em>{state.activeRuns.length}</em>}</button>)}
      </nav>
      <div className="nav-divider" />
      <div className="section-label">SYSTEM</div>
      <button className="nav-item"><span className="nav-icon">◒</span><span>Usage</span></button>
      <button className="nav-item"><span className="nav-icon">⚙</span><span>Settings</span></button>
      <div className="sidebar-bottom"><div className="connection-line"><span className={`status-dot ${state.connection === "connected" ? "green" : "amber"}`} />{state.connection === "connected" ? "Live" : state.connection}</div><button className="user-chip" onClick={() => void onLogout()}><span className="avatar">A</span><span>Ahmad</span><span>⌄</span></button></div>
    </aside>
    {mobileNav && <button className="scrim" aria-label="Close navigation" onClick={() => setMobileNav(false)} />}
    <main className="main-shell">
      <header className="topbar"><button className="mobile-menu" onClick={() => setMobileNav(true)} aria-label="Open navigation">☰</button><div className="breadcrumb"><span>GravityClaw</span><span className="crumb-separator">/</span><strong>{title}</strong></div><div className="topbar-actions"><button className="palette-hint" onClick={() => setFocus(!focus)}><span>⌘</span><span>K</span></button><button className="icon-button" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} aria-label="Toggle theme">{theme === "dark" ? "☼" : "☾"}</button><span className="system-health"><span className="status-dot green" /> Healthy</span></div></header>
      <div className="content-shell">
        {view === "home" && <Home state={state} onRunSelect={setSelectedRun} />}
        {view === "runs" && <Runs state={state} onRunSelect={setSelectedRun} />}
        {view !== "home" && view !== "runs" && <ComingSoon title={title} />}
      </div>
      {selectedRun && <Inspector run={selectedRun} state={state} onClose={() => setSelectedRun(null)} />}
      <button className="command-fab" onClick={() => setFocus(!focus)} aria-label="Open command palette">⌘K</button>
    </main>
    <div className={`focus-toast ${focus ? "show" : ""}`}><strong>Command palette</strong><span>Global actions arrive with M8B.7</span><button onClick={() => setFocus(false)}>×</button></div>
  </div>;
}

function Home({ state, onRunSelect }: { state: ControlState; onRunSelect: (run: RunRecord) => void }) {
  const snapshot = state.snapshot;
  const active = state.activeRuns.filter((run) => run.status === "running");
  const queued = state.activeRuns.filter((run) => run.status === "queued");
  return <div className="page fade-in"><div className="page-heading"><div><div className="eyebrow">SUNDAY · AUGUST 16, 2026</div><h1>Good evening, Ahmad <span className="wave">✦</span></h1><p className="muted">GravityClaw is keeping watch.</p></div><div className="live-pill"><span className="status-dot green" /> {state.connection === "connected" ? "Live" : "Reconnecting"}</div></div>
    <section className="stat-grid"><StatCard label="Active runs" value={String(active.length)} detail={active.length ? "Agent activity in progress" : "Nothing running"} accent="blue" /><StatCard label="Queued" value={String(queued.length)} detail={queued.length ? "Waiting for a conversation lock" : "Queue is clear"} accent="violet" /><StatCard label="Next heartbeat" value={nextHeartbeat(snapshot)} detail="Autonomous check" accent="amber" /></section>
    <div className="content-grid"><section className="panel active-panel"><PanelHeader title="Active now" link={active.length ? "View all runs" : undefined} onLink={() => undefined} />{active.length === 0 && <EmptyState icon="◌" title="GravityClaw is quiet" detail="Start a conversation when you have something to build, inspect, or untangle." />}{active.map((run) => <RunRow key={run.id} run={run} onClick={() => onRunSelect(run)} />)}</section><section className="panel"><PanelHeader title="Up next" /><div className="schedule-row"><span className="schedule-icon">◷</span><div><strong>Main heartbeat</strong><span>Next evaluation in 18m</span></div><span className="schedule-state">Enabled</span></div><div className="schedule-row"><span className="schedule-icon subdued">◌</span><div><strong>Weekly dependency review</strong><span>Monday · 09:00 · gravityclaw</span></div><span className="schedule-state muted-text">Tomorrow</span></div></section></div>
    <section className="panel activity-panel"><PanelHeader title="Recent activity" link="Open runs" onLink={() => undefined} /><div className="activity-list">{state.activity.slice(-8).reverse().map((event) => <ActivityRow key={event.id} event={event} />)}</div></section>
  </div>;
}

function Runs({ state, onRunSelect }: { state: ControlState; onRunSelect: (run: RunRecord) => void }) {
  const runs = state.activeRuns;
  return <div className="page fade-in"><div className="page-heading"><div><div className="eyebrow">OPERATIONS</div><h1>Runs</h1><p className="muted">Every execution, one durable timeline.</p></div><button className="secondary-button">⌕ <span>Search runs</span></button></div><div className="filter-bar"><button className="filter active">All <span>⌄</span></button><button className="filter">Workspace <span>⌄</span></button><button className="filter">State <span>⌄</span></button><span className="filter-count">{runs.length} visible</span></div><section className="panel runs-table"><div className="table-head"><span>STATE</span><span>TASK</span><span>WORKSPACE</span><span>VERSION</span><span>TIME</span></div>{runs.length === 0 && <EmptyState icon="ϟ" title="No runs yet" detail="Your execution history will appear here." />}{runs.map((run) => <button className="table-row" key={run.id} onClick={() => onRunSelect(run)}><span><StatusBadge status={run.status} /></span><span className="task-cell"><strong>{String(run.request.prompt ?? "Untitled run")}</strong><small>{run.id.slice(0, 8)} · {run.request.context_profile ?? "chat"}</small></span><span className="workspace-cell">gravityclaw</span><span className="mono">v{run.version}</span><span className="muted-text">{formatTime(run.created_at)}</span></button>)}</section></div>;
}

function Inspector({ run, state, onClose }: { run: RunRecord; state: ControlState; onClose: () => void }) {
  const [tab, setTab] = useState("Run");
  const tabs = ["Run", "Context", "Capabilities", "Events"];
  const events = state.activity.filter((event) => event.run_id === run.id).slice(-6).reverse();
  return <aside className="inspector-panel"><div className="inspector-header"><div><div className="eyebrow">RUN INSPECTOR</div><strong>{run.id.slice(0, 12)}</strong></div><button className="icon-button" onClick={onClose} aria-label="Close inspector">×</button></div><div className="inspector-tabs">{tabs.map((item) => <button key={item} className={tab === item ? "active" : ""} onClick={() => setTab(item)}>{item}</button>)}</div>{tab === "Run" && <div className="inspector-body"><StatusBadge status={run.status} large /><h2>{String(run.request.prompt ?? "Untitled run")}</h2><Detail label="Workspace" value="gravityclaw" /><Detail label="Context" value={String(run.request.context_profile ?? "chat")} /><Detail label="Backend" value="AGY container" /><Detail label="Run version" value={`v${run.version}`} /><button className="danger-button">■ Stop run</button></div>}{tab === "Events" && <div className="inspector-body"><div className="mini-timeline">{events.map((event) => <ActivityRow key={event.id} event={event} />)}</div>{events.length === 0 && <EmptyState icon="◎" title="No events yet" detail="Observable execution events will appear here." />}</div>}{tab === "Context" && <><InspectorPlaceholder title="Context manifest" detail="Identity, memory, provenance, budget, and artifact references will appear here." /><div className="inspector-meter"><span style={{ width: "72%" }} /></div></>}{tab === "Capabilities" && <InspectorPlaceholder title="Capability snapshot" detail="This run's immutable skills, MCP, network, and secret references." />}</aside>;
}

function StatCard({ label, value, detail, accent }: { label: string; value: string; detail: string; accent: string }) { return <div className={`stat-card ${accent}`}><div className="stat-label">{label}<span className="stat-glyph">✦</span></div><strong>{value}</strong><span>{detail}</span></div>; }
function PanelHeader({ title, link, onLink = () => undefined }: { title: string; link?: string; onLink?: () => void }) { return <div className="panel-header"><h2>{title}</h2>{link && <button className="text-link" onClick={onLink}>{link} →</button>}</div>; }
function RunRow({ run, onClick }: { run: RunRecord; onClick: () => void }) { return <button className="run-row" onClick={onClick}><StatusBadge status={run.status} /><div><strong>{String(run.request.prompt ?? "Untitled run")}</strong><span>{run.request.context_profile ?? "chat"} · {formatTime(run.created_at)}</span></div><span className="row-arrow">→</span></button>; }
function ActivityRow({ event }: { event: PersistedEvent }) { const label = event.event_type.replaceAll(".", " · "); return <div className="activity-row"><span className={`activity-icon ${event.event_type.includes("failed") ? "error" : event.event_type.includes("running") ? "active" : ""}`}>{event.event_type.includes("completed") ? "✓" : event.event_type.includes("failed") ? "!" : event.event_type.includes("tool") ? "⚙" : "•"}</span><div><strong>{label}</strong><span>{event.run_id.slice(0, 8)} · {formatTime(event.created_at)}</span></div><span className="activity-cursor">#{event.id}</span></div>; }
function StatusBadge({ status, large = false }: { status: string; large?: boolean }) { const labels: Record<string, string> = { running: "Running", queued: "Queued", completed: "Completed", failed: "Failed", cancelled: "Cancelled", interrupted: "Interrupted", orphaned: "Orphaned" }; return <span className={`status-badge ${status} ${large ? "large" : ""}`}><span className="status-dot" />{labels[status] ?? status}</span>; }
function Detail({ label, value }: { label: string; value: string }) { return <div className="detail-row"><span>{label}</span><strong>{value}</strong></div>; }
function InspectorPlaceholder({ title, detail }: { title: string; detail: string }) { return <div className="inspector-body"><div className="placeholder-icon">◎</div><h2>{title}</h2><p className="muted">{detail}</p><div className="placeholder-lines"><i /><i /><i /></div></div>; }
function EmptyState({ icon, title, detail }: { icon: string; title: string; detail: string }) { return <div className="empty-state"><span className="empty-icon">{icon}</span><strong>{title}</strong><span>{detail}</span></div>; }
function ComingSoon({ title }: { title: string }) { return <div className="page centered fade-in"><div className="placeholder-icon">✦</div><div className="eyebrow">M8B FOUNDATION</div><h1>{title}</h1><p className="muted">This surface is connected to the control plane and scheduled for the next M8B slice.</p></div>; }
function nextHeartbeat(snapshot: ControlState["snapshot"]): string { const schedule = snapshot?.next_schedules.find((item) => item.trigger_type === "heartbeat"); return schedule ? "18m" : "—"; }
function formatTime(value: string): string { const date = new Date(value); return Number.isNaN(date.getTime()) ? "now" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }

export default App;
