import { useEffect, useMemo, useState } from "react";
import { createAutomation, getAutomations, getWorkspaces, runAutomationNow, setAutomationEnabled, updateAutomation } from "./api";
import type { ScheduleRecord, TriggerRecord } from "./types";

const emptyForm = (workspaceId = "") => ({
  name: "New automation", trigger_type: "interval" as ScheduleRecord["trigger_type"], expression: "1800",
  timezone: "UTC", prompt: "Inspect the workspace and report only actionable findings.",
  context_profile: "scheduled", workspace_id: workspaceId, conversation_policy: "new" as ScheduleRecord["conversation_policy"],
  concurrency_policy: "QUEUE" as ScheduleRecord["concurrency_policy"], misfire_policy: "MISFIRE_RUN_ONCE" as ScheduleRecord["misfire_policy"],
  misfire_grace_seconds: 3600, notification_policy: "silent" as ScheduleRecord["notification_policy"],
  notification_channel: null as string | null, notification_chat_id: null as string | null, start_at: null as string | null,
});

export function AutomationsStudio() {
  const [items, setItems] = useState<ScheduleRecord[]>([]);
  const [workspaces, setWorkspaces] = useState<Array<{ id: string; name: string }>>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState(emptyForm());
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  async function refresh() {
    try {
      const [schedules, spaces] = await Promise.all([getAutomations(), getWorkspaces()]);
      setItems(schedules); setWorkspaces(spaces);
      setSelectedId((current) => current && schedules.some((item) => item.id === current) ? current : schedules[0]?.id ?? null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to load automations"); }
  }
  useEffect(() => { void refresh(); }, []);
  const selected = items.find((item) => item.id === selectedId) ?? null;
  const workspaceName = (id: string) => workspaces.find((item) => item.id === id)?.name ?? id.slice(0, 8);
  const beginCreate = () => { setForm(emptyForm(workspaces[0]?.id ?? "")); setCreating(true); setEditing(false); setError(null); };
  const beginEdit = () => { if (!selected) return; setForm({ ...selected, start_at: null }); setEditing(true); setCreating(false); setError(null); };
  const setField = (key: string, value: string | number | null) => setForm((current) => ({ ...current, [key]: value }));
  async function save() {
    setBusy("save"); setError(null);
    try {
      if (creating) await createAutomation(form as Parameters<typeof createAutomation>[0]);
      else if (selected) await updateAutomation({ ...selected, ...form } as ScheduleRecord);
      setCreating(false); setEditing(false); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to save automation"); }
    finally { setBusy(null); }
  }
  async function toggle() {
    if (!selected) return; setBusy("toggle"); setError(null);
    try { await setAutomationEnabled(selected, !selected.enabled); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "This automation changed in another tab"); }
    finally { setBusy(null); }
  }
  async function runNow() {
    if (!selected) return; setBusy("run"); setError(null);
    try { await runAutomationNow(selected.id, crypto.randomUUID()); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to create durable trigger"); }
    finally { setBusy(null); }
  }
  return <div className="page fade-in">
    <div className="page-heading"><div><div className="eyebrow">AUTONOMY</div><h1>Automations</h1><p className="muted">When GravityClaw should act, with every occurrence durable and inspectable.</p></div><button className="primary-button automation-new" onClick={beginCreate}>+ New automation</button></div>
    {error && <div className="inline-error studio-error" role="alert">{error}</div>}
    {(creating || editing) && <AutomationForm form={form} workspaces={workspaces} creating={creating} busy={busy === "save"} setField={setField} onCancel={() => { setCreating(false); setEditing(false); }} onSave={() => void save()} />}
    {!creating && !editing && <div className="automation-layout"><section className="panel automation-list"><div className="panel-header"><h2>Schedules</h2><span className="mono">{items.length} configured</span></div>{items.map((item) => <button className={`automation-row ${selectedId === item.id ? "selected" : ""}`} key={item.id} onClick={() => setSelectedId(item.id)}><span className={`automation-state ${item.enabled ? "enabled" : "paused"}`}>●</span><div><strong>{item.name}</strong><span>{scheduleSummary(item)} · {workspaceName(item.workspace_id)}</span></div><small>{item.enabled ? formatNext(item.next_run_at) : "Paused"}</small></button>)}{items.length === 0 && <div className="studio-empty"><span>◷</span><strong>No automations yet</strong><small>Create a heartbeat, interval, cron, or one-shot task.</small></div>}</section><section className="panel automation-detail">{selected ? <AutomationDetail schedule={selected} workspaceName={workspaceName(selected.workspace_id)} busy={busy} onToggle={() => void toggle()} onRunNow={() => void runNow()} onEdit={beginEdit} /> : <div className="studio-empty"><span>◷</span><strong>Select an automation</strong><small>Its recurrence, policy, and durable occurrence ledger will appear here.</small></div>}</section></div>}
  </div>;
}

function AutomationForm({ form, workspaces, creating, busy, setField, onCancel, onSave }: { form: ReturnType<typeof emptyForm> | ScheduleRecord; workspaces: Array<{ id: string; name: string }>; creating: boolean; busy: boolean; setField: (key: string, value: string | number | null) => void; onCancel: () => void; onSave: () => void }) {
  return <section className="panel automation-form"><div className="editor-head"><div><div className="eyebrow">{creating ? "NEW AUTOMATION" : "EDIT AUTOMATION"}</div><h2>{creating ? "Create automation" : "Update schedule"}</h2><span className="editor-meta">Changes publish as a new schedule generation.</span></div><div className="editor-actions"><button className="secondary-button" onClick={onCancel}>Cancel</button><button className="primary-button" onClick={onSave} disabled={busy || !form.workspace_id}>{busy ? "Saving…" : creating ? "Create" : "Save changes"}</button></div></div><div className="automation-form-grid"><label><span className="field-label">Name</span><input className="text-input" value={form.name} onChange={(e) => setField("name", e.target.value)} /></label><label><span className="field-label">When</span><select className="text-input" value={form.trigger_type} onChange={(e) => { const value = e.target.value as ScheduleRecord["trigger_type"]; setField("trigger_type", value); setField("context_profile", value === "heartbeat" ? "heartbeat" : "scheduled"); }}><option value="interval">Interval</option><option value="cron">Cron</option><option value="one_shot">Once</option><option value="heartbeat">Heartbeat</option></select></label><label><span className="field-label">Expression</span><input className="text-input" value={form.expression} onChange={(e) => setField("expression", e.target.value)} placeholder="seconds or cron expression" /></label><label><span className="field-label">Timezone</span><input className="text-input" value={form.timezone} onChange={(e) => setField("timezone", e.target.value)} placeholder="Asia/Kolkata" /></label><label><span className="field-label">Workspace</span><select className="text-input" value={form.workspace_id} onChange={(e) => setField("workspace_id", e.target.value)}>{workspaces.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label><span className="field-label">Concurrency</span><select className="text-input" value={form.concurrency_policy} onChange={(e) => setField("concurrency_policy", e.target.value)}><option>SKIP</option><option>QUEUE</option><option>REPLACE</option></select></label><label className="form-wide"><span className="field-label">Task</span><textarea className="text-input automation-prompt" value={form.prompt} onChange={(e) => setField("prompt", e.target.value)} rows={3} /></label></div></section>;
}

function AutomationDetail({ schedule, workspaceName, busy, onToggle, onRunNow, onEdit }: { schedule: ScheduleRecord; workspaceName: string; busy: string | null; onToggle: () => void; onRunNow: () => void; onEdit: () => void }) {
  const triggers = useMemo(() => (schedule.triggers ?? []).slice().reverse(), [schedule.triggers]);
  return <><div className="automation-detail-head"><div><div className="eyebrow">{schedule.trigger_type.toUpperCase()} · AUTOMATION</div><h2>{schedule.name} <span className={`automation-state ${schedule.enabled ? "enabled" : "paused"}`}>●</span></h2><p className="muted">{schedule.prompt}</p></div><span className={`state-pill ${schedule.enabled ? "healthy" : "muted"}`}>{schedule.enabled ? "Enabled" : "Paused"}</span></div><div className="automation-facts"><Fact label="Schedule" value={scheduleSummary(schedule)} /><Fact label="Timezone" value={schedule.timezone} /><Fact label="Workspace" value={workspaceName} /><Fact label="Context" value={schedule.context_profile.toUpperCase()} /><Fact label="Concurrency" value={schedule.concurrency_policy} /><Fact label="Misfire" value={schedule.misfire_policy.replace("MISFIRE_", "")} /><Fact label="Next run" value={formatNext(schedule.next_run_at)} /><Fact label="Generation" value={`#${schedule.generation} · v${schedule.version}`} /></div><div className="automation-actions"><button className="primary-button" onClick={onRunNow} disabled={busy !== null}>{busy === "run" ? "Creating trigger…" : "▶ Run now"}</button><button className="secondary-button" onClick={onToggle} disabled={busy !== null}>{busy === "toggle" ? "Publishing…" : schedule.enabled ? "Pause" : "Enable"}</button><button className="secondary-button" onClick={onEdit} disabled={busy !== null}>Edit</button></div><div className="occurrence-section"><div className="panel-header"><h2>Recent occurrences</h2><span className="mono">durable trigger ledger</span></div>{triggers.length === 0 && <div className="empty-source">No occurrences have been materialized yet.</div>}{triggers.map((trigger) => <Occurrence key={trigger.id} trigger={trigger} />)}</div></>;
}

function Occurrence({ trigger }: { trigger: TriggerRecord }) { return <div className="occurrence-row"><span className={`trigger-dot ${trigger.state.toLowerCase()}`} /> <div><strong>{trigger.state}</strong><span>{formatDate(trigger.scheduled_for)} · attempt {trigger.attempt_count}</span></div><small>{trigger.decision_reason || (trigger.run_id ? `run ${trigger.run_id.slice(0, 8)}` : "awaiting dispatch")}</small></div>; }
function Fact({ label, value }: { label: string; value: string }) { return <div className="automation-fact"><span>{label}</span><strong>{value}</strong></div>; }
function scheduleSummary(schedule: Pick<ScheduleRecord, "trigger_type" | "expression">) { if (schedule.trigger_type === "heartbeat") return `Every ${formatInterval(schedule.expression)}`; if (schedule.trigger_type === "interval") return `Every ${formatInterval(schedule.expression)}`; if (schedule.trigger_type === "cron") return `Cron ${schedule.expression}`; return `Once · ${formatDate(schedule.expression)}`; }
function formatInterval(value: string) { const seconds = Number(value); if (!Number.isFinite(seconds)) return value; if (seconds % 3600 === 0) return `${seconds / 3600}h`; if (seconds % 60 === 0) return `${seconds / 60}m`; return `${seconds}s`; }
function formatNext(value: string | null) { return value ? formatDate(value) : "Complete"; }
function formatDate(value: string) { const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); }
