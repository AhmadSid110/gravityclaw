import { useEffect, useState } from "react";
import {
  approveProposal,
  archiveSkill,
  deleteLearningMemory,
  dismissMemoryCandidate,
  getLearningEvents,
  getLearningMemory,
  getLearningOverview,
  getLearningProposals,
  getLearningSettings,
  getLearningSkill,
  getLearningSkillRevisions,
  getLearningSkillRuns,
  getLearningSkills,
  getMemoryCandidates,
  pinSkill,
  promoteMemoryCandidate,
  rejectProposal,
  restoreSkill,
  rollbackSkill,
  scanMemoryCandidates,
  submitLearn,
  unpinSkill,
  updateLearningMemory,
  updateLearningSettings,
} from "./api";
import { JourneyGraphView } from "./JourneyGraph";
import type {
  LearningOverview,
  LearningEvent,
  MemoryCandidate,
  SkillProposal,
  LearnedSkill,
  SkillRevision,
  SkillRunEvent,
  LearningConfig,
  LearnResponse,
} from "./types";

type LearningView = "overview" | "candidates" | "proposals" | "skills" | "memory" | "settings" | "learn" | "journey";

export function LearningStudio() {
  const [view, setView] = useState<LearningView>("overview");

  const tabs: Array<[LearningView, string, string]> = [
    ["overview", "◎", "Overview"],
    ["candidates", "★", "Candidates"],
    ["proposals", "◆", "Proposals"],
    ["skills", "⊙", "Skills"],
    ["journey", "⟡", "Journey"],
    ["memory", "✦", "Memory"],
    ["learn", "↓", "Learn"],
    ["settings", "⚙", "Settings"],
  ];

  return (
    <div className="page fade-in">
      <div className="page-heading">
        <div>
          <div className="eyebrow">LEARNING MODE</div>
          <h1>Learning Studio</h1>
          <p className="muted">Visibility, governed skill proposals, memory curation, and trust.</p>
        </div>
      </div>
      <nav className="learning-tabs" aria-label="Learning Studio navigation">
        {tabs.map(([id, icon, label]) => (
          <button
            key={id}
            className={`learning-tab ${view === id ? "active" : ""}`}
            onClick={() => setView(id)}
          >
            <span>{icon}</span> {label}
          </button>
        ))}
      </nav>
      <div className="learning-content">
        {view === "overview" && <OverviewPage onNavigate={setView} />}
        {view === "candidates" && <CandidatesPage onNavigate={setView} />}
        {view === "proposals" && <ProposalsPage />}
        {view === "skills" && <SkillsPage />}
        {view === "journey" && <JourneyGraphView />}
        {view === "memory" && <MemoryPage />}
        {view === "learn" && <LearnPage />}
        {view === "settings" && <SettingsPage />}
      </div>
    </div>
  );
}

// ─── Overview Page with Status Panel & Contextual Zero-States ───────────────

function OverviewPage({ onNavigate }: { onNavigate: (tab: LearningView) => void }) {
  const [overview, setOverview] = useState<LearningOverview | null>(null);
  const [events, setEvents] = useState<LearningEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  const loadData = () => {
    let cancelled = false;
    Promise.all([getLearningOverview(), getLearningEvents(20)])
      .then(([o, e]) => {
        if (!cancelled) {
          setOverview(o);
          setEvents(e);
        }
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load");
      });
    return () => {
      cancelled = true;
    };
  };

  useEffect(() => {
    return loadData();
  }, []);

  const handleScan = async () => {
    setScanning(true);
    try {
      const res = await scanMemoryCandidates();
      setToast(`✓ Scan completed: found ${res.candidates_discovered} new candidate facts.`);
      loadData();
      setTimeout(() => setToast(null), 4000);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Scan failed");
    } finally {
      setScanning(false);
    }
  };

  if (error) return <div className="inline-error" role="alert">{error}</div>;
  if (!overview) return <div className="workspace-loading">Loading overview...</div>;

  const mode = overview.mode || "suggest";
  const engineStatus = overview.learning_engine_status || "active";
  const skillCount = overview.stats.skills ?? 0;
  const memoryCandidatesCount = overview.stats.memory_candidates ?? 0;
  const pendingProposalsCount = overview.stats.pending_proposals ?? 0;
  const curatedMemoriesCount = overview.stats.curated_memories ?? overview.stats.memories ?? 0;
  const dailyJournalsCount = overview.stats.daily_journals ?? 0;
  const totalMessagesCount = overview.stats.total_messages ?? 0;

  return (
    <div className="learning-overview">
      {toast && <div className="toast-banner" role="status">{toast}</div>}

      {/* Learning Status Panel */}
      <section className="panel learning-status-panel">
        <div className="panel-header">
          <h2>Learning System Status</h2>
          <button className="secondary-button" onClick={handleScan} disabled={scanning}>
            {scanning ? "Scanning…" : "↻ Scan Recent History"}
          </button>
        </div>
        <div className="learning-status-grid">
          <div className="status-item">
            <span className="muted-label">Mode</span>
            <strong>{mode === "suggest" ? "Suggest (Governed)" : mode === "automatic" ? "Automatic" : "Off"}</strong>
            <small className="muted">
              {mode === "suggest" ? "Discovers opportunities and asks before updating" : "Applies approved changes automatically"}
            </small>
          </div>
          <div className="status-item">
            <span className="muted-label">Learning Engine</span>
            <strong><span className="status-dot green" /> {engineStatus === "active" ? "Active" : "Idle"}</strong>
            <small className="muted">Scoring run eligibility & intent</small>
          </div>
          <div className="status-item">
            <span className="muted-label">Skill Registry</span>
            <strong><span className="status-dot green" /> Healthy</strong>
            <small className="muted">Governed versioned skills & revisions</small>
          </div>
          <div className="status-item">
            <span className="muted-label">Memory Index</span>
            <strong><span className="status-dot green" /> Healthy</strong>
            <small className="muted">SQLite FTS5 full-text search active</small>
          </div>
        </div>
      </section>

      {/* High-level KPI Stats */}
      <div className="stat-grid">
        <StatCard
          label="Learned Skills"
          value={String(skillCount)}
          detail={skillCount === 0 ? "0 indexed" : `${skillCount} active in context`}
          accent="violet"
        />
        <StatCard
          label="Long-term Memory"
          value={String(curatedMemoriesCount)}
          detail="Curated in FTS & MEMORY.md"
          accent="blue"
        />
        <StatCard
          label="Memory Candidates"
          value={String(memoryCandidatesCount)}
          detail={memoryCandidatesCount > 0 ? "Pending your review" : "Up to date"}
          accent={memoryCandidatesCount > 0 ? "amber" : "blue"}
        />
        <StatCard
          label="Skill Proposals"
          value={String(pendingProposalsCount)}
          detail={pendingProposalsCount > 0 ? "Awaiting review" : "None pending"}
          accent={pendingProposalsCount > 0 ? "amber" : "violet"}
        />
      </div>

      {/* Zero State Explanations when 0 */}
      <div className="learning-cards-grid">
        {/* Learned Skills Zero/Active Card */}
        <section className="panel zero-state-card">
          <div className="panel-header">
            <h3>Learned Skills</h3>
            <span className="pill-badge">{skillCount}</span>
          </div>
          {skillCount === 0 ? (
            <div className="zero-state-body">
              <p><strong>No learned skills yet.</strong></p>
              <p className="muted">
                GravityClaw can turn successful procedures and repeated workflows into reusable skills.
                Runs will automatically propose skills, or you can use <code>/learn</code> in chat.
              </p>
              <div className="zero-actions">
                <button className="secondary-button" onClick={() => onNavigate("settings")}>
                  View Learning Settings
                </button>
                <button className="primary-button" onClick={() => onNavigate("learn")}>
                  Propose a Skill
                </button>
              </div>
            </div>
          ) : (
            <div className="zero-state-body">
              <p>GravityClaw has <strong>{skillCount} active learned skills</strong> available to future context retrieval.</p>
              <div className="zero-actions">
                <button className="primary-button" onClick={() => onNavigate("skills")}>
                  Manage Skills →
                </button>
              </div>
            </div>
          )}
        </section>

        {/* Long-Term Memory Zero/Active Card */}
        <section className="panel zero-state-card">
          <div className="panel-header">
            <h3>Long-term Memory</h3>
            <span className="pill-badge">{curatedMemoriesCount}</span>
          </div>
          {curatedMemoriesCount === 0 ? (
            <div className="zero-state-body">
              <p><strong>No curated memories yet.</strong></p>
              <ul className="zero-stats-list">
                <li><strong>{dailyJournalsCount}</strong> daily journal entries</li>
                <li><strong>{totalMessagesCount}</strong> conversation messages indexed</li>
                <li><strong>{memoryCandidatesCount}</strong> potential memory candidates detected</li>
              </ul>
              <div className="zero-actions">
                <button className="primary-button" onClick={() => onNavigate("candidates")}>
                  Review Candidates ({memoryCandidatesCount})
                </button>
                <button className="secondary-button" onClick={() => onNavigate("memory")}>
                  Open Memory Studio
                </button>
              </div>
            </div>
          ) : (
            <div className="zero-state-body">
              <p><strong>{curatedMemoriesCount} curated records</strong> indexed in SQLite FTS5 with source provenance.</p>
              <div className="zero-actions">
                <button className="primary-button" onClick={() => onNavigate("candidates")}>
                  Candidates ({memoryCandidatesCount})
                </button>
                <button className="secondary-button" onClick={() => onNavigate("memory")}>
                  Open Memory Studio
                </button>
              </div>
            </div>
          )}
        </section>
      </div>

      {/* Recent Activity Audit */}
      <section className="panel">
        <div className="panel-header">
          <h2>Recent learning activity</h2>
        </div>
        {events.length === 0 && (
          <div className="empty-state">
            <span className="empty-icon">◌</span>
            <strong>No activity yet</strong>
            <span>Learning events and promotions will appear here.</span>
          </div>
        )}
        <div className="activity-list">
          {events.map((event) => (
            <div key={event.id} className="activity-row">
              <span className="activity-icon">{eventIcon(event.action)}</span>
              <div>
                <strong>{formatAction(event.action)}</strong>
                <span>{event.resource_id?.slice(0, 8)} · {formatTime(event.created_at)}</span>
              </div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

// ─── Candidates Page (Memory Candidates) ────────────────────────────────────

function CandidatesPage({ onNavigate: _onNavigate }: { onNavigate: (tab: LearningView) => void }) {
  const [candidates, setCandidates] = useState<MemoryCandidate[]>([]);
  const [filter, setFilter] = useState<string>("pending_approval");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);

  const loadCandidates = () => {
    let cancelled = false;
    setLoading(true);
    getMemoryCandidates(filter || undefined)
      .then((items) => {
        if (!cancelled) {
          setCandidates(items);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load candidates");
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  };

  useEffect(() => {
    return loadCandidates();
  }, [filter]);

  const handlePromote = async (id: string) => {
    setBusyId(id);
    setError(null);
    try {
      const res = await promoteMemoryCandidate(id);
      setToast(`✓ Promoted to long-term memory: "${res.content.slice(0, 60)}…"`);
      setCandidates((items) => items.map((c) => (c.id === id ? { ...c, status: "applied" } : c)));
      setTimeout(() => setToast(null), 4000);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Promotion failed");
    } finally {
      setBusyId(null);
    }
  };

  const handleDismiss = async (id: string) => {
    setBusyId(id);
    setError(null);
    try {
      await dismissMemoryCandidate(id);
      setToast("✓ Candidate dismissed.");
      setCandidates((items) => items.map((c) => (c.id === id ? { ...c, status: "rejected" } : c)));
      setTimeout(() => setToast(null), 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Dismiss failed");
    } finally {
      setBusyId(null);
    }
  };

  const handleScan = async () => {
    setScanning(true);
    try {
      const res = await scanMemoryCandidates();
      setToast(`✓ Scanned recent history: discovered ${res.candidates_discovered} candidates.`);
      loadCandidates();
      setTimeout(() => setToast(null), 4000);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Scan failed");
    } finally {
      setScanning(false);
    }
  };

  return (
    <div className="candidates-page">
      {toast && <div className="toast-banner" role="status">{toast}</div>}
      {error && <div className="inline-error" role="alert">{error}</div>}

      <div className="panel-header" style={{ marginBottom: "1rem" }}>
        <div>
          <h2>Memory Candidates</h2>
          <p className="muted">Extracted facts, directives, and user preferences waiting for curation into long-term memory.</p>
        </div>
        <button className="secondary-button" onClick={handleScan} disabled={scanning}>
          {scanning ? "Scanning…" : "↻ Scan Recent History"}
        </button>
      </div>

      <div className="filter-bar">
        {[
          ["pending_approval", "Pending Review"],
          ["applied", "Promoted"],
          ["rejected", "Dismissed"],
          ["", "All"],
        ].map(([val, label]) => (
          <button
            key={val || "all"}
            className={`filter ${filter === val ? "active" : ""}`}
            onClick={() => setFilter(val)}
          >
            {label}
          </button>
        ))}
        <span className="filter-count">{candidates.length} candidates</span>
      </div>

      {loading && <div className="workspace-loading">Loading memory candidates...</div>}

      {!loading && candidates.length === 0 && (
        <div className="empty-state">
          <span className="empty-icon">★</span>
          <strong>No candidates found</strong>
          <span>{filter === "pending_approval" ? "All detected facts have been reviewed or none are pending." : "No candidates match this filter."}</span>
        </div>
      )}

      <div className="candidates-grid">
        {candidates.map((cand) => (
          <div key={cand.id} className="panel candidate-card">
            <div className="candidate-header">
              <span className={`category-badge ${cand.category.replace(/\s+/g, "-")}`}>{cand.category}</span>
              <span className="target-namespace">{cand.namespace === "agent" ? "MEMORY.md" : "USER.md"}</span>
              <span className="confidence-pill">{(cand.confidence * 100).toFixed(0)}% confidence</span>
            </div>
            <p className="candidate-content">"{cand.content}"</p>
            <div className="candidate-meta">
              <span>{cand.source_run_id ? `Source: Run #${cand.source_run_id.slice(0, 8)}` : "Source: heuristic scan"}</span>
              <span className="muted-text">{formatTime(cand.created_at)}</span>
            </div>
            <div className="candidate-actions">
              {cand.status === "pending_approval" ? (
                <>
                  <button
                    className="danger-button"
                    onClick={() => handleDismiss(cand.id)}
                    disabled={busyId === cand.id}
                  >
                    Dismiss
                  </button>
                  <button
                    className="primary-button"
                    onClick={() => handlePromote(cand.id)}
                    disabled={busyId === cand.id}
                  >
                    {busyId === cand.id ? "Promoting…" : "Promote to Long-term Memory"}
                  </button>
                </>
              ) : cand.status === "applied" ? (
                <span className="status-badge green">✓ Curated into Long-term Memory</span>
              ) : (
                <span className="status-badge muted">Dismissed</span>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── Proposals Page (Governed Skills) ───────────────────────────────────────

function ProposalsPage() {
  const [proposals, setProposals] = useState<SkillProposal[]>([]);
  const [selected, setSelected] = useState<SkillProposal | null>(null);
  const [filter, setFilter] = useState<string>("pending");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getLearningProposals(filter || undefined)
      .then((items) => {
        if (!cancelled) {
          setProposals(items);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed");
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [filter]);

  async function handleApprove(id: string) {
    setActionBusy(true);
    setError(null);
    try {
      const updated = await approveProposal(id);
      setSelected(updated);
      setProposals((items) => items.map((p) => (p.id === id ? updated : p)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Approve failed");
    } finally {
      setActionBusy(false);
    }
  }

  async function handleReject(id: string) {
    setActionBusy(true);
    setError(null);
    try {
      const updated = await rejectProposal(id);
      setSelected(updated);
      setProposals((items) => items.map((p) => (p.id === id ? updated : p)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Reject failed");
    } finally {
      setActionBusy(false);
    }
  }

  if (selected) {
    const isPatch = selected.operation === "patch";
    return (
      <div className="proposal-detail">
        <button className="text-link" onClick={() => setSelected(null)}>← Back to proposals</button>
        <div className="proposal-header">
          <div>
            <div className="eyebrow">{isPatch ? "UPDATE EXISTING SKILL" : "PROPOSED NEW SKILL"}</div>
            <h2>{selected.skill_name}</h2>
          </div>
          <ProposalBadge status={selected.status} />
        </div>
        <div className="proposal-recommendation-box">
          <strong>Recommendation: {isPatch ? `Update existing skill "${selected.skill_name}"` : `Create new skill "${selected.skill_name}"`}</strong>
          <p className="muted">{selected.description}</p>
        </div>
        {selected.base_revision !== null && <div className="detail-row"><span>Base revision</span><strong>{selected.base_revision}</strong></div>}
        <div className="detail-row"><span>Confidence</span><strong>{(selected.confidence * 100).toFixed(0)}%</strong></div>
        {selected.review_model && <div className="detail-row"><span>Reviewer</span><strong>{selected.review_model}</strong></div>}
        {selected.source_run_id && <div className="detail-row"><span>Evidence (Source Run)</span><strong>Run #{selected.source_run_id.slice(0, 8)}</strong></div>}
        <section className="panel"><div className="panel-header"><h2>Rationale & Evidence</h2></div><p className="proposal-reason">{selected.reason}</p></section>
        {selected.before !== null && (
          <section className="panel">
            <div className="panel-header"><h2>Proposed Diff</h2></div>
            <div className="diff-view">
              <pre className="diff-before">{selected.before}</pre>
              <pre className="diff-after">{selected.content}</pre>
            </div>
          </section>
        )}
        {selected.before === null && (
          <section className="panel">
            <div className="panel-header"><h2>Proposed SKILL.md content</h2></div>
            <pre className="proposal-content">{selected.content}</pre>
          </section>
        )}
        {error && <div className="inline-error" role="alert">{error}</div>}
        {selected.status === "pending" && (
          <div className="proposal-actions">
            <button className="danger-button" onClick={() => void handleReject(selected.id)} disabled={actionBusy}>Dismiss</button>
            <button className="primary-button" onClick={() => void handleApprove(selected.id)} disabled={actionBusy}>Approve & Publish</button>
          </div>
        )}
      </div>
    );
  }

  return (
    <div>
      <div className="filter-bar">
        {["pending", "approved", "rejected", ""].map((f) => (
          <button key={f || "all"} className={`filter ${filter === f ? "active" : ""}`} onClick={() => setFilter(f)}>
            {f === "pending" ? "Pending Review" : f === "approved" ? "Approved" : f === "rejected" ? "Dismissed" : "All"}
          </button>
        ))}
        <span className="filter-count">{proposals.length} proposals</span>
      </div>
      {error && <div className="inline-error" role="alert">{error}</div>}
      {loading && <div className="workspace-loading">Loading proposals...</div>}
      {!loading && proposals.length === 0 && (
        <div className="empty-state">
          <span className="empty-icon">◆</span>
          <strong>No skill proposals</strong>
          <span>{filter === "pending" ? "No skill proposals need review at this time." : "No proposals match this filter."}</span>
        </div>
      )}
      <section className="panel proposals-table">
        {proposals.map((proposal) => (
          <button key={proposal.id} className="table-row" onClick={() => setSelected(proposal)}>
            <span><ProposalBadge status={proposal.status} /></span>
            <span className="task-cell">
              <strong>{proposal.operation === "patch" ? "Update: " : "New: "}{proposal.skill_name}</strong>
              <small>{proposal.description.slice(0, 80)}</small>
            </span>
            <span className="mono">{(proposal.confidence * 100).toFixed(0)}%</span>
            <span className="muted-text">{formatTime(proposal.created_at)}</span>
          </button>
        ))}
      </section>
    </div>
  );
}

function ProposalBadge({ status }: { status: string }) {
  const colors: Record<string, string> = { pending: "amber", approved: "green", rejected: "error", conflict: "error", expired: "muted" };
  return <span className={`status-badge ${colors[status] ?? ""}`}>{status}</span>;
}

// ─── Skills ──────────────────────────────────────────────────────────────────

function SkillsPage() {
  const [skills, setSkills] = useState<LearnedSkill[]>([]);
  const [selected, setSelected] = useState<LearnedSkill | null>(null);
  const [revisions, setRevisions] = useState<SkillRevision[]>([]);
  const [runs, setRuns] = useState<SkillRunEvent[]>([]);
  const [tab, setTab] = useState<"content" | "revisions" | "runs">("content");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  useEffect(() => {
    let cancelled = false;
    getLearningSkills()
      .then((items) => { if (!cancelled) { setSkills(items); setLoading(false); } })
      .catch((err) => { if (!cancelled) { setError(err instanceof Error ? err.message : "Failed"); setLoading(false); } });
    return () => { cancelled = true; };
  }, []);

  async function openSkill(skill: LearnedSkill) {
    try {
      const [detail, revs, skillRuns] = await Promise.all([
        getLearningSkill(skill.skill_id),
        getLearningSkillRevisions(skill.skill_id),
        getLearningSkillRuns(skill.skill_id),
      ]);
      setSelected(detail);
      setRevisions(revs);
      setRuns(skillRuns);
      setTab("content");
    } catch (err) { setError(err instanceof Error ? err.message : "Failed to load skill"); }
  }

  async function togglePin(skill: LearnedSkill) {
    try {
      const updated = skill.pinned ? await unpinSkill(skill.skill_id) : await pinSkill(skill.skill_id);
      setSkills((items) => items.map((s) => (s.skill_id === updated.skill_id ? { ...s, ...updated } : s)));
      if (selected?.skill_id === updated.skill_id) setSelected({ ...selected, ...updated });
    } catch (err) { setError(err instanceof Error ? err.message : "Pin toggle failed"); }
  }

  async function handleArchiveRestore(skill: LearnedSkill) {
    try {
      const updated = skill.state === "archived" ? await restoreSkill(skill.skill_id) : await archiveSkill(skill.skill_id);
      setSkills((items) => items.map((s) => (s.skill_id === updated.skill_id ? { ...s, ...updated } : s)));
      if (selected?.skill_id === updated.skill_id) setSelected({ ...selected, ...updated });
    } catch (err) { setError(err instanceof Error ? err.message : "Archive/restore failed"); }
  }

  async function handleRollback(skill: LearnedSkill, targetRevision: number) {
    try {
      const updated = await rollbackSkill(skill.skill_id, targetRevision, "UI rollback");
      setSkills((items) => items.map((s) => (s.skill_id === updated.skill_id ? { ...s, ...updated } : s)));
      if (selected?.skill_id === updated.skill_id) setSelected({ ...selected, ...updated });
      const revs = await getLearningSkillRevisions(skill.skill_id);
      setRevisions(revs);
    } catch (err) { setError(err instanceof Error ? err.message : "Rollback failed"); }
  }

  if (selected) {
    return (
      <div className="skill-detail">
        <button className="text-link" onClick={() => setSelected(null)}>← Back to skills</button>
        <div className="skill-header">
          <h2>{selected.name}</h2>
          <div className="skill-meta">
            <span className={`status-badge ${selected.state}`}>{selected.state}</span>
            <span>{selected.owner}-owned</span>
            <span>Rev {selected.revision}</span>
            {selected.pinned && <span className="pin-badge">📌 Pinned</span>}
          </div>
        </div>
        <p className="muted">{selected.description}</p>
        {selected.stats && (
          <div className="stat-grid">
            <StatCard label="Success" value={selected.stats.success_rate !== null ? `${selected.stats.success_rate}%` : "—"} detail={`${selected.stats.executed} executions`} accent="blue" />
            <StatCard label="Loaded" value={String(selected.stats.loaded)} detail="times" accent="violet" />
            <StatCard label="Corrected" value={String(selected.stats.corrected)} detail="deviations" accent="amber" />
          </div>
        )}
        <div className="skill-actions">
          <button className="secondary-button" onClick={() => void togglePin(selected)}>{selected.pinned ? "Unpin" : "Pin"}</button>
          <button className="secondary-button" onClick={() => void handleArchiveRestore(selected)}>{selected.state === "archived" ? "Restore" : "Archive"}</button>
        </div>
        <nav className="learning-tabs" aria-label="Skill detail tabs">
          <button className={`learning-tab ${tab === "content" ? "active" : ""}`} onClick={() => setTab("content")}>Content</button>
          <button className={`learning-tab ${tab === "revisions" ? "active" : ""}`} onClick={() => setTab("revisions")}>History ({revisions.length})</button>
          <button className={`learning-tab ${tab === "runs" ? "active" : ""}`} onClick={() => setTab("runs")}>Runs ({runs.length})</button>
        </nav>
        {tab === "content" && <section className="panel"><pre className="proposal-content">{selected.content || "No content loaded."}</pre></section>}
        {tab === "revisions" && (
          <section className="panel">
            {revisions.map((rev) => (
              <div key={rev.id} className="revision-row">
                <div>
                  <strong>Revision {rev.revision}</strong>
                  <span className="muted"> · {rev.operation} · {formatTime(rev.created_at)}</span>
                  <p className="muted">{rev.reason}</p>
                </div>
                {rev.revision < selected.revision && (
                  <button className="secondary-button" onClick={() => void handleRollback(selected, rev.revision)}>Rollback</button>
                )}
              </div>
            ))}
            {revisions.length === 0 && <div className="empty-state"><span className="empty-icon">◌</span><strong>No revision history</strong></div>}
          </section>
        )}
        {tab === "runs" && (
          <section className="panel">
            {runs.map((run) => (
              <div key={`${run.run_id}-${run.event}`} className="activity-row">
                <span className="activity-icon">{run.event === "successful" ? "✓" : run.event === "failed" ? "!" : "•"}</span>
                <div><strong>{run.event}</strong><span>{run.run_id.slice(0, 8)} · {formatTime(run.created_at)}</span></div>
              </div>
            ))}
            {runs.length === 0 && <div className="empty-state"><span className="empty-icon">◌</span><strong>No run data yet</strong></div>}
          </section>
        )}
        {error && <div className="inline-error" role="alert">{error}</div>}
      </div>
    );
  }

  const filtered = skills.filter((s) => !search || s.name.includes(search) || s.description.includes(search));
  return (
    <div>
      <div className="filter-bar">
        <input className="text-input" type="search" placeholder="Search skills..." value={search} onChange={(e) => setSearch(e.target.value)} style={{ maxWidth: "240px" }} />
        <span className="filter-count">{filtered.length} skills</span>
      </div>
      {error && <div className="inline-error" role="alert">{error}</div>}
      {loading && <div className="workspace-loading">Loading skills...</div>}
      {!loading && filtered.length === 0 && <div className="empty-state"><span className="empty-icon">⊙</span><strong>No skills found</strong><span>Learned skills will appear here.</span></div>}
      <section className="panel skills-table" aria-label="Learned skills">
        <div className="table-head"><span>STATE</span><span>NAME</span><span>OWNER</span><span>REV</span><span>SUCCESS</span><span>USED</span></div>
        {filtered.map((skill) => (
          <button key={skill.skill_id} className="table-row" onClick={() => void openSkill(skill)}>
            <span><span className={`status-badge ${skill.state}`}>{skill.state}</span></span>
            <span className="task-cell"><strong>{skill.name}</strong><small>{skill.description.slice(0, 60)}</small></span>
            <span>{skill.owner}</span>
            <span className="mono">{skill.revision}</span>
            <span>{skill.stats?.success_rate !== null && skill.stats?.success_rate !== undefined ? `${skill.stats.success_rate}%` : "—"}</span>
            <span className="muted-text">{formatTime(skill.updated_at)}</span>
          </button>
        ))}
      </section>
    </div>
  );
}

// ─── Memory ──────────────────────────────────────────────────────────────────

function MemoryPage() {
  const [memories, setMemories] = useState<Array<Record<string, unknown>>>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editId, setEditId] = useState<string | null>(null);
  const [editContent, setEditContent] = useState("");

  useEffect(() => {
    let cancelled = false;
    getLearningMemory()
      .then((items) => { if (!cancelled) { setMemories(items); setLoading(false); } })
      .catch((err) => { if (!cancelled) { setError(err instanceof Error ? err.message : "Failed"); setLoading(false); } });
    return () => { cancelled = true; };
  }, []);

  async function handleSave(id: string) {
    try {
      await updateLearningMemory(id, editContent);
      setMemories((items) => items.map((m) => (m.id === id ? { ...m, content: editContent } : m)));
      setEditId(null);
    } catch (err) { setError(err instanceof Error ? err.message : "Update failed"); }
  }

  async function handleDelete(id: string) {
    try {
      await deleteLearningMemory(id);
      setMemories((items) => items.filter((m) => m.id !== id));
    } catch (err) { setError(err instanceof Error ? err.message : "Delete failed"); }
  }

  if (loading) return <div className="workspace-loading">Loading memories...</div>;

  return (
    <div>
      {error && <div className="inline-error" role="alert">{error}</div>}
      <span className="filter-count">{memories.length} memories</span>
      {memories.length === 0 && <div className="empty-state"><span className="empty-icon">✦</span><strong>No memories</strong><span>Memories from learning sessions will appear here.</span></div>}
      <div className="memory-grid">
        {memories.map((mem) => {
          const id = String(mem.id);
          const content = String(mem.content ?? "");
          const source = String(mem.source ?? "unknown");
          const kind = String(mem.kind ?? "episodic");
          const isEditing = editId === id;
          return (
            <div key={id} className="panel memory-card">
              <div className="memory-card-header">
                <span className={`status-badge ${kind}`}>{kind}</span>
                <span className="muted">{source}</span>
              </div>
              {isEditing ? (
                <textarea className="text-input memory-edit" value={editContent} onChange={(e) => setEditContent(e.target.value)} rows={4} />
              ) : (
                <p className="memory-text">{content.slice(0, 200)}{content.length > 200 ? "…" : ""}</p>
              )}
              <div className="memory-actions">
                {isEditing ? (
                  <>
                    <button className="secondary-button" onClick={() => setEditId(null)}>Cancel</button>
                    <button className="primary-button" onClick={() => void handleSave(id)}>Save</button>
                  </>
                ) : (
                  <>
                    <button className="secondary-button" onClick={() => { setEditId(id); setEditContent(content); }}>Edit</button>
                    <button className="danger-button" onClick={() => void handleDelete(id)}>Forget</button>
                  </>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── Learn ───────────────────────────────────────────────────────────────────

function LearnPage() {
  const [source, setSource] = useState("");
  const [skillName, setSkillName] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<LearnResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleLearn() {
    if (!source.trim()) return;
    setBusy(true); setError(null); setResult(null);
    try {
      const response = await submitLearn(source, skillName ? { skill_name: skillName } : undefined);
      setResult(response);
      if (response.status === "success" || response.status === "pending_approval") {
        setSource(""); setSkillName("");
      }
    } catch (err) { setError(err instanceof Error ? err.message : "Learn failed"); }
    finally { setBusy(false); }
  }

  return (
    <div className="learn-page">
      <section className="panel">
        <div className="panel-header"><h2>Learn something</h2></div>
        <div className="learn-form">
          <label className="field-label" htmlFor="learn-source">Source</label>
          <textarea id="learn-source" className="text-input" value={source} onChange={(e) => setSource(e.target.value)} placeholder="https://docs.example.com or paste text..." rows={4} disabled={busy} />
          <label className="field-label" htmlFor="learn-name">Optional skill name</label>
          <input id="learn-name" className="text-input" value={skillName} onChange={(e) => setSkillName(e.target.value)} placeholder="e.g. deployment-recovery" disabled={busy} />
          <button className="primary-button" onClick={() => void handleLearn()} disabled={busy || !source.trim()}>{busy ? "Processing..." : "Learn"}</button>
        </div>
        {error && <div className="inline-error" role="alert">{error}</div>}
        {result && (
          <div className={`learn-result ${result.status}`}>
            <strong>{result.status}</strong>
            <p>{result.message}</p>
            {result.proposal_id && <span className="muted">Proposal: {result.proposal_id.slice(0, 8)}</span>}
            {result.warnings && result.warnings.length > 0 && <ul className="learn-warnings">{result.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>}
          </div>
        )}
      </section>
    </div>
  );
}

// ─── Settings ────────────────────────────────────────────────────────────────

function SettingsPage() {
  const [config, setConfig] = useState<LearningConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getLearningSettings()
      .then((c) => { if (!cancelled) { setConfig(c); setLoading(false); } })
      .catch((err) => { if (!cancelled) { setError(err instanceof Error ? err.message : "Failed"); setLoading(false); } });
    return () => { cancelled = true; };
  }, []);

  async function handleToggle(key: string, value: unknown) {
    setSaving(true); setError(null);
    try { await updateLearningSettings({ [key]: value }); }
    catch (err) { setError(err instanceof Error ? err.message : "Save failed"); }
    finally { setSaving(false); }
  }

  if (loading) return <div className="workspace-loading">Loading settings...</div>;
  if (!config) return <div className="inline-error">Unable to load settings</div>;

  return (
    <div className="settings-page">
      {error && <div className="inline-error" role="alert">{error}</div>}
      <section className="panel">
        <div className="panel-header"><h2>Learning Mode</h2></div>
        <div className="settings-row"><span>Enabled</span><ToggleSwitch checked={config.enabled} onChange={(v) => void handleToggle("enabled", v)} disabled={saving} /></div>
      </section>
      <section className="panel">
        <div className="panel-header"><h2>Trust</h2></div>
        <div className="settings-row">
          <span>Mode</span>
          <div className="radio-group">
            {["strict", "balanced", "autonomous"].map((mode) => (
              <label key={mode} className={`radio-label ${config.skills.trust_mode === mode ? "selected" : ""}`}>
                <input type="radio" name="trust_mode" value={mode} checked={config.skills.trust_mode === mode} onChange={() => void handleToggle("trust_mode", mode)} disabled={saving} />
                {mode.charAt(0).toUpperCase() + mode.slice(1)}
              </label>
            ))}
          </div>
        </div>
        <div className="settings-row"><span>Minimum confidence</span><strong>{config.skills.min_confidence}</strong></div>
      </section>
      <section className="panel">
        <div className="panel-header"><h2>Reviewer</h2></div>
        <div className="settings-row"><span>Model</span><strong>{config.reviewer.model}</strong></div>
        <div className="settings-row"><span>Provider</span><strong>{config.reviewer.provider}</strong></div>
      </section>
      <section className="panel">
        <div className="panel-header"><h2>Curator</h2></div>
        <div className="settings-row"><span>Enabled</span><ToggleSwitch checked={config.curator.enabled} onChange={(v) => void handleToggle("curator_enabled", v)} disabled={saving} /></div>
        <div className="settings-row"><span>Schedule</span><strong>{config.curator.schedule} ({config.curator.timezone})</strong></div>
        <div className="settings-row"><span>Stale after</span><strong>{config.curator.stale_after_days} days</strong></div>
        <div className="settings-row"><span>Archive after</span><strong>{config.curator.archive_after_days} days</strong></div>
      </section>
      <section className="panel">
        <div className="panel-header"><h2>Notifications</h2></div>
        <div className="settings-row"><span>Mode</span><strong>{config.notifications.mode}</strong></div>
      </section>
      <p className="muted" style={{ marginTop: "1rem" }}>Runtime changes are non-persistent. For persistent changes, edit gravityclaw.toml and restart.</p>
    </div>
  );
}

// ─── Shared ──────────────────────────────────────────────────────────────────

function StatCard({ label, value, detail, accent }: { label: string; value: string; detail: string; accent: string }) {
  return <div className={`stat-card ${accent}`}><div className="stat-label">{label}<span className="stat-glyph">✦</span></div><strong>{value}</strong><span>{detail}</span></div>;
}

function ToggleSwitch({ checked, onChange, disabled }: { checked: boolean; onChange: (value: boolean) => void; disabled?: boolean }) {
  return (
    <button
      role="switch"
      aria-checked={checked}
      className={`toggle-switch ${checked ? "on" : "off"}`}
      onClick={() => onChange(!checked)}
      disabled={disabled}
    >
      <span className="toggle-thumb" />
    </button>
  );
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatAction(action: string): string {
  return action.replaceAll(".", " · ").replaceAll("_", " ");
}

function eventIcon(action: string): string {
  if (action.includes("approved")) return "✓";
  if (action.includes("rejected")) return "✗";
  if (action.includes("created") || action.includes("learn")) return "↓";
  if (action.includes("updated") || action.includes("patch")) return "↻";
  if (action.includes("archive") || action.includes("delete")) return "—";
  return "•";
}
