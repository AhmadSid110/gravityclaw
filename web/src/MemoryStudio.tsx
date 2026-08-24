import { useEffect, useState } from "react";
import {
  getIdentity,
  getIdentityHistory,
  getJournal,
  getJournals,
  getMemories,
  getMemory,
  getCuratorStatus,
  updateCuratorSettings,
  consolidateJournals,
  rememberExplicit,
  getMemoryRevisions,
  searchMemories,
  updateIdentity,
  updateJournal,
} from "./api";
import type {
  IdentityDocument,
  JournalRecord,
  MemoryRecord,
  MemoryUsage,
  CuratorStatus,
  MemoryRevision,
  ConsolidationReport,
} from "./types";

type StudioTab = "Memory" | "Curator & Revisions" | "Daily Journal" | "Identity" | "Search";
const identityOrder = ["SOUL.md", "USER.md", "AGENTS.md", "TOOLS.md", "MEMORY.md", "HEARTBEAT.md"];

export function MemoryStudio() {
  const [tab, setTab] = useState<StudioTab>("Memory");
  const [identity, setIdentity] = useState<IdentityDocument[]>([]);
  const [memories, setMemories] = useState<MemoryRecord[]>([]);
  const [journals, setJournals] = useState<JournalRecord[]>([]);
  const [curatorStatus, setCuratorStatus] = useState<CuratorStatus | null>(null);
  const [revisions, setRevisions] = useState<MemoryRevision[]>([]);
  const [selectedMemory, setSelectedMemory] = useState<MemoryUsage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [rememberOpen, setRememberOpen] = useState(false);
  const [rememberText, setRememberText] = useState("");
  const [rememberCategory, setRememberCategory] = useState("user_preference");
  const [rememberBusy, setRememberBusy] = useState(false);
  const [dreamingBusy, setDreamingBusy] = useState(false);
  const [dreamingReport, setDreamingReport] = useState<ConsolidationReport | null>(null);

  const loadData = () => {
    let cancelled = false;
    void Promise.all([
      getIdentity(),
      getMemories(),
      getJournals(),
      getCuratorStatus().catch(() => null),
      getMemoryRevisions().catch(() => []),
    ]).then(([docs, records, days, status, revs]) => {
      if (cancelled) return;
      setIdentity(docs);
      setMemories(records);
      setJournals(days);
      if (status) setCuratorStatus(status);
      if (revs) setRevisions(revs);
    }).catch((reason) => !cancelled && setError(messageOf(reason, "Unable to load memory studio")));
    return () => { cancelled = true; };
  };

  useEffect(() => {
    return loadData();
  }, []);

  const handleModeChange = async (newMode: "manual" | "assisted" | "automatic") => {
    try {
      await updateCuratorSettings(newMode);
      setCuratorStatus((prev) => prev ? { ...prev, mode: newMode } : null);
      setToast(`Curator mode updated to ${newMode}`);
      setTimeout(() => setToast(null), 3000);
    } catch (err) {
      setError(messageOf(err, "Failed to update curator mode"));
    }
  };

  const handleDreaming = async () => {
    setDreamingBusy(true);
    setError(null);
    try {
      const report = await consolidateJournals(7);
      setDreamingReport(report);
      loadData();
    } catch (err) {
      setError(messageOf(err, "Consolidation failed"));
    } finally {
      setDreamingBusy(false);
    }
  };

  const handleRememberSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!rememberText.trim()) return;
    setRememberBusy(true);
    try {
      const res = await rememberExplicit(rememberText.trim(), rememberCategory);
      setRememberText("");
      setRememberOpen(false);
      setToast((res as { message?: string }).message || "Saved to long-term memory");
      setTimeout(() => setToast(null), 4000);
      loadData();
    } catch (err) {
      setError(messageOf(err, "Failed to remember fact"));
    } finally {
      setRememberBusy(false);
    }
  };

  return (
    <div className="page fade-in memory-studio">
      <div className="page-heading">
        <div>
          <div className="eyebrow">PERSISTENT AGENT KNOWLEDGE</div>
          <h1>Memory Studio</h1>
          <p className="muted">Governed curation, long-term memory, supersessions, and daily journals.</p>
        </div>
        <div className="studio-heading-actions">
          <button className="secondary-button" onClick={() => setRememberOpen(true)}>
            ✦ Remember a Fact
          </button>
          <button className="primary-button" disabled={dreamingBusy} onClick={handleDreaming}>
            {dreamingBusy ? "Consolidating…" : "🌙 Consolidate Journals"}
          </button>
        </div>
      </div>

      {toast && <div className="toast-banner success-toast">{toast}</div>}
      {error && <div className="inline-error studio-error" role="alert">{error}</div>}

      {/* Memory Curator Status Panel */}
      <section className="learning-status-panel panel">
        <div className="status-grid">
          <div className="status-item">
            <span className="status-label">Curator Mode</span>
            <div className="mode-selector-pill">
              {(["manual", "assisted", "automatic"] as const).map((m) => (
                <button
                  key={m}
                  className={`mode-btn ${curatorStatus?.mode === m ? "active" : ""}`}
                  onClick={() => handleModeChange(m)}
                >
                  {m.charAt(0).toUpperCase() + m.slice(1)}
                  {m === "assisted" && " (Default)"}
                </button>
              ))}
            </div>
          </div>
          <div className="status-item">
            <span className="status-label">Memory Curator</span>
            <span className="status-value positive">
              <span className="status-dot green" /> Active
            </span>
          </div>
          <div className="status-item">
            <span className="status-label">Contradiction Engine</span>
            <span className="status-value positive">
              <span className="status-dot green" /> Supersession Guard
            </span>
          </div>
          <div className="status-item">
            <span className="status-label">Curated Records</span>
            <span className="status-value">{memories.filter((m) => m.kind !== "episodic").length}</span>
          </div>
          <div className="status-item">
            <span className="status-label">Historical Revisions</span>
            <span className="status-value">{revisions.length}</span>
          </div>
        </div>
      </section>

      {/* Dreaming / Consolidation Result Modal */}
      {dreamingReport && (
        <section className="panel dreaming-report-panel">
          <div className="panel-header">
            <div>
              <span className="eyebrow">CONSOLIDATION COMPLETE</span>
              <h2>Episodic Memory Consolidation Report</h2>
            </div>
            <button className="secondary-button" onClick={() => setDreamingReport(null)}>Dismiss</button>
          </div>
          <p className="dreaming-summary">{dreamingReport.summary}</p>
          <div className="metrics-grid">
            <div className="metric-box">
              <strong>{dreamingReport.journals_scanned}</strong>
              <span>Journals Scanned</span>
            </div>
            <div className="metric-box">
              <strong>{dreamingReport.entries_analyzed}</strong>
              <span>Entries Analyzed</span>
            </div>
            <div className="metric-box">
              <strong>{dreamingReport.candidates_discovered}</strong>
              <span>Candidates Discovered</span>
            </div>
            <div className="metric-box highlight">
              <strong>{dreamingReport.memories_promoted}</strong>
              <span>Memories Promoted</span>
            </div>
            <div className="metric-box">
              <strong>{dreamingReport.memories_superseded}</strong>
              <span>Superseded</span>
            </div>
          </div>
        </section>
      )}

      {/* Explicit Remember Dialog */}
      {rememberOpen && (
        <div className="modal-backdrop" onClick={() => setRememberOpen(false)}>
          <div className="modal-card panel" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>✦ Explicit Memory Intent</h2>
              <button className="close-btn" onClick={() => setRememberOpen(false)}>✕</button>
            </div>
            <form onSubmit={handleRememberSubmit}>
              <p className="modal-desc">
                Explicitly record a durable rule, preference, or architectural fact. High-trust directives bypass approval queues and update MEMORY.md and FTS5 directly.
              </p>
              <div className="form-group">
                <label>Category</label>
                <select
                  className="select-input"
                  value={rememberCategory}
                  onChange={(e) => setRememberCategory(e.target.value)}
                >
                  <option value="user_preference">User Preference (e.g. "Use rootless Podman")</option>
                  <option value="project_decision">Project Decision (e.g. "Single FastAPI gateway")</option>
                  <option value="persistent_constraint">Persistent Constraint (e.g. "No public ingress")</option>
                  <option value="stable_fact">Stable System Fact</option>
                  <option value="configuration_decision">Configuration Decision</option>
                </select>
              </div>
              <div className="form-group">
                <label>Fact or Preference to Remember</label>
                <textarea
                  className="text-input"
                  rows={4}
                  placeholder="e.g. Always use rootless Podman for GravityClaw workers."
                  value={rememberText}
                  onChange={(e) => setRememberText(e.target.value)}
                  required
                />
              </div>
              <div className="modal-actions">
                <button type="button" className="secondary-button" onClick={() => setRememberOpen(false)}>
                  Cancel
                </button>
                <button type="submit" className="primary-button" disabled={rememberBusy || !rememberText.trim()}>
                  {rememberBusy ? "Saving…" : "Save to Memory"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      <div className="studio-tabs" role="tablist">
        {(["Memory", "Curator & Revisions", "Daily Journal", "Identity", "Search"] as StudioTab[]).map((item) => (
          <button key={item} className={tab === item ? "active" : ""} onClick={() => setTab(item)}>
            {item}
            {item === "Curator & Revisions" && revisions.length > 0 && (
              <span className="tab-badge">{revisions.length}</span>
            )}
          </button>
        ))}
      </div>

      {tab === "Memory" && (
        <MemoryHome
          memories={memories}
          selected={selectedMemory}
          onOpen={(record) =>
            void getMemory(record.id)
              .then(setSelectedMemory)
              .catch((reason) => setError(messageOf(reason, "Unable to inspect memory")))
          }
          onRememberClick={() => setRememberOpen(true)}
        />
      )}

      {tab === "Curator & Revisions" && (
        <RevisionsView revisions={revisions} />
      )}

      {tab === "Daily Journal" && <JournalView journals={journals} onError={setError} />}
      {tab === "Identity" && <IdentityView documents={identity} onChange={setIdentity} onError={setError} />}
      {tab === "Search" && <MemorySearch onError={setError} />}
    </div>
  );
}

function MemoryHome({
  memories,
  selected,
  onOpen,
  onRememberClick,
}: {
  memories: MemoryRecord[];
  selected: MemoryUsage | null;
  onOpen: (record: MemoryRecord) => void;
  onRememberClick: () => void;
}) {
  const pinned = memories.filter((item) => item.kind !== "episodic");
  const recent = memories.filter((item) => item.kind === "episodic").slice(0, 12);

  return (
    <div className="memory-home">
      <section className="studio-hero panel">
        <div>
          <span className="eyebrow">4-LAYER GOVERNED KNOWLEDGE</span>
          <h2>Curated memory & episodic journals, with receipts.</h2>
          <p>
            <strong>Layer 1:</strong> Curated Long-Term Memory (FTS5 + MEMORY.md) · <strong>Layer 2:</strong> Governed Learned Skills · <strong>Layer 3:</strong> Daily Episodic Journals · <strong>Layer 4:</strong> Conversation Archive.
          </p>
        </div>
        <div className="memory-count">
          <strong>{pinned.length}</strong>
          <span>curated memories</span>
        </div>
      </section>

      <div className="memory-columns">
        <section className="panel studio-list">
          <div className="panel-header">
            <div>
              <h2>Curated Long-Term Memory</h2>
              <span className="muted-text">Authored rules, project decisions, and user preferences</span>
            </div>
            <span className="muted-badge">{pinned.length} records</span>
          </div>
          {pinned.length ? (
            pinned.map((item) => <MemoryRow key={item.id} record={item} onOpen={onOpen} />)
          ) : (
            <div className="empty-state">
              <span className="empty-icon">✦</span>
              <strong>No curated memories yet</strong>
              <span>
                Use the Memory Curator or type <code>/remember &lt;fact&gt;</code> in chat to promote durable project decisions.
              </span>
              <button className="primary-button" style={{ marginTop: 12 }} onClick={onRememberClick}>
                ✦ Remember a Fact
              </button>
            </div>
          )}
        </section>

        <section className="panel studio-list">
          <div className="panel-header">
            <div>
              <h2>Episodic History & Daily Entries</h2>
              <span className="muted-text">Turn-by-turn interactions before memory consolidation</span>
            </div>
            <span className="muted-badge">{recent.length} records</span>
          </div>
          {recent.length ? (
            recent.map((item) => <MemoryRow key={item.id} record={item} onOpen={onOpen} />)
          ) : (
            <div className="empty-state">
              <span className="empty-icon">◷</span>
              <strong>No episodic records yet</strong>
              <span>Daily memory logs will appear automatically as runs execute.</span>
            </div>
          )}
        </section>
      </div>

      {selected && <MemoryDetail memory={selected} />}
    </div>
  );
}

function RevisionsView({
  revisions,
}: {
  revisions: MemoryRevision[];
}) {
  return (
    <div className="revisions-view panel">
      <div className="panel-header">
        <div>
          <span className="eyebrow">MEMORY GOVERNANCE</span>
          <h2>Supersession & Revision History</h2>
          <p className="muted">
            When you update a preference or rule, GravityClaw tracks previous versions and supersession reasons rather than silently deleting or blindly duplicating records.
          </p>
        </div>
        <span className="muted-badge">{revisions.length} supersessions</span>
      </div>

      {revisions.length === 0 ? (
        <div className="empty-state" style={{ padding: 48 }}>
          <span className="empty-icon">↺</span>
          <strong>No memory revisions recorded yet</strong>
          <span>
            When a new directive supersedes an older fact (e.g. changing deployment target from Modal to VPS), the revision history with previous contents will be preserved here.
          </span>
        </div>
      ) : (
        <div className="revision-cards-list">
          {revisions.map((rev) => (
            <div key={rev.id} className="revision-card">
              <div className="rev-header">
                <span className="rev-number">Revision #{rev.revision}</span>
                <span className="rev-meta">
                  Superseded: {formatDate(rev.superseded_at)} · {rev.source_run_id ? `Run #${rev.source_run_id.slice(0, 8)}` : "User update"}
                </span>
              </div>
              <div className="rev-diff-grid">
                <div className="diff-col prev-col">
                  <span className="diff-tag old">Previous Memory</span>
                  <div className="diff-box old-box">{rev.previous_content}</div>
                </div>
                <div className="diff-arrow">→</div>
                <div className="diff-col curr-col">
                  <span className="diff-tag new">Current Active Memory</span>
                  <div className="diff-box new-box">{rev.new_content}</div>
                </div>
              </div>
              {rev.reason && <div className="rev-reason">Reason: {rev.reason}</div>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function MemoryRow({ record, onOpen }: { record: MemoryRecord; onOpen: (record: MemoryRecord) => void }) {
  return (
    <button className="memory-row" onClick={() => onOpen(record)}>
      <div className={`memory-kind ${record.kind}`}>{record.kind === "episodic" ? "◷" : "✦"}</div>
      <div className="memory-row-copy">
        <strong>
          {record.content.slice(0, 92)}
          {record.content.length > 92 ? "…" : ""}
        </strong>
        <span>
          {record.source} · confidence {record.confidence.toFixed(2)} · {formatDate(record.created_at)}
        </span>
      </div>
      <span className="memory-arrow">→</span>
    </button>
  );
}

function MemoryDetail({ memory }: { memory: MemoryUsage }) {
  return (
    <section className="memory-detail panel">
      <div className="editor-head">
        <div>
          <div className="eyebrow">MEMORY PROVENANCE</div>
          <h2>{memory.id.slice(0, 14)}</h2>
          <span className="editor-meta">
            {memory.source} · {memory.kind} · confidence {memory.confidence.toFixed(2)}
          </span>
        </div>
        <span className="immutable-badge">FTS5 INDEXED</span>
      </div>
      <p className="memory-detail-content">{memory.content}</p>
      <div className="section-label">RECENT CONTEXT USAGE · {memory.usage.length} RUNS</div>
      {memory.usage.length ? (
        memory.usage.map((item) => (
          <div className="memory-usage" key={`${String(item.run_id)}-${String(item.label)}`}>
            <strong>Run #{String(item.run_id).slice(0, 10)}</strong>
            <span>
              {item.included ? "Included" : "Excluded"} ·{" "}
              {item.included ? `${item.estimated_tokens} tokens` : String(item.exclusion_reason ?? "policy")}
            </span>
          </div>
        ))
      ) : (
        <div className="muted memory-no-usage">No persisted context manifest has queried this record yet.</div>
      )}
    </section>
  );
}

function IdentityView({
  documents,
  onChange,
  onError,
}: {
  documents: IdentityDocument[];
  onChange: (docs: IdentityDocument[]) => void;
  onError: (value: string | null) => void;
}) {
  const [selected, setSelected] = useState("SOUL.md");
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [history, setHistory] = useState<IdentityDocument[]>([]);
  const [saving, setSaving] = useState(false);
  const document = documents.find((item) => item.name === selected) ?? documents[0];

  useEffect(() => {
    setDraft(document?.content ?? "");
    setEditing(false);
    setHistory([]);
  }, [document?.name, document?.sha256]);

  async function save() {
    if (!document) return;
    setSaving(true);
    onError(null);
    try {
      const updated = await updateIdentity(document.name, draft, document.version);
      onChange(documents.map((item) => (item.name === updated.name ? updated : item)));
      setEditing(false);
    } catch (reason) {
      onError(messageOf(reason, "Unable to save identity document"));
    } finally {
      setSaving(false);
    }
  }

  async function showHistory() {
    if (!document) return;
    try {
      setHistory(await getIdentityHistory(document.name));
    } catch (reason) {
      onError(messageOf(reason, "Unable to load revision history"));
    }
  }

  return (
    <div className="identity-studio">
      <aside className="identity-nav panel">
        <div className="panel-header">
          <h2>Identity files</h2>
        </div>
        {identityOrder.map((name) => (
          <button
            key={name}
            className={name === selected ? "selected" : ""}
            onClick={() => setSelected(name)}
          >
            <span>{name === "MEMORY.md" ? "✦" : "#"}</span>
            <strong>{name}</strong>
            {documents.find((item) => item.name === name) && (
              <small>v{documents.find((item) => item.name === name)?.version}</small>
            )}
          </button>
        ))}
      </aside>
      <section className="identity-editor panel">
        {document ? (
          <>
            <div className="editor-head">
              <div>
                <div className="eyebrow">VERSIONED DOCUMENT</div>
                <h2>{document.name}</h2>
                <span className="editor-meta">
                  v{document.version} · sha {document.sha256.slice(0, 12)} · {formatDate(document.updated_at)}
                </span>
              </div>
              <div className="editor-actions">
                <button className="secondary-button" onClick={() => void showHistory()}>
                  History
                </button>
                {editing ? (
                  <>
                    <button
                      className="secondary-button"
                      onClick={() => {
                        setDraft(document.content);
                        setEditing(false);
                      }}
                    >
                      Discard
                    </button>
                    <button className="primary-button" disabled={saving} onClick={() => void save()}>
                      {saving ? "Saving…" : "Save"}
                    </button>
                  </>
                ) : (
                  <button className="primary-button" onClick={() => setEditing(true)}>
                    Edit
                  </button>
                )}
              </div>
            </div>
            {editing ? (
              <textarea
                className="identity-textarea"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                spellCheck={false}
              />
            ) : (
              <pre className="identity-preview">{document.content}</pre>
            )}
            {history.length > 0 && (
              <div className="revision-list">
                <div className="section-label">REVISION HISTORY</div>
                {history.map((item) => (
                  <button key={`${item.name}-${item.version}`} onClick={() => setDraft(item.content)}>
                    <strong>v{item.version}</strong>
                    <span>{item.sha256.slice(0, 12)}</span>
                    <small>{formatDate(item.updated_at)}</small>
                  </button>
                ))}
              </div>
            )}
          </>
        ) : (
          <Empty title="No identity documents" detail="Bootstrap the agent home to create them." />
        )}
      </section>
    </div>
  );
}

function JournalView({
  journals,
  onError,
}: {
  journals: JournalRecord[];
  onError: (value: string | null) => void;
}) {
  const [selected, setSelected] = useState(journals[0]?.date ?? "");
  const [journal, setJournal] = useState<JournalRecord | null>(null);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    if (!selected && journals[0]) setSelected(journals[0].date);
  }, [journals, selected]);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    void getJournal(selected)
      .then((item) => {
        if (!cancelled) {
          setJournal(item);
          setDraft(item.content ?? "");
          setEditing(false);
        }
      })
      .catch((reason) => !cancelled && onError(messageOf(reason, "Unable to load journal")));
    return () => {
      cancelled = true;
    };
  }, [selected, onError]);

  async function save() {
    if (!journal) return;
    try {
      const updated = await updateJournal(journal.date, draft, journal.sha256);
      setJournal(updated);
      setDraft(updated.content ?? "");
      setEditing(false);
    } catch (reason) {
      onError(messageOf(reason, "Unable to save journal"));
    }
  }

  return (
    <div className="journal-studio">
      <aside className="journal-nav panel">
        <div className="panel-header">
          <h2>Daily journal</h2>
          <span className="muted-text">{journals.length} days</span>
        </div>
        {journals.map((item) => (
          <button
            key={item.date}
            className={item.date === selected ? "selected" : ""}
            onClick={() => setSelected(item.date)}
          >
            <strong>{formatJournalDate(item.date)}</strong>
            <small>{item.characters ?? 0} chars</small>
          </button>
        ))}
        {journals.length === 0 && (
          <Empty title="Journal is quiet" detail="Episodic memory creates daily files as it runs." />
        )}
      </aside>
      <section className="journal-editor panel">
        {journal ? (
          <>
            <div className="editor-head">
              <div>
                <div className="eyebrow">EPISODIC MEMORY</div>
                <h2>{formatJournalDate(journal.date)}</h2>
                <span className="editor-meta">sha {journal.sha256.slice(0, 12)}</span>
              </div>
              <div className="editor-actions">
                {editing ? (
                  <>
                    <button
                      className="secondary-button"
                      onClick={() => {
                        setDraft(journal.content ?? "");
                        setEditing(false);
                      }}
                    >
                      Discard
                    </button>
                    <button className="primary-button" onClick={() => void save()}>
                      Save
                    </button>
                  </>
                ) : (
                  <button className="primary-button" onClick={() => setEditing(true)}>
                    Edit source
                  </button>
                )}
              </div>
            </div>
            {editing ? (
              <textarea
                className="identity-textarea"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                spellCheck={false}
              />
            ) : (
              <pre className="identity-preview">{journal.content}</pre>
            )}
          </>
        ) : (
          <Empty title="Select a day" detail="Choose a journal date to inspect its source Markdown." />
        )}
      </section>
    </div>
  );
}

function MemorySearch({ onError }: { onError: (value: string | null) => void }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<MemoryRecord[]>([]);
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState("all");

  async function search() {
    if (!query.trim()) return;
    setBusy(true);
    onError(null);
    try {
      setResults(await searchMemories(query));
    } catch (reason) {
      onError(messageOf(reason, "Search failed"));
    } finally {
      setBusy(false);
    }
  }

  const visible =
    filter === "all" || filter === "identity"
      ? results
      : results.filter((record) => (filter === "episodic" ? record.kind === "episodic" : record.kind !== "episodic"));

  return (
    <section className="search-studio panel">
      <div className="search-bar">
        <input
          className="text-input"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void search();
          }}
          placeholder="Search durable memory…"
        />
        <button className="primary-button" disabled={busy || !query.trim()} onClick={() => void search()}>
          {busy ? "Searching…" : "Search"}
        </button>
      </div>
      <div className="filter-bar memory-filters">
        {[
          ["all", "All"],
          ["curated", "Long-term"],
          ["episodic", "Episodic"],
          ["identity", "Identity"],
        ].map(([value, label]) => (
          <button
            key={value}
            className={`filter ${filter === value ? "active" : ""}`}
            onClick={() => setFilter(value)}
          >
            {label}
          </button>
        ))}
      </div>
      {visible.length ? (
        <div className="search-results">
          {visible.map((record) => (
            <MemoryRow key={record.id} record={record} onOpen={() => undefined} />
          ))}
        </div>
      ) : (
        <Empty title="Search the agent's memory" detail="SQLite FTS results will include source and confidence." />
      )}
    </section>
  );
}

function Empty({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="studio-empty">
      <span>✦</span>
      <strong>{title}</strong>
      <small>{detail}</small>
    </div>
  );
}

function messageOf(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

function formatDate(value?: string): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleDateString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function formatJournalDate(value: string): string {
  const date = new Date(`${value}T12:00:00Z`);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleDateString([], { month: "long", day: "numeric", year: "numeric", timeZone: "UTC" });
}
