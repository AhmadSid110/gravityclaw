import React, { useEffect, useState } from "react";
import { listFlowTasks, listTaskFlows } from "./api";
import type { FlowTask, TaskFlow } from "./types";

interface TaskFlowHomeCardProps {
  onOpenTaskFlow: () => void;
}

export const TaskFlowHomeCard: React.FC<TaskFlowHomeCardProps> = ({ onOpenTaskFlow }) => {
  const [activeFlow, setActiveFlow] = useState<TaskFlow | null>(null);
  const [tasks, setTasks] = useState<FlowTask[]>([]);
  const [loading, setLoading] = useState<boolean>(true);

  useEffect(() => {
    let cancelled = false;
    const fetchFlowData = async () => {
      try {
        const flows = await listTaskFlows();
        const active = flows.find((f) => f.status === "RUNNING") || flows[0] || null;
        if (!cancelled) {
          setActiveFlow(active);
          if (active) {
            const flowTasks = await listFlowTasks(active.id);
            if (!cancelled) setTasks(flowTasks);
          }
        }
      } catch (err) {
        console.error("Failed to load TaskFlow card:", err);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    fetchFlowData();
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading || !activeFlow) {
    return null;
  }

  const total = tasks.length;
  const done = tasks.filter((t) => t.status === "DONE").length;
  const running = tasks.filter((t) => t.status === "RUNNING");
  const blocked = tasks.filter((t) => t.status === "BLOCKED");
  const percent = total > 0 ? Math.round((done / total) * 100) : 0;

  return (
    <section className="panel tf-home-card fade-in" aria-label="Active TaskFlow Overview">
      <div className="tf-home-header">
        <div className="tf-home-eyebrow-row">
          <span className="tf-home-eyebrow">TASKFLOW</span>
          <span className={`status-badge status-${activeFlow.status.toLowerCase()}`}>
            <span className="tf-pulse-dot" />
            {activeFlow.status === "RUNNING" ? "Running" : activeFlow.status}
          </span>
        </div>
        <h2 className="tf-home-title">{activeFlow.title}</h2>
        <div className="tf-home-metrics">
          <strong>{percent}%</strong> · {running.length} active · {blocked.length} blocked · {done}/{total} complete
        </div>
      </div>

      <div className="tf-home-progress-bar">
        <div className="tf-home-progress-fill" style={{ width: `${percent}%` }} />
      </div>

      <div className="tf-home-body-grid">
        {/* Needs Attention Section if blocked */}
        {blocked.length > 0 && (
          <div className="tf-home-blocked-col">
            <div className="tf-home-subhead blocked">
              <span>⚠ Needs your attention ({blocked.length})</span>
            </div>
            <div className="tf-home-tasks-list">
              {blocked.map((t) => (
                <div key={t.id} className="tf-home-task-item blocked" onClick={onOpenTaskFlow}>
                  <div className="title"><strong>{t.title}</strong></div>
                  <div className="reason">{t.block_detail || "Needs user confirmation"}</div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Active Now Section */}
        <div className="tf-home-active-col">
          <div className="tf-home-subhead active">
            <span>◉ Active ({running.length})</span>
          </div>
          <div className="tf-home-tasks-list">
            {running.length > 0 ? (
              running.map((t) => (
                <div key={t.id} className="tf-home-task-item active" onClick={onOpenTaskFlow}>
                  <div className="title"><strong>{t.title}</strong></div>
                  <div className="subagent">⚙ Isolation probe · ↳ 1 subagent</div>
                </div>
              ))
            ) : (
              <div className="text-muted small">Ready for dispatch</div>
            )}
          </div>
        </div>
      </div>

      <div className="tf-home-footer">
        <button className="tf-home-action-pill-btn" onClick={onOpenTaskFlow}>
          Open TaskFlow →
        </button>
      </div>
    </section>
  );
};
