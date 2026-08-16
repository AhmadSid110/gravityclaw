import { useEffect, useState } from "react";
import { getIdentity, getIdentityHistory, getJournal, getJournals, getMemories, getMemory, searchMemories, updateIdentity, updateJournal } from "./api";
import type { IdentityDocument, JournalRecord, MemoryRecord, MemoryUsage } from "./types";

type StudioTab = "Memory" | "Daily Journal" | "Identity" | "Search";
const identityOrder = ["SOUL.md", "USER.md", "AGENTS.md", "TOOLS.md", "MEMORY.md", "HEARTBEAT.md"];

export function MemoryStudio() {
  const [tab, setTab] = useState<StudioTab>("Memory");
  const [identity, setIdentity] = useState<IdentityDocument[]>([]);
  const [memories, setMemories] = useState<MemoryRecord[]>([]);
  const [journals, setJournals] = useState<JournalRecord[]>([]);
  const [selectedMemory, setSelectedMemory] = useState<MemoryUsage | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    void Promise.all([getIdentity(), getMemories(), getJournals()]).then(([docs, records, days]) => {
      if (cancelled) return;
      setIdentity(docs); setMemories(records); setJournals(days);
    }).catch((reason) => !cancelled && setError(messageOf(reason, "Unable to load memory studio")));
    return () => { cancelled = true; };
  }, []);
  return <div className="page fade-in memory-studio">
    <div className="page-heading"><div><div className="eyebrow">PERSISTENT AGENT</div><h1>Memory Studio</h1><p className="muted">What GravityClaw remembers across runs.</p></div><span className="studio-health"><span className="status-dot green" /> SQLite FTS · atomic state</span></div>
    {error && <div className="inline-error studio-error">{error}</div>}
    <div className="studio-tabs" role="tablist">{(["Memory", "Daily Journal", "Identity", "Search"] as StudioTab[]).map((item) => <button key={item} className={tab === item ? "active" : ""} onClick={() => setTab(item)}>{item}</button>)}</div>
    {tab === "Memory" && <MemoryHome memories={memories} selected={selectedMemory} onOpen={(record) => void getMemory(record.id).then(setSelectedMemory).catch((reason) => setError(messageOf(reason, "Unable to inspect memory")))} />}
    {tab === "Daily Journal" && <JournalView journals={journals} onError={setError} />}
    {tab === "Identity" && <IdentityView documents={identity} onChange={setIdentity} onError={setError} />}
    {tab === "Search" && <MemorySearch onError={setError} />}
  </div>;
}

function MemoryHome({ memories, selected, onOpen }: { memories: MemoryRecord[]; selected: MemoryUsage | null; onOpen: (record: MemoryRecord) => void }) {
  const pinned = memories.filter((item) => item.kind !== "episodic").slice(0, 4);
  const recent = memories.filter((item) => item.kind === "episodic").slice(0, 8);
  return <div className="memory-home"><section className="studio-hero panel"><div><span className="eyebrow">SECOND BRAIN</span><h2>Durable knowledge, with receipts.</h2><p>Memory is separate from context. Every retrieval keeps its source, confidence, and run provenance.</p></div><div className="memory-count"><strong>{memories.length}</strong><span>indexed records</span></div></section><div className="memory-columns"><section className="panel studio-list"><div className="panel-header"><h2>Pinned / long-term</h2><span className="muted-text">{pinned.length}</span></div>{pinned.length ? pinned.map((item) => <MemoryRow key={item.id} record={item} onOpen={onOpen} />) : <Empty title="No curated memories" detail="Curated knowledge will appear here." />}</section><section className="panel studio-list"><div className="panel-header"><h2>Recent episodic</h2><span className="muted-text">{recent.length}</span></div>{recent.length ? recent.map((item) => <MemoryRow key={item.id} record={item} onOpen={onOpen} />) : <Empty title="No journal entries" detail="Daily memory records will appear here." />}</section></div>{selected && <MemoryDetail memory={selected} />}</div>;
}

function MemoryRow({ record, onOpen }: { record: MemoryRecord; onOpen: (record: MemoryRecord) => void }) { return <button className="memory-row" onClick={() => onOpen(record)}><div className={`memory-kind ${record.kind}`}>{record.kind === "episodic" ? "◷" : "✦"}</div><div className="memory-row-copy"><strong>{record.content.slice(0, 92)}{record.content.length > 92 ? "…" : ""}</strong><span>{record.source} · confidence {record.confidence.toFixed(2)} · {formatDate(record.created_at)}</span></div><span className="memory-arrow">→</span></button>; }

function MemoryDetail({ memory }: { memory: MemoryUsage }) { return <section className="memory-detail panel"><div className="editor-head"><div><div className="eyebrow">MEMORY PROVENANCE</div><h2>{memory.id.slice(0, 14)}</h2><span className="editor-meta">{memory.source} · {memory.kind} · confidence {memory.confidence.toFixed(2)}</span></div><span className="immutable-badge">SOURCE LINKED</span></div><p className="memory-detail-content">{memory.content}</p><div className="section-label">RECENT USAGE · {memory.usage.length} RUNS</div>{memory.usage.length ? memory.usage.map((item) => <div className="memory-usage" key={`${String(item.run_id)}-${String(item.label)}`}><strong>Run #{String(item.run_id).slice(0, 10)}</strong><span>{item.included ? "Included" : "Excluded"} · {item.included ? `${item.estimated_tokens} tokens` : String(item.exclusion_reason ?? "policy")}</span></div>) : <div className="muted memory-no-usage">No persisted context manifest has used this memory yet.</div>}</section>; }

function IdentityView({ documents, onChange, onError }: { documents: IdentityDocument[]; onChange: (docs: IdentityDocument[]) => void; onError: (value: string | null) => void }) {
  const [selected, setSelected] = useState("SOUL.md");
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [history, setHistory] = useState<IdentityDocument[]>([]);
  const [saving, setSaving] = useState(false);
  const document = documents.find((item) => item.name === selected) ?? documents[0];
  useEffect(() => { setDraft(document?.content ?? ""); setEditing(false); setHistory([]); }, [document?.name, document?.sha256]);
  async function save() {
    if (!document) return;
    setSaving(true); onError(null);
    try { const updated = await updateIdentity(document.name, draft, document.version); onChange(documents.map((item) => item.name === updated.name ? updated : item)); setEditing(false); }
    catch (reason) { onError(messageOf(reason, "Unable to save identity document")); }
    finally { setSaving(false); }
  }
  async function showHistory() {
    if (!document) return;
    try { setHistory(await getIdentityHistory(document.name)); }
    catch (reason) { onError(messageOf(reason, "Unable to load revision history")); }
  }
  return <div className="identity-studio"><aside className="identity-nav panel"><div className="panel-header"><h2>Identity files</h2></div>{identityOrder.map((name) => <button key={name} className={name === selected ? "selected" : ""} onClick={() => setSelected(name)}><span>{name === "MEMORY.md" ? "✦" : "#"}</span><strong>{name}</strong>{documents.find((item) => item.name === name) && <small>v{documents.find((item) => item.name === name)?.version}</small>}</button>)}</aside><section className="identity-editor panel">{document ? <><div className="editor-head"><div><div className="eyebrow">VERSIONED DOCUMENT</div><h2>{document.name}</h2><span className="editor-meta">v{document.version} · sha {document.sha256.slice(0, 12)} · {formatDate(document.updated_at)}</span></div><div className="editor-actions"><button className="secondary-button" onClick={() => void showHistory()}>History</button>{editing ? <><button className="secondary-button" onClick={() => { setDraft(document.content); setEditing(false); }}>Discard</button><button className="primary-button" disabled={saving} onClick={() => void save()}>{saving ? "Saving…" : "Save"}</button></> : <button className="primary-button" onClick={() => setEditing(true)}>Edit</button>}</div></div>{editing ? <textarea className="identity-textarea" value={draft} onChange={(event) => setDraft(event.target.value)} spellCheck={false} /> : <pre className="identity-preview">{document.content}</pre>}{history.length > 0 && <div className="revision-list"><div className="section-label">REVISION HISTORY</div>{history.map((item) => <button key={`${item.name}-${item.version}`} onClick={() => setDraft(item.content)}><strong>v{item.version}</strong><span>{item.sha256.slice(0, 12)}</span><small>{formatDate(item.updated_at)}</small></button>)}</div>}</> : <Empty title="No identity documents" detail="Bootstrap the agent home to create them." />}</section></div>;
}

function JournalView({ journals, onError }: { journals: JournalRecord[]; onError: (value: string | null) => void }) {
  const [selected, setSelected] = useState(journals[0]?.date ?? "");
  const [journal, setJournal] = useState<JournalRecord | null>(null);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  useEffect(() => { if (!selected && journals[0]) setSelected(journals[0].date); }, [journals, selected]);
  useEffect(() => { if (!selected) return; let cancelled = false; void getJournal(selected).then((item) => { if (!cancelled) { setJournal(item); setDraft(item.content ?? ""); setEditing(false); } }).catch((reason) => !cancelled && onError(messageOf(reason, "Unable to load journal"))); return () => { cancelled = true; }; }, [selected, onError]);
  async function save() { if (!journal) return; try { const updated = await updateJournal(journal.date, draft, journal.sha256); setJournal(updated); setDraft(updated.content ?? ""); setEditing(false); } catch (reason) { onError(messageOf(reason, "Unable to save journal")); } }
  return <div className="journal-studio"><aside className="journal-nav panel"><div className="panel-header"><h2>Daily journal</h2><span className="muted-text">{journals.length} days</span></div>{journals.map((item) => <button key={item.date} className={item.date === selected ? "selected" : ""} onClick={() => setSelected(item.date)}><strong>{formatJournalDate(item.date)}</strong><small>{item.characters ?? 0} chars</small></button>)}{journals.length === 0 && <Empty title="Journal is quiet" detail="Episodic memory creates daily files as it runs." />}</aside><section className="journal-editor panel">{journal ? <><div className="editor-head"><div><div className="eyebrow">EPISODIC MEMORY</div><h2>{formatJournalDate(journal.date)}</h2><span className="editor-meta">sha {journal.sha256.slice(0, 12)}</span></div><div className="editor-actions">{editing ? <><button className="secondary-button" onClick={() => { setDraft(journal.content ?? ""); setEditing(false); }}>Discard</button><button className="primary-button" onClick={() => void save()}>Save</button></> : <button className="primary-button" onClick={() => setEditing(true)}>Edit source</button>}</div></div>{editing ? <textarea className="identity-textarea" value={draft} onChange={(event) => setDraft(event.target.value)} spellCheck={false} /> : <pre className="identity-preview">{journal.content}</pre>}</> : <Empty title="Select a day" detail="Choose a journal date to inspect its source Markdown." />}</section></div>;
}

function MemorySearch({ onError }: { onError: (value: string | null) => void }) {
  const [query, setQuery] = useState(""); const [results, setResults] = useState<MemoryRecord[]>([]); const [busy, setBusy] = useState(false); const [filter, setFilter] = useState("all");
  async function search() { if (!query.trim()) return; setBusy(true); onError(null); try { setResults(await searchMemories(query)); } catch (reason) { onError(messageOf(reason, "Search failed")); } finally { setBusy(false); } }
  const visible = filter === "all" || filter === "identity" ? results : results.filter((record) => filter === "episodic" ? record.kind === "episodic" : record.kind !== "episodic");
  return <section className="search-studio panel"><div className="search-bar"><input className="text-input" value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void search(); }} placeholder="Search durable memory…" /><button className="primary-button" disabled={busy || !query.trim()} onClick={() => void search()}>{busy ? "Searching…" : "Search"}</button></div><div className="filter-bar memory-filters">{[["all", "All"], ["curated", "Long-term"], ["episodic", "Episodic"], ["identity", "Identity"]].map(([value, label]) => <button key={value} className={`filter ${filter === value ? "active" : ""}`} onClick={() => setFilter(value)}>{label}</button>)}</div>{visible.length ? <div className="search-results">{visible.map((record) => <MemoryRow key={record.id} record={record} onOpen={() => undefined} />)}</div> : <Empty title="Search the agent's memory" detail="SQLite FTS results will include source and confidence." />}</section>;
}

function Empty({ title, detail }: { title: string; detail: string }) { return <div className="studio-empty"><span>✦</span><strong>{title}</strong><small>{detail}</small></div>; }
function messageOf(reason: unknown, fallback: string): string { return reason instanceof Error ? reason.message : fallback; }
function formatDate(value?: string): string { if (!value) return "—"; const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString([], { month: "short", day: "numeric" }); }
function formatJournalDate(value: string): string { const date = new Date(`${value}T12:00:00Z`); return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString([], { month: "long", day: "numeric", year: "numeric", timeZone: "UTC" }); }
