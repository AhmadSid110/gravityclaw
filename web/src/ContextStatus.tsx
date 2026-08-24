import { useEffect, useState } from "react";
import { getContextStatus } from "./api";
import type { ContextStatus } from "./types";

function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1000) {
    const k = value / 1000;
    return k === Math.floor(k) ? `${k}K` : `${k.toFixed(1)}K`;
  }
  return String(value);
}

function percent(value: number): number { return Math.max(0, Math.min(100, value)); }

function getCircleGlyph(percentVal: number): string {
  if (percentVal <= 5) return "○";
  if (percentVal <= 25) return "◔";
  if (percentVal <= 50) return "◑";
  if (percentVal <= 75) return "◕";
  return "●";
}

export function ContextStatus({ conversationId, refreshKey }: { conversationId: string; refreshKey?: string }) {
  const [status, setStatus] = useState<ContextStatus | null>(null);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getContextStatus(conversationId).then((value) => {
      if (!cancelled) { setStatus(value); setError(null); }
    }).catch((reason) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : "Context unavailable");
    });
    return () => { cancelled = true; };
  }, [conversationId, refreshKey]);

  if (!status) return <span className="context-status-loading" title={error ?? "Loading context status"}>○ …</span>;
  const value = percent(status.percent);
  const severity = value >= 95 ? "critical" : value >= 85 ? "high" : value >= 70 ? "moderate" : "normal";

  return <>
    <button
      className={`context-status-pill context-${severity}`}
      onClick={() => setOpen(true)}
      title={`Context: ${status.used_tokens.toLocaleString()} / ${status.budget_tokens.toLocaleString()} tokens (${value.toFixed(0)}%)`}
      aria-label={`Context: ${value.toFixed(0)}% used`}
    >
      <span className="context-circle-glyph">{getCircleGlyph(value)}</span>
      <span className="context-percent-label">{value.toFixed(0)}%</span>
    </button>
    {open && <ContextDialog status={status} onClose={() => setOpen(false)} />}
  </>;
}

function ContextDialog({ status, onClose }: { status: ContextStatus; onClose: () => void }) {
  const value = percent(status.percent);
  const isCurrentlyRunning = status.state === "current";
  const currentRunUsed = isCurrentlyRunning ? status.used_tokens : status.used_tokens;
  const currentRunBudget = status.budget_tokens || 1000000;
  
  const conversationTotal = status.conversation_total_tokens ?? (status.breakdown.conversation ?? 0);
  const systemHarness = (status.breakdown.identity ?? 0) + (status.breakdown.operational ?? 0);
  const memory = status.breakdown.memory ?? 0;
  const toolResults = status.breakdown.artifacts ?? 0;
  const reserved = Math.max(0, currentRunBudget - currentRunUsed);

  return (
    <div className="context-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="context-modal" role="dialog" aria-modal="true" aria-labelledby="context-dialog-title">
        {/* Header */}
        <header className="context-modal-head">
          <div className="context-head-main">
            <div className="eyebrow">CONTEXT UTILIZATION</div>
            <div className="context-head-hero">
              <span className="context-glyph-big">{getCircleGlyph(value)}</span>
              <h2>{value.toFixed(0)}% Used</h2>
            </div>
            <span className="context-tokens-counter">{formatTokens(status.used_tokens)} / {formatTokens(status.budget_tokens)} tokens</span>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="Close context details">&times;</button>
        </header>

        {/* Breakdown List */}
        <div className="context-modal-section">
          <div className="section-label">ACTIVE BUDGET BREAKDOWN</div>
          <div className="context-breakdown-row highlight">
            <span>Current run</span>
            <strong>{formatTokens(currentRunUsed)} / {formatTokens(currentRunBudget)}</strong>
          </div>
          <div className="context-breakdown-row">
            <span>Used capacity</span>
            <strong>{value.toFixed(1)}%</strong>
          </div>
          <div className="context-breakdown-row">
            <span>System / Harness</span>
            <strong>{formatTokens(systemHarness)}</strong>
          </div>
          <div className="context-breakdown-row">
            <span>Conversation</span>
            <strong>{formatTokens(conversationTotal)}</strong>
          </div>
          <div className="context-breakdown-row">
            <span>Memory (curated + indexed)</span>
            <strong>{formatTokens(memory)}</strong>
          </div>
          <div className="context-breakdown-row">
            <span>Tools & Artifacts</span>
            <strong>{formatTokens(toolResults)}</strong>
          </div>
          <div className="context-breakdown-row">
            <span>Reserved headroom</span>
            <strong>{formatTokens(reserved)}</strong>
          </div>
        </div>

        {/* Compaction Status */}
        <div className="context-modal-section">
          <div className="section-label">COMPACTION & THRESHOLDS</div>
          <div className="context-breakdown-row">
            <span>Last compaction</span>
            <strong>{status.messages_compacted > 0 ? "Active" : "None yet"}</strong>
          </div>
          <div className="context-breakdown-row">
            <span>Messages compacted</span>
            <strong>{status.messages_compacted} messages</strong>
          </div>
          <div className="context-breakdown-row">
            <span>Compaction threshold</span>
            <strong>80% of budget</strong>
          </div>
        </div>

        {/* Footer Link */}
        <footer className="context-modal-foot">
          <button
            className="context-details-link-btn"
            onClick={() => {
              onClose();
              window.location.hash = "#context";
            }}
          >
            View context details & manifest →
          </button>
        </footer>
      </section>
    </div>
  );
}
