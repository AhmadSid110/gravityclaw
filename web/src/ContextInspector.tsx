import { useEffect, useState } from "react";
import { getRunContextSnapshot } from "./api";
import type { ContextMemoryEntry, ContextSegment, ContextSkillEntry, ContextSnapshot, ContextTransformation, RunStatus } from "./types";

// ─── Context Circle Indicator ────────────────────────────────────────────────

export interface ContextCircleProps {
  /** 0–1 usage ratio */
  ratio: number;
  /** Whether the run is still in progress */
  running?: boolean;
  /** Click handler to open the inspector dialog */
  onClick?: () => void;
}

/**
 * Compact circle indicator showing context usage as a filling arc.
 * Renders an SVG donut with percentage label.
 */
export function ContextCircle({ ratio, running, onClick }: ContextCircleProps) {
  const percent = Math.round(ratio * 100);
  const radius = 16;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference * (1 - Math.min(ratio, 1));
  const color = ratio > 0.85 ? "var(--danger)" : ratio > 0.65 ? "var(--warning)" : "var(--accent)";

  return (
    <button
      className="context-circle-button"
      onClick={onClick}
      aria-label={`Context usage ${percent}%. Click to inspect.`}
      title={`Context: ${percent}%`}
    >
      <svg className="context-circle-svg" viewBox="0 0 40 40" width="32" height="32">
        <circle
          className="context-circle-track"
          cx="20" cy="20" r={radius}
          fill="none"
          stroke="var(--surface-subtle, #333)"
          strokeWidth="3"
        />
        <circle
          className="context-circle-fill"
          cx="20" cy="20" r={radius}
          fill="none"
          stroke={color}
          strokeWidth="3"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          strokeLinecap="round"
          transform="rotate(-90 20 20)"
        />
      </svg>
      <span className="context-circle-label">
        {running ? `~${percent}%` : `${percent}%`}
      </span>
    </button>
  );
}

// ─── Context Inspector Dialog ────────────────────────────────────────────────

export interface ContextInspectorProps {
  /** Run ID to inspect */
  runId: string;
  /** Current run status for live indicator */
  runStatus?: RunStatus;
  /** Close the dialog */
  onClose: () => void;
  /** Navigate to a skill in Learning Studio */
  onOpenSkill?: (skillId: string) => void;
  /** Navigate to a memory in Learning Studio */
  onOpenMemory?: (memoryId: string) => void;
  /** Navigate to the Journey graph for a skill */
  onOpenJourney?: (skillId: string) => void;
}

type InspectorSection = "overview" | "detailed";

export function ContextInspector({
  runId,
  runStatus,
  onClose,
  onOpenSkill,
  onOpenMemory,
  onOpenJourney,
}: ContextInspectorProps) {
  const [snapshot, setSnapshot] = useState<ContextSnapshot | null>(null);
  const [section, setSection] = useState<InspectorSection>("overview");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getRunContextSnapshot(runId)
      .then((data) => {
        if (!cancelled) {
          setSnapshot(data);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load context");
          setLoading(false);
        }
      });
    return () => { cancelled = true; };
  }, [runId]);

  // Re-poll if run is in progress to get updated token counts
  useEffect(() => {
    if (!runStatus || runStatus !== "running") return;
    const interval = window.setInterval(() => {
      getRunContextSnapshot(runId)
        .then((data) => { if (data) setSnapshot(data); })
        .catch(() => { /* silent retry */ });
    }, 5000);
    return () => window.clearInterval(interval);
  }, [runId, runStatus]);

  if (loading) {
    return (
      <div className="context-inspector-overlay" role="dialog" aria-label="Context Inspector">
        <div className="context-inspector-dialog">
          <InspectorHeader onClose={onClose} />
          <div className="context-inspector-loading">Loading context data...</div>
        </div>
        <button className="context-inspector-scrim" onClick={onClose} aria-label="Close" />
      </div>
    );
  }

  if (error || !snapshot) {
    return (
      <div className="context-inspector-overlay" role="dialog" aria-label="Context Inspector">
        <div className="context-inspector-dialog">
          <InspectorHeader onClose={onClose} />
          <div className="context-inspector-empty">
            {error ? <span className="inline-error">{error}</span> : "No context data available for this run."}
          </div>
        </div>
        <button className="context-inspector-scrim" onClick={onClose} aria-label="Close" />
      </div>
    );
  }

  const percent = Math.round(snapshot.usage_ratio * 100);

  return (
    <div className="context-inspector-overlay" role="dialog" aria-label="Context Inspector" aria-modal="true">
      <div className="context-inspector-dialog">
        <InspectorHeader onClose={onClose} />

        {/* Usage summary */}
        <div className="ci-usage-header">
          <div className="ci-usage-circle">
            <ContextCircle ratio={snapshot.usage_ratio} running={snapshot.is_estimated} />
          </div>
          <div className="ci-usage-text">
            <strong>Context {percent}%</strong>
            <span className="ci-token-count">
              {formatTokens(snapshot.input_tokens)} / {formatTokens(snapshot.context_limit)} tokens
            </span>
            {snapshot.is_estimated && (
              <span className="ci-estimated-badge">
                {snapshot.run_status === "running" ? "Estimated while running" : `${formatTokens(snapshot.input_tokens)} estimated tokens`}
              </span>
            )}
            {snapshot.is_final && (
              <span className="ci-provider-badge">Provider-reported</span>
            )}
          </div>
        </div>

        {/* Token bar */}
        <div className="ci-token-bar-container">
          <div className="ci-token-bar">
            <div
              className={`ci-token-bar-fill ${percent > 85 ? "danger" : percent > 65 ? "warning" : ""}`}
              style={{ width: `${Math.min(percent, 100)}%` }}
            />
          </div>
          <div className="ci-token-bar-labels">
            <span>{formatTokens(snapshot.input_tokens)} used</span>
            <span>{formatTokens(snapshot.remaining_tokens)} remaining</span>
          </div>
        </div>

        {/* Section toggle */}
        <div className="ci-section-toggle">
          <button className={section === "overview" ? "active" : ""} onClick={() => setSection("overview")}>Current run</button>
          <button className={section === "detailed" ? "active" : ""} onClick={() => setSection("detailed")}>Detailed context</button>
        </div>

        <div className="ci-body">
          {section === "overview" && (
            <OverviewSection
              snapshot={snapshot}
              onOpenSkill={onOpenSkill}
              onOpenMemory={onOpenMemory}
              onOpenJourney={onOpenJourney}
            />
          )}
          {section === "detailed" && (
            <DetailedSection snapshot={snapshot} />
          )}
        </div>
      </div>
      <button className="context-inspector-scrim" onClick={onClose} aria-label="Close inspector" />
    </div>
  );
}

// ─── Sub-components ──────────────────────────────────────────────────────────

function InspectorHeader({ onClose }: { onClose: () => void }) {
  return (
    <div className="ci-header">
      <div className="ci-header-title">
        <span className="ci-header-icon">◎</span>
        <strong>Context Inspector</strong>
      </div>
      <button className="icon-button" onClick={onClose} aria-label="Close">×</button>
    </div>
  );
}

function OverviewSection({
  snapshot,
  onOpenSkill,
  onOpenMemory,
  onOpenJourney,
}: {
  snapshot: ContextSnapshot;
  onOpenSkill?: (id: string) => void;
  onOpenMemory?: (id: string) => void;
  onOpenJourney?: (id: string) => void;
}) {
  return (
    <div className="ci-overview">
      {/* Segment breakdown */}
      <section className="ci-section">
        <div className="ci-section-label">SEGMENT BREAKDOWN</div>
        <SegmentTable segments={snapshot.segments} total={snapshot.input_tokens} />
      </section>

      {/* Loaded skills */}
      {snapshot.skills.length > 0 && (
        <section className="ci-section">
          <div className="ci-section-label">LOADED SKILLS</div>
          <SkillList skills={snapshot.skills} onOpen={onOpenSkill} onJourney={onOpenJourney} />
        </section>
      )}

      {/* Retrieved memories */}
      {snapshot.memories.length > 0 && (
        <section className="ci-section">
          <div className="ci-section-label">RETRIEVED MEMORIES</div>
          <MemoryList memories={snapshot.memories} onOpen={onOpenMemory} />
        </section>
      )}

      {/* Model info */}
      <section className="ci-section">
        <div className="ci-section-label">RUN INFO</div>
        <div className="ci-info-grid">
          <div className="ci-info-row"><span>Model</span><strong>{snapshot.model}</strong></div>
          <div className="ci-info-row"><span>Token source</span><strong>{snapshot.token_source}</strong></div>
          {snapshot.output_tokens !== null && (
            <div className="ci-info-row"><span>Output tokens</span><strong>{formatTokens(snapshot.output_tokens)}</strong></div>
          )}
          <div className="ci-info-row"><span>Run status</span><strong>{snapshot.run_status}</strong></div>
        </div>
      </section>
    </div>
  );
}

function DetailedSection({ snapshot }: { snapshot: ContextSnapshot }) {
  return (
    <div className="ci-detailed">
      {/* Current vs last invocation */}
      {snapshot.conversation_tokens !== null && snapshot.last_invocation_tokens !== null && (
        <section className="ci-section">
          <div className="ci-section-label">CONTEXT COMPARISON</div>
          <div className="ci-comparison">
            <div className="ci-comparison-item">
              <span>Current conversation context</span>
              <strong>{formatTokens(snapshot.conversation_tokens)} tokens</strong>
            </div>
            <div className="ci-comparison-item">
              <span>Last model invocation</span>
              <strong>{formatTokens(snapshot.last_invocation_tokens)} tokens</strong>
            </div>
          </div>
        </section>
      )}

      {/* Transformations */}
      {snapshot.transformations && snapshot.transformations.length > 0 && (
        <section className="ci-section">
          <div className="ci-section-label">CONTEXT TRANSFORMATIONS</div>
          <TransformationList transformations={snapshot.transformations} />
        </section>
      )}

      {/* Full segment detail */}
      <section className="ci-section">
        <div className="ci-section-label">FULL SEGMENT DETAIL</div>
        <table className="ci-full-table">
          <thead>
            <tr><th>Segment</th><th>Tokens</th><th>%</th></tr>
          </thead>
          <tbody>
            {snapshot.segments.map((seg) => (
              <tr key={seg.kind}>
                <td>{formatSegmentKind(seg.kind)}</td>
                <td>{formatTokens(seg.tokens)}</td>
                <td>{snapshot.input_tokens > 0 ? `${Math.round((seg.tokens / snapshot.input_tokens) * 100)}%` : "—"}</td>
              </tr>
            ))}
            <tr className="ci-total-row">
              <td><strong>Total</strong></td>
              <td><strong>{formatTokens(snapshot.input_tokens)}</strong></td>
              <td><strong>100%</strong></td>
            </tr>
          </tbody>
        </table>
      </section>

      {/* Context limit and model */}
      <section className="ci-section">
        <div className="ci-section-label">CONTEXT WINDOW</div>
        <div className="ci-info-grid">
          <div className="ci-info-row"><span>Window size</span><strong>{formatTokens(snapshot.context_limit)}</strong></div>
          <div className="ci-info-row"><span>Used</span><strong>{formatTokens(snapshot.input_tokens)}</strong></div>
          <div className="ci-info-row"><span>Available</span><strong>{formatTokens(snapshot.remaining_tokens)}</strong></div>
          <div className="ci-info-row"><span>Model</span><strong>{snapshot.model}</strong></div>
        </div>
      </section>
    </div>
  );
}

function SegmentTable({ segments, total }: { segments: ContextSegment[]; total: number }) {
  const sorted = [...segments].sort((a, b) => b.tokens - a.tokens);
  return (
    <div className="ci-segment-table">
      {sorted.map((seg) => {
        const pct = total > 0 ? (seg.tokens / total) * 100 : 0;
        return (
          <div key={seg.kind} className="ci-segment-row">
            <div className="ci-segment-bar-bg">
              <div
                className={`ci-segment-bar-fill segment-${seg.kind}`}
                style={{ width: `${pct}%` }}
              />
            </div>
            <span className="ci-segment-label">{formatSegmentKind(seg.kind)}</span>
            <span className="ci-segment-tokens">{formatTokens(seg.tokens)}</span>
          </div>
        );
      })}
    </div>
  );
}

function SkillList({
  skills,
  onOpen,
  onJourney,
}: {
  skills: ContextSkillEntry[];
  onOpen?: (id: string) => void;
  onJourney?: (id: string) => void;
}) {
  return (
    <div className="ci-skill-list">
      {skills.map((skill) => (
        <div key={skill.skill_id} className="ci-skill-item">
          <div className="ci-skill-info">
            <strong>{skill.name}{skill.revision !== undefined ? `@${skill.revision}` : ""}</strong>
            <span>{formatTokens(skill.tokens)} tokens</span>
          </div>
          <div className="ci-skill-actions">
            {onOpen && (
              <button className="text-link" onClick={() => onOpen(skill.skill_id)}>
                Open skill
              </button>
            )}
            {onJourney && (
              <button className="text-link" onClick={() => onJourney(skill.skill_id)}>
                View journey
              </button>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

function MemoryList({
  memories,
  onOpen,
}: {
  memories: ContextMemoryEntry[];
  onOpen?: (id: string) => void;
}) {
  const totalTokens = memories.reduce((sum, m) => sum + m.tokens, 0);
  return (
    <div className="ci-memory-list">
      <div className="ci-memory-summary">
        <span>{memories.length} entries</span>
        <span>{formatTokens(totalTokens)}</span>
      </div>
      {memories.map((mem) => (
        <div key={mem.id} className="ci-memory-item">
          <div className="ci-memory-info">
            <span className="ci-memory-label">{mem.label || mem.id.slice(0, 12)}</span>
            <span className="ci-memory-tokens">{formatTokens(mem.tokens)}</span>
          </div>
          {onOpen && (
            <button className="text-link" onClick={() => onOpen(mem.id)}>
              Open memory
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

function TransformationList({ transformations }: { transformations: ContextTransformation[] }) {
  return (
    <div className="ci-transformations">
      {transformations.map((t, i) => (
        <div key={i} className="ci-transformation-row">
          <span className="ci-transformation-label">{t.label}</span>
          <span className="ci-transformation-values">
            {formatTokens(t.tokens_before)} → {formatTokens(t.tokens_after)}
            <span className={t.tokens_after < t.tokens_before ? "ci-delta-negative" : "ci-delta-positive"}>
              {t.tokens_after < t.tokens_before ? "−" : "+"}{formatTokens(Math.abs(t.tokens_after - t.tokens_before))}
            </span>
          </span>
        </div>
      ))}
    </div>
  );
}

// ─── Utilities ───────────────────────────────────────────────────────────────

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function formatSegmentKind(kind: string): string {
  const labels: Record<string, string> = {
    system: "System",
    conversation: "Conversation",
    tool_results: "Tool results",
    memory: "Memory",
    skills: "Skills",
    other: "Other",
    identity: "Identity",
    artifacts: "Artifacts",
    operational: "Operational",
  };
  return labels[kind] ?? kind.charAt(0).toUpperCase() + kind.slice(1).replace(/_/g, " ");
}
