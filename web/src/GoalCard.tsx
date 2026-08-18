import { useEffect, useState } from "react";
import { cancelGoal, completeGoal, getGoalEvaluations, getGoals, pauseGoal, resumeGoal } from "./api";
import type { GoalEvaluation, GoalRecord, GoalStatus } from "./types";

function statusLabel(status: GoalStatus): string {
  switch (status) {
    case "active": return "ACTIVE";
    case "paused": return "PAUSED";
    case "completed": return "COMPLETED";
    case "cancelled": return "CANCELLED";
    case "failed": return "FAILED";
  }
}

function statusColor(status: GoalStatus): string {
  switch (status) {
    case "active": return "blue";
    case "paused": return "amber";
    case "completed": return "green";
    case "cancelled": return "muted";
    case "failed": return "red";
  }
}

function ProgressRing({ turns, maxTurns }: { turns: number; maxTurns: number }) {
  const percent = Math.min(100, (turns / maxTurns) * 100);
  const radius = 20;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (percent / 100) * circumference;
  return (
    <svg width="52" height="52" viewBox="0 0 52 52" className="goal-ring">
      <circle cx="26" cy="26" r={radius} fill="none" stroke="var(--border-subtle)" strokeWidth="3" />
      <circle
        cx="26" cy="26" r={radius} fill="none"
        stroke="var(--accent-blue)" strokeWidth="3"
        strokeDasharray={circumference} strokeDashoffset={offset}
        strokeLinecap="round" transform="rotate(-90 26 26)"
      />
      <text x="26" y="26" textAnchor="middle" dominantBaseline="central" className="goal-ring-text">
        {turns}/{maxTurns}
      </text>
    </svg>
  );
}

export function GoalCard({ conversationId }: { conversationId?: string }) {
  const [goal, setGoal] = useState<GoalRecord | null>(null);
  const [evaluations, setEvaluations] = useState<GoalEvaluation[]>([]);
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const goals = await getGoals(conversationId, "active");
        const active = goals[0] ?? null;
        if (!cancelled) {
          setGoal(active);
          if (active) {
            const evals = await getGoalEvaluations(active.id);
            if (!cancelled) setEvaluations(evals);
          }
        }
      } catch { /* ignore */ }
      finally { if (!cancelled) setLoading(false); }
    })();
    return () => { cancelled = true; };
  }, [conversationId]);

  // Also check for paused goals if no active one
  useEffect(() => {
    if (goal || loading) return;
    let cancelled = false;
    void (async () => {
      try {
        const goals = await getGoals(conversationId, "paused");
        if (!cancelled && goals[0]) {
          setGoal(goals[0]);
          const evals = await getGoalEvaluations(goals[0].id);
          if (!cancelled) setEvaluations(evals);
        }
      } catch { /* ignore */ }
    })();
    return () => { cancelled = true; };
  }, [goal, loading, conversationId]);

  if (loading || !goal) return null;

  async function handlePause() {
    if (!goal) return;
    try {
      const updated = await pauseGoal(goal.id);
      setGoal(updated);
    } catch { /* ignore */ }
  }

  async function handleResume() {
    if (!goal) return;
    try {
      const updated = await resumeGoal(goal.id);
      setGoal(updated);
    } catch { /* ignore */ }
  }

  async function handleCancel() {
    if (!goal) return;
    try {
      const updated = await cancelGoal(goal.id);
      setGoal(updated);
    } catch { /* ignore */ }
  }

  async function handleComplete() {
    if (!goal) return;
    try {
      const updated = await completeGoal(goal.id);
      setGoal(updated);
    } catch { /* ignore */ }
  }

  const isTerminal = goal.status === "completed" || goal.status === "cancelled" || goal.status === "failed";

  return (
    <section className="panel goal-panel">
      <div className="goal-header">
        <div className="goal-title-row">
          <span className="goal-label">Goal</span>
          <span className={`goal-status-badge ${statusColor(goal.status)}`}>
            {statusLabel(goal.status)}
          </span>
        </div>
        <button className="goal-expand" onClick={() => setExpanded(!expanded)} aria-label="Toggle details">
          {expanded ? "▴" : "▾"}
        </button>
      </div>

      <div className="goal-body">
        <div className="goal-objective">{goal.objective}</div>

        <div className="goal-progress-row">
          <ProgressRing turns={goal.turns_used} maxTurns={goal.max_turns} />
          <div className="goal-progress-info">
            <span className="goal-progress-label">
              turn {goal.turns_used} / {goal.max_turns}
            </span>
            {goal.current_step && (
              <span className="goal-current-step">{goal.current_step}</span>
            )}
          </div>
        </div>

        {goal.acceptance.length > 0 && (
          <div className="goal-acceptance">
            <span className="goal-section-label">Acceptance</span>
            {goal.acceptance.map((criterion, idx) => (
              <div key={idx} className="goal-criterion">
                <span className={`goal-criterion-icon ${criterion.passed ? "passed" : ""}`}>
                  {criterion.passed ? "✓" : "○"}
                </span>
                <span>{criterion.description || criterion.command || criterion.path || "unnamed"}</span>
              </div>
            ))}
          </div>
        )}

        {expanded && evaluations.length > 0 && (
          <div className="goal-evaluations">
            <span className="goal-section-label">Evaluations</span>
            {evaluations.slice(0, 5).map((ev) => (
              <div key={ev.id} className="goal-eval-row">
                <span className="goal-eval-turn">T{ev.turn_number}</span>
                <span className={`goal-eval-verdict ${ev.verdict}`}>{ev.verdict}</span>
                {ev.reason && <span className="goal-eval-reason">{ev.reason}</span>}
              </div>
            ))}
          </div>
        )}
      </div>

      {!isTerminal && (
        <div className="goal-actions">
          {goal.status === "active" && (
            <button className="goal-action-btn" onClick={handlePause}>Pause</button>
          )}
          {goal.status === "paused" && (
            <button className="goal-action-btn primary" onClick={handleResume}>Resume</button>
          )}
          <button className="goal-action-btn" onClick={handleComplete}>Complete</button>
          <button className="goal-action-btn danger" onClick={handleCancel}>Cancel</button>
        </div>
      )}
    </section>
  );
}
