import { useEffect, useState } from "react";
import { getContextStatus } from "./api";
import type { ContextStatus } from "./types";

function tokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1000) {
    const k = value / 1000;
    return k === Math.floor(k) ? `${k}k` : `${k.toFixed(1)}k`;
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
  const currentRunUsed = isCurrentlyRunning ? status.used_tokens : 0;
  const currentRunBudget = status.budget_tokens || 128000;
  
  // Previous run is recent[1] if currently executing, otherwise recent[0]
  const prevRun = isCurrentlyRunning ? status.recent[1] : status.recent[0];
  
  const conversationTotal = status.conversation_total_tokens ?? (status.breakdown.conversation ?? 0);
  const systemHarness = (status.breakdown.identity ?? 0) + (status.breakdown.operational ?? 0);
  const memory = status.breakdown.memory ?? 0;
  const toolResults = status.breakdown.artifacts ?? 0;
  const messages = status.breakdown.conversation ?? conversationTotal;
  const compactions = status.compactions_count ?? (status.messages_compacted > 0 ? 1 : 0);

  return (
    <div className="context-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="context-modal" role="dialog" aria-modal="true" aria-labelledby="context-dialog-title">
        {/* Header */}
        <header className="context-modal-head">
          <div>
            <div className="eyebrow">CONTEXT BUDGET</div>
            <h2 id="context-dialog-title">{getCircleGlyph(value)} {value.toFixed(0)}%</h2>
            <span className="mono">{tokens(status.used_tokens)} / {tokens(status.budget_tokens)} tokens</span>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="Close context details">&times;</button>
        </header>

        {/* Breakdown List */}
        <div className="context-modal-section">
          <div className="section-label">CONTEXT</div>
          <div className="context-breakdown-row">
            <span>Current run</span>
            <strong>{tokens(currentRunUsed)} / {tokens(currentRunBudget)}</strong>
          </div>
          <div className="context-breakdown-row">
            <span>Previous run</span>
            <strong>{prevRun ? `${tokens(prevRun.used_tokens)} / ${tokens(prevRun.budget_tokens)}` : "—"}</strong>
          </div>
          <div className="context-breakdown-row">
            <span>Conversation total</span>
            <strong>{tokens(conversationTotal)} tokens</strong>
          </div>
          <div className="context-breakdown-row">
            <span>System / harness</span>
            <strong>{tokens(systemHarness)}</strong>
          </div>
          <div className="context-breakdown-row">
            <span>Memory</span>
            <strong>{tokens(memory)}</strong>
          </div>
          <div className="context-breakdown-row">
            <span>Tool results</span>
            <strong>{tokens(toolResults)}</strong>
          </div>
          <div className="context-breakdown-row">
            <span>Messages</span>
            <strong>{tokens(messages)}</strong>
          </div>
          <div className="context-breakdown-row">
            <span>Compactions</span>
            <strong>{compactions}</strong>
          </div>
        </div>

        {/* Footer */}
        <footer className="context-modal-foot">
          Context is immutable per run. Model and state changes apply on next execution.
        </footer>
      </section>
    </div>
  );
}
