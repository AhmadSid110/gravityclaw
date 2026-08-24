import React, { useEffect, useMemo, useState } from "react";
import {
  addTaskComment,
  blockTask,
  createFlowTask,
  createTaskFlow,
  deleteFlowTask,
  dispatchTaskFlow,
  getTaskFlow,
  getTaskHandoffs,
  getWorkspaces,
  listFlowTasks,
  listTaskAttempts,
  listTaskComments,
  listTaskFlows,
  retryTask,
  unblockTask,
  updateFlowTask,
  updateTaskFlow,
} from "./api";
import { ContextCircle } from "./ContextInspector";
import type {
  BlockReason,
  DensityMode,
  FlowTask,
  FlowTaskStatus,
  InspectorTab,
  TaskAttempt,
  TaskComment,
  TaskFlow,
  TaskFlowView,
  TaskHandoffItem,
  TaskPriority,
  WorkspaceRecord,
} from "./types";

interface TaskFlowStudioProps {
  onNavigateToRun?: (runId: string) => void;
  onOpenContextInspector?: (runId: string) => void;
}

// Strictly 5 visible columns
const COLUMNS: Array<{ key: FlowTaskStatus; label: string }> = [
  { key: "TRIAGE", label: "TRIAGE" },
  { key: "TODO", label: "TODO" },
  { key: "READY", label: "READY" },
  { key: "RUNNING", label: "RUNNING" },
  { key: "DONE", label: "DONE" },
];

const BLOCK_REASON_LABELS: Record<BlockReason, string> = {
  dependency: "Upstream Dependency",
  needs_user_input: "Needs user input",
  missing_capability: "Missing capability",
  transient_failure: "Transient error",
  external_service: "External service down",
  review_required: "Review required",
};

export const TaskFlowStudio: React.FC<TaskFlowStudioProps> = ({
  onNavigateToRun,
  onOpenContextInspector,
}) => {
  const [workspaces, setWorkspaces] = useState<WorkspaceRecord[]>([]);
  const [flows, setFlows] = useState<TaskFlow[]>([]);
  const [selectedFlowId, setSelectedFlowId] = useState<string>("");
  const [tasks, setTasks] = useState<FlowTask[]>([]);
  const [dispatching, setDispatching] = useState<boolean>(false);

  // View & Density States
  const [currentView, setCurrentView] = useState<TaskFlowView>("board");
  const [density, setDensity] = useState<DensityMode>(() => {
    try {
      return (localStorage.getItem("gravityclaw:taskflow-density") as DensityMode) || "comfortable";
    } catch {
      return "comfortable";
    }
  });
  const [priorityFilter, setPriorityFilter] = useState<string>("ALL");
  const [showDoneCompleted, setShowDoneCompleted] = useState<boolean>(false);
  const [objectiveCollapsed, setObjectiveCollapsed] = useState<boolean>(false);
  const [showMobileFilterSheet, setShowMobileFilterSheet] = useState<boolean>(false);
  const [mobileBoardViewMode, setMobileBoardViewMode] = useState<"board" | "list">("board");
  const [activeMobileColumnIdx, setActiveMobileColumnIdx] = useState<number>(1); // Default to TODO (idx 1)

  // Title Editing State
  const [isEditingTitle, setIsEditingTitle] = useState<boolean>(false);
  const [titleDraft, setTitleDraft] = useState<string>("");

  // Inspector & Modals State
  const [selectedTask, setSelectedTask] = useState<FlowTask | null>(null);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("overview");
  const [showNewFlowModal, setShowNewFlowModal] = useState<boolean>(false);
  const [showNewTaskModal, setShowNewTaskModal] = useState<boolean>(false);
  const [showBlockModal, setShowBlockModal] = useState<boolean>(false);
  const [showUnblockModal, setShowUnblockModal] = useState<boolean>(false);
  const [showOverrideModal, setShowOverrideModal] = useState<boolean>(false);
  const [overrideTaskId, setOverrideTaskId] = useState<string | null>(null);
  const [overrideReason, setOverrideReason] = useState<string>("");

  // Drawer details
  const [taskAttempts, setTaskAttempts] = useState<TaskAttempt[]>([]);
  const [taskComments, setTaskComments] = useState<TaskComment[]>([]);
  const [taskHandoffs, setTaskHandoffs] = useState<TaskHandoffItem[]>([]);
  const [newCommentBody, setNewCommentBody] = useState<string>("");

  // New Flow Form & Plan Proposal State
  const [newFlowObjective, setNewFlowObjective] = useState("");
  const [newFlowWorkspace, setNewFlowWorkspace] = useState("");
  const [planStage, setPlanStage] = useState<"input" | "proposal">("input");
  const [proposedPlan, setProposedPlan] = useState<Array<{ title: string; criteria: string[] }>>([]);

  // New Task Form
  const [newTaskTitle, setNewTaskTitle] = useState("");
  const [newTaskBody, setNewTaskBody] = useState("");
  const [newTaskPriority, setNewTaskPriority] = useState<TaskPriority>("MEDIUM");
  const [newTaskAssignee, setNewTaskAssignee] = useState("default");
  const [newTaskCriteria, setNewTaskCriteria] = useState<string[]>([""]);
  const [newTaskParentIds, setNewTaskParentIds] = useState<string[]>([]);

  // Block/Unblock fields
  const [blockReason, setBlockReason] = useState<BlockReason>("needs_user_input");
  const [blockDetail, setBlockDetail] = useState("");
  const [unblockComment, setUnblockComment] = useState("");

  // Mobile detection
  const [isMobile, setIsMobile] = useState<boolean>(() => window.innerWidth < 768);

  useEffect(() => {
    const handleResize = () => setIsMobile(window.innerWidth < 768);
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);

  const activeFlow = useMemo(
    () => flows.find((f) => f.id === selectedFlowId) || flows[0] || null,
    [flows, selectedFlowId]
  );

  const loadInitialData = async () => {
    try {
      const [wsList, flowList] = await Promise.all([getWorkspaces(), listTaskFlows()]);
      setWorkspaces(wsList);
      if (wsList.length > 0 && !newFlowWorkspace) {
        setNewFlowWorkspace(wsList[0].id);
      }
      setFlows(flowList);
      if (flowList.length > 0 && (!selectedFlowId || !flowList.some((f) => f.id === selectedFlowId))) {
        setSelectedFlowId(flowList[0].id);
      }
    } catch (err) {
      console.error("Failed to load TaskFlow data:", err);
    }
  };

  const loadTasks = async (flowId: string) => {
    if (!flowId) {
      setTasks([]);
      return;
    }
    try {
      const data = await listFlowTasks(flowId);
      setTasks(data);
    } catch (err) {
      console.error("Failed to load tasks:", err);
    }
  };

  useEffect(() => {
    loadInitialData();
  }, []);

  useEffect(() => {
    if (activeFlow) {
      loadTasks(activeFlow.id);
      setTitleDraft(activeFlow.title);
    }
  }, [activeFlow?.id]);

  // Live polling for running tasks
  useEffect(() => {
    if (!activeFlow) return;
    const hasRunning = tasks.some((t) => t.status === "RUNNING");
    if (!hasRunning) return;

    const timer = setInterval(() => {
      loadTasks(activeFlow.id);
      getTaskFlow(activeFlow.id).then((updated) => {
        setFlows((prev) => prev.map((f) => (f.id === updated.id ? updated : f)));
      });
    }, 2000);

    return () => clearInterval(timer);
  }, [activeFlow?.id, tasks]);

  // Load Inspector details
  const openTaskInspector = async (task: FlowTask) => {
    setSelectedTask(task);
    try {
      const [attempts, comments, handoffs] = await Promise.all([
        listTaskAttempts(task.id),
        listTaskComments(task.id),
        getTaskHandoffs(task.id),
      ]);
      setTaskAttempts(attempts);
      setTaskComments(comments);
      setTaskHandoffs(handoffs);
    } catch (err) {
      console.error("Failed to load task details:", err);
    }
  };

  // Actions
  const handleDispatch = async () => {
    if (!activeFlow) return;
    setDispatching(true);
    try {
      await dispatchTaskFlow(activeFlow.id);
      await loadTasks(activeFlow.id);
      const updated = await getTaskFlow(activeFlow.id);
      setFlows((prev) => prev.map((f) => (f.id === updated.id ? updated : f)));
    } catch (err) {
      console.error("Dispatch failed:", err);
    } finally {
      setDispatching(false);
    }
  };

  const handleSaveTitle = async () => {
    if (!activeFlow || !titleDraft.trim() || titleDraft.trim() === activeFlow.title) {
      setIsEditingTitle(false);
      return;
    }
    try {
      const updated = await updateTaskFlow(activeFlow.id, { title: titleDraft.trim() });
      setFlows((prev) => prev.map((f) => (f.id === updated.id ? updated : f)));
      setIsEditingTitle(false);
    } catch (err) {
      console.error("Failed to update title:", err);
    }
  };

  const handleProposePlan = (e: React.FormEvent) => {
    e.preventDefault();
    if (!newFlowObjective.trim()) return;

    // Propose an initial structured decomposition plan
    const obj = newFlowObjective.trim();
    const isDeploy = /deploy|release|prod/i.test(obj);
    const isTest = /test|audit|verify/i.test(obj);

    const generated: Array<{ title: string; criteria: string[] }> = isDeploy
      ? [
          { title: "Verify backend test suite", criteria: ["All unit tests pass", "0 test regressions"] },
          { title: "Frontend bundle verification", criteria: ["Clean vite production build", "Responsive layout check"] },
          { title: "Security & configuration audit", criteria: ["Secret leakage check", "Isolated runtime verified"] },
          { title: "Dry-run deployment to staging", criteria: ["Database migrations applied", "Health check OK"] },
          { title: "Production rollout & certification", criteria: ["Service active", "Rollback plan verified"] },
        ]
      : isTest
      ? [
          { title: "Audit dependencies & contracts", criteria: ["Verify schema migrations", "DAG cycle checks"] },
          { title: "Run end-to-end integration tests", criteria: ["Multi-worker concurrency OK", "Recovery test passes"] },
          { title: "Generate test report summary", criteria: ["Summary artifact produced", "Sign-off"] },
        ]
      : [
          { title: `Decompose requirements: ${obj.slice(0, 32)}...`, criteria: ["Acceptance criteria documented", "Dependencies mapped"] },
          { title: "Implement core solution", criteria: ["Code changes implemented", "Local linting clean"] },
          { title: "Validate and certify results", criteria: ["Automated tests verify solution", "Summary written"] },
        ];

    setProposedPlan(generated);
    setPlanStage("proposal");
  };

  const handleCreateFlowWithPlan = async () => {
    if (!newFlowObjective.trim()) return;
    try {
      const title = newFlowObjective.length > 36 ? `${newFlowObjective.slice(0, 36)}…` : newFlowObjective;
      const flow = await createTaskFlow({
        title,
        objective: newFlowObjective.trim(),
        workspace_id: newFlowWorkspace || workspaces[0]?.id || "default",
      });

      // Create proposed tasks sequentially with dependencies
      let previousId: string | null = null;
      for (const item of proposedPlan) {
        const t = await createFlowTask(flow.id, {
          title: item.title,
          body: `Task generated for objective: ${newFlowObjective}`,
          workspace_id: flow.workspace_id,
          acceptance_criteria: item.criteria,
          parent_ids: previousId ? [previousId] : [],
        });
        previousId = t.id;
      }

      setFlows((prev) => [flow, ...prev]);
      setSelectedFlowId(flow.id);
      setShowNewFlowModal(false);
      setNewFlowObjective("");
      setPlanStage("input");
    } catch (err) {
      console.error("Failed to create flow with plan:", err);
    }
  };

  const handleCreateTask = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!activeFlow || !newTaskTitle.trim()) return;
    try {
      const cleanCriteria = newTaskCriteria.map((c) => c.trim()).filter(Boolean);
      const task = await createFlowTask(activeFlow.id, {
        title: newTaskTitle.trim(),
        body: newTaskBody.trim(),
        workspace_id: activeFlow.workspace_id,
        priority: newTaskPriority,
        assignee_profile: newTaskAssignee.trim() || "default",
        acceptance_criteria: cleanCriteria,
        parent_ids: newTaskParentIds,
      });
      setTasks((prev) => [...prev, task]);
      setShowNewTaskModal(false);
      setNewTaskTitle("");
      setNewTaskBody("");
      setNewTaskCriteria([""]);
      setNewTaskParentIds([]);
    } catch (err) {
      console.error("Failed to create task:", err);
    }
  };

  const handleAddComment = async () => {
    if (!selectedTask || !newCommentBody.trim()) return;
    try {
      const comment = await addTaskComment(selectedTask.id, newCommentBody.trim(), "user", "user");
      setTaskComments((prev) => [...prev, comment]);
      setNewCommentBody("");
    } catch (err) {
      console.error("Failed to add comment:", err);
    }
  };

  const handleBlockTask = async () => {
    if (!selectedTask) return;
    try {
      const updated = await blockTask(selectedTask.id, blockReason, blockDetail);
      setTasks((prev) => prev.map((t) => (t.id === updated.id ? updated : t)));
      setSelectedTask(updated);
      setShowBlockModal(false);
      setBlockDetail("");
      const comments = await listTaskComments(selectedTask.id);
      setTaskComments(comments);
    } catch (err) {
      console.error("Failed to block task:", err);
    }
  };

  const handleUnblockTask = async () => {
    if (!selectedTask) return;
    try {
      const updated = await unblockTask(selectedTask.id, unblockComment);
      setTasks((prev) => prev.map((t) => (t.id === updated.id ? updated : t)));
      setSelectedTask(updated);
      setShowUnblockModal(false);
      setUnblockComment("");
      const comments = await listTaskComments(selectedTask.id);
      setTaskComments(comments);
    } catch (err) {
      console.error("Failed to unblock task:", err);
    }
  };

  const handleRetryTask = async (task: FlowTask) => {
    try {
      const updated = await retryTask(task.id, "User triggered retry");
      setTasks((prev) => prev.map((t) => (t.id === updated.id ? updated : t)));
      if (selectedTask?.id === task.id) {
        setSelectedTask(updated);
      }
    } catch (err) {
      console.error("Failed to retry task:", err);
    }
  };

  const handleDeleteTask = async (taskId: string) => {
    if (!confirm("Are you sure you want to delete this task?")) return;
    try {
      await deleteFlowTask(taskId);
      setTasks((prev) => prev.filter((t) => t.id !== taskId));
      if (selectedTask?.id === taskId) {
        setSelectedTask(null);
      }
    } catch (err) {
      console.error("Failed to delete task:", err);
    }
  };

  // Drag and Drop (Human-controlled state changes)
  const handleDropOnColumn = async (e: React.DragEvent, targetStatus: FlowTaskStatus) => {
    e.preventDefault();
    const taskId = e.dataTransfer.getData("text/plain");
    if (!taskId) return;
    const task = tasks.find((t) => t.id === taskId);
    if (!task || task.status === targetStatus) return;

    // Disallow dragging into RUNNING directly (runtime agent truth)
    if (targetStatus === "RUNNING") {
      alert("Tasks enter RUNNING automatically when claimed and executed by AGY.");
      return;
    }

    // Dragging to DONE requires override confirmation if criteria incomplete
    if (targetStatus === "DONE") {
      setOverrideTaskId(task.id);
      setShowOverrideModal(true);
      return;
    }

    try {
      const updated = await updateFlowTask(task.id, { status: targetStatus });
      setTasks((prev) => prev.map((t) => (t.id === updated.id ? updated : t)));
      if (selectedTask?.id === task.id) setSelectedTask(updated);
    } catch (err) {
      console.error("Failed to move task:", err);
    }
  };

  const handleConfirmOverrideDone = async () => {
    if (!overrideTaskId) return;
    try {
      const updated = await updateFlowTask(overrideTaskId, { status: "DONE" });
      if (overrideReason.trim()) {
        await addTaskComment(
          overrideTaskId,
          `Manual completion override: ${overrideReason.trim()}`,
          "user",
          "user"
        );
      }
      setTasks((prev) => prev.map((t) => (t.id === updated.id ? updated : t)));
      if (selectedTask?.id === overrideTaskId) setSelectedTask(updated);
      setShowOverrideModal(false);
      setOverrideTaskId(null);
      setOverrideReason("");
    } catch (err) {
      console.error("Failed to force complete task:", err);
    }
  };

  // Metrics computation
  const stats = useMemo(() => {
    const total = tasks.length;
    const done = tasks.filter((t) => t.status === "DONE").length;
    const running = tasks.filter((t) => t.status === "RUNNING").length;
    const blocked = tasks.filter((t) => t.status === "BLOCKED").length;
    const ready = tasks.filter((t) => t.status === "READY").length;
    const todo = tasks.filter((t) => t.status === "TODO").length;
    const triage = tasks.filter((t) => t.status === "TRIAGE").length;
    const percent = total > 0 ? Math.round((done / total) * 100) : 0;
    return { total, done, running, blocked, ready, todo, triage, percent };
  }, [tasks]);

  // Tasks grouped by column (filtered by priority if set)
  const filteredTasks = useMemo(() => {
    if (priorityFilter === "ALL") return tasks;
    return tasks.filter((t) => t.priority === priorityFilter);
  }, [tasks, priorityFilter]);

  const tasksByColumn = useMemo(() => {
    const map: Record<string, FlowTask[]> = {
      TRIAGE: [],
      TODO: [],
      READY: [],
      RUNNING: [],
      DONE: [],
      BLOCKED: [],
      FAILED: [],
    };
    filteredTasks.forEach((t) => {
      if (t.status === "BLOCKED") {
        map.BLOCKED.push(t);
        // Place in logical stage: if parents are done or no parents -> READY, else TODO
        const allParentsDone = !t.parent_ids?.length || t.parent_ids.every((pid) => {
          const p = tasks.find((item) => item.id === pid);
          return p && p.status === "DONE";
        });
        if (allParentsDone) {
          map.READY.push(t);
        } else {
          map.TODO.push(t);
        }
      } else if (t.status === "FAILED") {
        map.FAILED.push(t);
        map.TODO.push(t);
      } else if (map[t.status]) {
        map[t.status].push(t);
      } else {
        map.TODO.push(t);
      }
    });
    return map;
  }, [filteredTasks, tasks]);

  return (
    <div className={`taskflow-app-canvas ${density === "compact" ? "density-compact" : "density-comfortable"}`}>
      {/* ─── Compact Flow Header ─── */}
      <header className="tf-header">
        <div className="tf-header-main">
          <div className="tf-title-row">
            {isEditingTitle ? (
              <input
                type="text"
                className="tf-title-input"
                value={titleDraft}
                onChange={(e) => setTitleDraft(e.target.value)}
                onBlur={handleSaveTitle}
                onKeyDown={(e) => e.key === "Enter" && handleSaveTitle()}
                autoFocus
              />
            ) : (
              <h1
                className="tf-flow-title"
                onClick={() => setIsEditingTitle(true)}
                title="Click to rename flow"
              >
                {activeFlow ? activeFlow.title : "TaskFlow"}
                <span className="tf-edit-pencil">✎</span>
              </h1>
            )}

            {activeFlow && (
              <span className={`tf-status-badge status-${activeFlow.status.toLowerCase()}`}>
                <span className="tf-pulse-dot" />
                {activeFlow.status === "RUNNING" ? "Running" : activeFlow.status}
              </span>
            )}
          </div>

          {activeFlow && (
            <div className="tf-objective-container">
              <p
                className={`tf-objective-text ${objectiveCollapsed ? "collapsed" : ""}`}
                onClick={() => setObjectiveCollapsed(!objectiveCollapsed)}
                title="Click to expand/collapse objective"
              >
                {activeFlow.objective}
              </p>
            </div>
          )}

          {/* Key Metrics Row */}
          <div className="tf-metrics-row">
            <span className="tf-metric-item">
              <strong>{stats.done}</strong> of <strong>{stats.total}</strong> complete
            </span>
            <span className="tf-metric-dot">·</span>
            <span className="tf-metric-item active-metric">
              <strong>{stats.running}</strong> active
            </span>
            <span className="tf-metric-dot">·</span>
            <span className="tf-metric-item blocked-metric">
              <strong>{stats.blocked}</strong> blocked
            </span>
          </div>

          {/* Progress Bar */}
          <div className="tf-progress-track">
            <div className="tf-progress-fill" style={{ width: `${stats.percent}%` }} />
            <span className="tf-progress-label">{stats.percent}%</span>
          </div>
        </div>

        {/* ── Streamlined View Navigation & Action Toolbar ── */}
        <div className="tf-header-controls streamlined-tf-controls">
          <div className="tf-controls-top-row">
            <nav className="tf-view-tabs" role="tablist">
              <button
                className={`tf-tab-btn ${currentView === "board" ? "active" : ""}`}
                onClick={() => setCurrentView("board")}
                role="tab"
                aria-selected={currentView === "board"}
              >
                Board
              </button>
              <button
                className={`tf-tab-btn ${currentView === "timeline" ? "active" : ""}`}
                onClick={() => setCurrentView("timeline")}
                role="tab"
                aria-selected={currentView === "timeline"}
              >
                Timeline
              </button>
              <button
                className={`tf-tab-btn ${currentView === "activity" ? "active" : ""}`}
                onClick={() => setCurrentView("activity")}
                role="tab"
                aria-selected={currentView === "activity"}
              >
                Activity
              </button>
            </nav>

            <div className="tf-top-right-actions">
              <button
                className="btn btn-secondary btn-sm tf-filter-toggle-btn"
                onClick={() => setShowMobileFilterSheet(true)}
                title="Filter & density options"
                aria-label="Filter options"
              >
                <span>⚙ Options & Filters</span>
              </button>
            </div>
          </div>

          <div className="tf-controls-sub-row">
            {/* Flow Switcher Dropdown */}
            {flows.length > 0 && (
              <div className="tf-flow-select-wrap">
                <select
                  className="tf-select-control flow-switcher"
                  value={activeFlow?.id || ""}
                  onChange={(e) => setSelectedFlowId(e.target.value)}
                  title="Switch active TaskFlow"
                >
                  {flows.map((f) => (
                    <option key={f.id} value={f.id}>
                      {f.title}
                    </option>
                  ))}
                </select>
              </div>
            )}

            {/* Segmented View Mode Toggle: List vs Board */}
            {currentView === "board" && (
              <div className="tf-segmented-control view-mode-toggle" title="Switch between Kanban Board and Grouped List">
                <button
                  className={`tf-segment-btn ${mobileBoardViewMode === "board" ? "active" : ""}`}
                  onClick={() => setMobileBoardViewMode("board")}
                >
                  ▦ Board
                </button>
                <button
                  className={`tf-segment-btn ${mobileBoardViewMode === "list" ? "active" : ""}`}
                  onClick={() => setMobileBoardViewMode("list")}
                >
                  ☷ List
                </button>
              </div>
            )}

            <div className="tf-action-buttons-row">
              <button
                className="btn btn-secondary btn-sm tf-dispatch-btn"
                onClick={handleDispatch}
                disabled={dispatching}
                title="Trigger dispatcher reconciliation tick"
              >
                {dispatching ? "⚡ Reconciling…" : "⚡ Dispatch"}
              </button>

              <button
                className="btn btn-primary btn-sm tf-add-task-btn"
                onClick={() => setShowNewTaskModal(true)}
                title="Create a new task in this TaskFlow"
              >
                ＋ New Task
              </button>
            </div>
          </div>
        </div>
      </header>

      {/* ── Mobile Filter & Density Bottom Sheet ── */}
      {showMobileFilterSheet && (
        <div className="tf-sheet-backdrop" onClick={() => setShowMobileFilterSheet(false)}>
          <div className="tf-bottom-sheet" onClick={(e) => e.stopPropagation()}>
            <div className="tf-sheet-header">
              <h3>TaskFlow Options</h3>
              <button className="icon-button" onClick={() => setShowMobileFilterSheet(false)}>&times;</button>
            </div>

            <div className="tf-sheet-section">
              <label className="tf-sheet-label">VIEW DENSITY</label>
              <div className="tf-segmented-control full-width">
                <button
                  className={`tf-segment-btn ${density === "comfortable" ? "active" : ""}`}
                  onClick={() => {
                    setDensity("comfortable");
                    try { localStorage.setItem("gravityclaw:taskflow-density", "comfortable"); } catch {}
                  }}
                >
                  Comfortable
                </button>
                <button
                  className={`tf-segment-btn ${density === "compact" ? "active" : ""}`}
                  onClick={() => {
                    setDensity("compact");
                    try { localStorage.setItem("gravityclaw:taskflow-density", "compact"); } catch {}
                  }}
                >
                  Compact
                </button>
              </div>
            </div>

            <div className="tf-sheet-section">
              <label className="tf-sheet-label">PRIORITY FILTER</label>
              <select
                className="tf-select-control full-width"
                value={priorityFilter}
                onChange={(e) => setPriorityFilter(e.target.value)}
              >
                <option value="ALL">All Priorities</option>
                <option value="URGENT">Urgent</option>
                <option value="HIGH">High</option>
                <option value="MEDIUM">Medium</option>
                <option value="LOW">Low</option>
              </select>
            </div>

            <div className="tf-sheet-section">
              <label className="tf-sheet-label">TASKFLOWS</label>
              <button
                className="btn btn-secondary full-width"
                onClick={() => {
                  setShowMobileFilterSheet(false);
                  setShowNewFlowModal(true);
                }}
              >
                ＋ Create New TaskFlow Objective
              </button>
            </div>

            <div className="tf-sheet-footer">
              <button className="btn btn-primary full-width" onClick={() => setShowMobileFilterSheet(false)}>
                Done
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ─── Attention Banner for Blocked Tasks ─── */}
      {stats.blocked > 0 && (
        <div className="tf-attention-strip">
          <div className="tf-attention-content">
            <span className="tf-attn-icon">⚠</span>
            <div className="tf-attn-text">
              <strong>{stats.blocked} task{stats.blocked > 1 ? "s" : ""} need{stats.blocked === 1 ? "s" : ""} your attention:</strong>{" "}
              {tasks.filter((t) => t.status === "BLOCKED").map((t, idx) => (
                <span
                  key={t.id}
                  className="tf-attn-item-link"
                  onClick={() => {
                    setSelectedTask(t);
                    setShowUnblockModal(true);
                  }}
                  title="Click to respond and unblock"
                >
                  {t.title} ({t.block_reason ? BLOCK_REASON_LABELS[t.block_reason] || t.block_reason : "Needs input"}){idx < stats.blocked - 1 ? ", " : ""}
                </span>
              ))}
            </div>
          </div>
          <button
            className="btn btn-primary btn-sm"
            onClick={() => {
              const firstBlocked = tasks.find((t) => t.status === "BLOCKED");
              if (firstBlocked) {
                setSelectedTask(firstBlocked);
                setShowUnblockModal(true);
              }
            }}
          >
            Respond Now
          </button>
        </div>
      )}

      {/* ── Main View Content Area ── */}
      <main className="tf-main-stage">
        {currentView === "board" && (
          isMobile ? (
            /* Mobile Responsive Board & Grouped List View */
            <div className="tf-mobile-list-view">
              {mobileBoardViewMode === "board" ? (
                <>
                  {/* Status Column Tabs for Board Mode (Horizontal Scroll) */}
                  <div className="tf-mobile-pills-bar">
                    {COLUMNS.map((col, idx) => {
                      const count = (tasksByColumn[col.key] || []).length;
                      return (
                        <button
                          key={col.key}
                          className={`tf-mobile-pill ${activeMobileColumnIdx === idx ? "active" : ""}`}
                          onClick={() => setActiveMobileColumnIdx(idx)}
                        >
                          {col.label} {count > 0 ? `(${count})` : "0"}
                        </button>
                      );
                    })}
                  </div>

                  {/* Selected Column Content */}
                  <div className="tf-mobile-single-col">
                    {(() => {
                      const col = COLUMNS[activeMobileColumnIdx] || COLUMNS[0];
                      const colTasks = tasksByColumn[col.key] || [];
                      return (
                        <div className="tf-column-inner">
                          <div className="tf-col-header">
                            <h3>{col.label}</h3>
                            <span className="tf-count-pill">{colTasks.length}</span>
                          </div>
                          <div className="tf-col-cards">
                            {colTasks.length === 0 ? (
                              <div className="tf-empty-col">No {col.label.toLowerCase()} tasks</div>
                            ) : (
                              colTasks.map((t) => (
                                <TaskCard
                                  key={t.id}
                                  task={t}
                                  onSelect={() => openTaskInspector(t)}
                                  onRespondBlocked={() => {
                                    setSelectedTask(t);
                                    setShowUnblockModal(true);
                                  }}
                                  onRetry={() => handleRetryTask(t)}
                                />
                              ))
                            )}
                          </div>
                        </div>
                      );
                    })()}
                  </div>
                </>
              ) : (
                /* Grouped List of All Tasks */
                <div className="tf-grouped-list">
                  {tasksByColumn.BLOCKED && tasksByColumn.BLOCKED.length > 0 && (
                    <section className="tf-mobile-group-section blocked-section">
                      <div className="tf-mobile-group-header" style={{ color: "#f59e0b" }}>
                        <span>⚠ NEEDS YOUR ATTENTION (BLOCKED)</span>
                        <span className="tf-count-pill" style={{ background: "rgba(245, 158, 11, 0.2)", color: "#f59e0b" }}>
                          {tasksByColumn.BLOCKED.length}
                        </span>
                      </div>
                      <div className="tf-mobile-group-cards">
                        {tasksByColumn.BLOCKED.map((t) => (
                          <TaskCard
                            key={t.id}
                            task={t}
                            onSelect={() => openTaskInspector(t)}
                            onRespondBlocked={() => {
                              setSelectedTask(t);
                              setShowUnblockModal(true);
                            }}
                            onRetry={() => handleRetryTask(t)}
                          />
                        ))}
                      </div>
                    </section>
                  )}

                  {COLUMNS.map((col) => {
                    const colTasks = (tasksByColumn[col.key] || []).filter((t) => t.status !== "BLOCKED");
                    if (colTasks.length === 0) return null;
                    return (
                      <section key={col.key} className="tf-mobile-group-section">
                        <div className="tf-mobile-group-header">
                          <span>{col.label}</span>
                          <span className="tf-count-pill">{colTasks.length}</span>
                        </div>
                        <div className="tf-mobile-group-cards">
                          {colTasks.map((t) => (
                            <TaskCard
                              key={t.id}
                              task={t}
                              onSelect={() => openTaskInspector(t)}
                              onRespondBlocked={() => {
                                setSelectedTask(t);
                                setShowUnblockModal(true);
                              }}
                              onRetry={() => handleRetryTask(t)}
                            />
                          ))}
                        </div>
                      </section>
                    );
                  })}
                </div>
              )}
            </div>
          ) : (
            /* Desktop 5-Column Kanban Board */
            <div className="tf-board-grid">
              {COLUMNS.map((col) => {
                const colTasks = tasksByColumn[col.key] || [];
                const isDoneCol = col.key === "DONE";

                return (
                  <div
                    key={col.key}
                    className="tf-board-column"
                    onDragOver={(e) => e.preventDefault()}
                    onDrop={(e) => handleDropOnColumn(e, col.key)}
                  >
                    <div className="tf-column-header-row">
                      <h3 className="tf-column-heading">{col.label}</h3>
                      <span className="tf-count-pill">{colTasks.length}</span>
                    </div>

                    <div className="tf-cards-stack">
                      {isDoneCol && colTasks.length > 5 && !showDoneCompleted ? (
                        <div className="tf-collapsed-done-card">
                          <div className="tf-collapsed-meta">
                            <span>✓ {colTasks.length} completed</span>
                          </div>
                          <button
                            className="btn btn-secondary btn-sm"
                            onClick={() => setShowDoneCompleted(true)}
                          >
                            Show completed
                          </button>
                        </div>
                      ) : (
                        colTasks.map((task) => (
                          <TaskCard
                            key={task.id}
                            task={task}
                            onSelect={() => openTaskInspector(task)}
                            onRespondBlocked={() => {
                              setSelectedTask(task);
                              setShowUnblockModal(true);
                            }}
                            onRetry={() => handleRetryTask(task)}
                          />
                        ))
                      )}

                      {colTasks.length === 0 && (
                        <div className="tf-empty-col-dropzone">
                          <span>Empty</span>
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )
        )}

        {/* ── Timeline View ── */}
        {currentView === "timeline" && (
          <TimelineView
            tasks={tasks}
            onSelectTask={openTaskInspector}
          />
        )}

        {/* ── Dependencies DAG View ── */}
        {currentView === "dependencies" && (
          <DependenciesDAGView
            tasks={tasks}
            onSelectTask={openTaskInspector}
          />
        )}

        {/* ── Activity View ── */}
        {currentView === "activity" && (
          <ActivityFeedView
            tasks={tasks}
            onSelectTask={openTaskInspector}
            onNavigateToRun={onNavigateToRun}
          />
        )}
      </main>

      {/* ── Right-Side Task Inspector (Desktop Slide-Over / Mobile Bottom Sheet) ── */}
      {selectedTask && (
        <aside
          className={`tf-inspector-drawer ${isMobile ? "bottom-sheet" : "side-inspector"}`}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="tf-inspector-header">
            <div className="tf-insp-title-block">
              <div className="tf-insp-badges">
                <span className={`tf-insp-status status-${selectedTask.status.toLowerCase()}`}>
                  ● {selectedTask.status}
                </span>
                <span className="tf-insp-priority">{selectedTask.priority}</span>
              </div>
              <h2 className="tf-insp-title">{selectedTask.title}</h2>
              {selectedTask.body && (
                <p className="tf-insp-desc">{selectedTask.body}</p>
              )}
            </div>

            <button
              className="tf-insp-close-btn"
              onClick={() => setSelectedTask(null)}
              aria-label="Close inspector"
            >
              ✕
            </button>
          </div>

          {/* Inspector Navigation Tabs */}
          <nav className="tf-insp-nav" role="tablist">
            <button
              className={`tf-insp-tab ${inspectorTab === "overview" ? "active" : ""}`}
              onClick={() => setInspectorTab("overview")}
            >
              Overview
            </button>
            <button
              className={`tf-insp-tab ${inspectorTab === "activity" ? "active" : ""}`}
              onClick={() => setInspectorTab("activity")}
            >
              Activity
            </button>
            <button
              className={`tf-insp-tab ${inspectorTab === "runs" ? "active" : ""}`}
              onClick={() => setInspectorTab("runs")}
            >
              Runs ({taskAttempts.length})
            </button>
            <button
              className={`tf-insp-tab ${inspectorTab === "comments" ? "active" : ""}`}
              onClick={() => setInspectorTab("comments")}
            >
              Comments ({taskComments.length})
            </button>
            <button
              className={`tf-insp-tab ${inspectorTab === "artifacts" ? "active" : ""}`}
              onClick={() => setInspectorTab("artifacts")}
            >
              Files
            </button>
          </nav>

          <div className="tf-insp-body">
            {inspectorTab === "overview" && (
              <div className="tf-insp-overview-tab">
                {/* Blocked Alert Banner if blocked */}
                {selectedTask.status === "BLOCKED" && (
                  <div className="tf-blocked-banner">
                    <div className="tf-blocked-banner-title">
                      ⚠ Blocked: {selectedTask.block_reason && BLOCK_REASON_LABELS[selectedTask.block_reason]}
                    </div>
                    <div className="tf-blocked-banner-body">
                      {selectedTask.block_detail || "Awaiting resolution from user or upstream resource."}
                    </div>
                    <div className="tf-blocked-banner-actions">
                      <button
                        className="btn btn-primary btn-sm"
                        onClick={() => setShowUnblockModal(true)}
                      >
                        Respond / Unblock
                      </button>
                    </div>
                  </div>
                )}

                {/* Acceptance Criteria Section */}
                <section className="tf-insp-section">
                  <div className="tf-section-head">
                    <h4>ACCEPTANCE</h4>
                    <span className="tf-section-sub">
                      {selectedTask.status === "DONE"
                        ? `${selectedTask.acceptance_criteria?.length || 1} of ${selectedTask.acceptance_criteria?.length || 1} complete`
                        : `${Math.max(0, (selectedTask.acceptance_criteria?.length || 1) - 1)} of ${selectedTask.acceptance_criteria?.length || 1} complete`}
                    </span>
                  </div>
                  <ul className="tf-criteria-list">
                    {(selectedTask.acceptance_criteria && selectedTask.acceptance_criteria.length > 0) ? (
                      selectedTask.acceptance_criteria.map((crit, idx) => {
                        const label = typeof crit === "string" ? crit : crit.text || crit.criterion || "";
                        const isDone = selectedTask.status === "DONE";
                        const isCurrent = selectedTask.status === "RUNNING" && idx === 0;
                        return (
                          <li key={idx} className="tf-criterion-row">
                            <span className={`tf-criterion-icon ${isDone ? "done" : isCurrent ? "active" : "pending"}`}>
                              {isDone ? "✓" : isCurrent ? "◉" : "○"}
                            </span>
                            <span className="tf-criterion-label">{label}</span>
                          </li>
                        );
                      })
                    ) : (
                      <li className="tf-criterion-row">
                        <span className={`tf-criterion-icon ${selectedTask.status === "DONE" ? "done" : "pending"}`}>
                          {selectedTask.status === "DONE" ? "✓" : "○"}
                        </span>
                        <span className="tf-criterion-label">Complete task requirements</span>
                      </li>
                    )}
                  </ul>
                </section>

                {/* Current Activity / Run Section */}
                {selectedTask.status === "RUNNING" && (
                  <section className="tf-insp-section">
                    <div className="tf-section-head">
                      <h4>CURRENT ACTIVITY</h4>
                      <span className="tf-activity-timer">02:18</span>
                    </div>
                    <div className="tf-current-activity-box">
                      <div className="tf-activity-step">
                        <span className="tf-step-icon">⚙</span>
                        <span>Running isolation probe & contract verification</span>
                      </div>
                      <div className="tf-activity-subagent">
                        <span>↳ Security subagent · Active · 48s</span>
                      </div>
                    </div>

                    <div className="tf-current-run-card">
                      <div className="tf-run-card-top">
                        <div>
                          <strong>Run #{taskAttempts[taskAttempts.length - 1]?.run_id?.slice(0, 8) || "2918"}</strong>
                          <div className="tf-run-model">Gemini 2.5 Flash · 18.2k tokens</div>
                        </div>
                        <div
                          className="tf-context-ring-wrapper"
                          onClick={() => {
                            const runId = taskAttempts[taskAttempts.length - 1]?.run_id;
                            if (runId && onOpenContextInspector) {
                              onOpenContextInspector(runId);
                            }
                          }}
                          title="Click to open Context Inspector"
                        >
                          <ContextCircle ratio={0.68} running={true} />
                          <span className="tf-context-pct">68%</span>
                        </div>
                      </div>
                      <button
                        className="btn btn-secondary btn-sm tf-open-run-btn"
                        onClick={() => {
                          const runId = taskAttempts[taskAttempts.length - 1]?.run_id;
                          if (runId && onNavigateToRun) onNavigateToRun(runId);
                        }}
                      >
                        Open Run Inspector
                      </button>
                    </div>
                  </section>
                )}

                {/* Upstream Dependencies Section */}
                <section className="tf-insp-section">
                  <div className="tf-section-head">
                    <h4>DEPENDENCIES</h4>
                  </div>
                  <div className="tf-dep-list">
                    {selectedTask.parent_ids?.length > 0 ? (
                      selectedTask.parent_ids.map((pid) => {
                        const ptask = tasks.find((t) => t.id === pid);
                        const isDone = ptask?.status === "DONE";
                        return (
                          <div key={pid} className="tf-dep-item">
                            <span className={`tf-dep-status-icon ${isDone ? "done" : ""}`}>
                              {isDone ? "✓" : "○"}
                            </span>
                            <span className="tf-dep-title">
                              {ptask ? ptask.title : pid}
                            </span>
                          </div>
                        );
                      })
                    ) : (
                      <span className="text-muted small">None (Root task)</span>
                    )}
                  </div>
                </section>

                {/* Upstream Handoffs Notes */}
                {taskHandoffs.length > 0 && (
                  <section className="tf-insp-section">
                    <div className="tf-section-head">
                      <h4>UPSTREAM HANDOFFS</h4>
                    </div>
                    {taskHandoffs.map((h, idx) => (
                      <div key={idx} className="tf-handoff-note">
                        <div className="tf-handoff-head">
                          <strong>{h.parent_task.title}</strong>
                          <span className="status-badge status-done">✓ DONE</span>
                        </div>
                        {h.comments.map((c) => (
                          <div key={c.id} className="tf-handoff-comment-line">
                            <span className="author">[{c.author_type}]:</span> {c.body}
                          </div>
                        ))}
                      </div>
                    ))}
                  </section>
                )}
              </div>
            )}

            {inspectorTab === "activity" && (
              <div className="tf-insp-activity-tab">
                <div className="tf-activity-timeline-feed">
                  <div className="tf-feed-entry">
                    <span className="time">13:06</span>
                    <span className="icon">⚙</span>
                    <div className="desc">
                      <strong>Worker spawned:</strong> AGY profile <code>TASKFLOW_WORKER</code>
                    </div>
                  </div>
                  <div className="tf-feed-entry">
                    <span className="time">13:05</span>
                    <span className="icon">▶</span>
                    <div className="desc">
                      <strong>Task promoted to READY:</strong> Upstream parent tests completed.
                    </div>
                  </div>
                  <div className="tf-feed-entry">
                    <span className="time">13:01</span>
                    <span className="icon">◻</span>
                    <div className="desc">
                      <strong>Task created:</strong> Waiting on dependencies.
                    </div>
                  </div>
                </div>
              </div>
            )}

            {inspectorTab === "runs" && (
              <div className="tf-insp-runs-tab">
                {taskAttempts.length === 0 ? (
                  <p className="text-muted">No runs executed yet for this task.</p>
                ) : (
                  <div className="tf-attempts-list">
                    {taskAttempts.map((att) => (
                      <div key={att.id} className="tf-attempt-card">
                        <div className="tf-att-head">
                          <strong>Attempt #{att.attempt_no}</strong>
                          <span className={`status-badge status-${(att.outcome || "running").toLowerCase()}`}>
                            {att.outcome || "RUNNING"}
                          </span>
                        </div>
                        <div className="tf-att-summary">{att.summary || "In progress"}</div>
                        <div className="tf-att-footer">
                          <span className="tf-att-runid">Run `{att.run_id.slice(0, 8)}`</span>
                          <button
                            className="btn btn-link btn-sm"
                            onClick={() => onNavigateToRun && onNavigateToRun(att.run_id)}
                          >
                            Inspect Run →
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {inspectorTab === "comments" && (
              <div className="tf-insp-comments-tab">
                <div className="tf-comments-stream">
                  {taskComments.map((c) => (
                    <div key={c.id} className="tf-comment-bubble">
                      <div className="tf-comment-meta">
                        <span className="author">{c.author_type === "agent" ? "GravityClaw" : c.author_type}</span>
                        <span className="time">{new Date(c.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
                      </div>
                      <div className="tf-comment-content">{c.body}</div>
                    </div>
                  ))}
                  {taskComments.length === 0 && (
                    <p className="text-muted">No comments or handoff notes yet.</p>
                  )}
                </div>

                <div className="tf-add-comment-row">
                  <input
                    type="text"
                    className="tf-comment-input"
                    placeholder="Add comment or instruction…"
                    value={newCommentBody}
                    onChange={(e) => setNewCommentBody(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && handleAddComment()}
                  />
                  <button
                    className="btn btn-secondary btn-sm"
                    onClick={handleAddComment}
                    disabled={!newCommentBody.trim()}
                  >
                    Post
                  </button>
                </div>
              </div>
            )}

            {inspectorTab === "artifacts" && (
              <div className="tf-insp-artifacts-tab">
                <div className="tf-files-list">
                  <div className="tf-file-item">
                    <span className="icon">📄</span>
                    <div className="file-info">
                      <strong>security_audit_report.md</strong>
                      <small>Generated by Run #2918 · 14.2 KB</small>
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* Action Footer Bar */}
          <div className="tf-inspector-footer">
            {selectedTask.status === "BLOCKED" ? (
              <button
                className="btn btn-primary btn-sm"
                onClick={() => setShowUnblockModal(true)}
              >
                <span>✓</span> Respond & Unblock
              </button>
            ) : selectedTask.status === "RUNNING" ? (
              <button
                className="btn btn-secondary btn-sm"
                onClick={() => handleRetryTask(selectedTask)}
              >
                <span>⏹</span> Stop Run
              </button>
            ) : (
              <button
                className="btn btn-secondary btn-sm"
                onClick={() => setShowBlockModal(true)}
              >
                <span>⏸</span> Block Task
              </button>
            )}

            <button
              className="btn btn-secondary btn-sm"
              onClick={() => handleRetryTask(selectedTask)}
            >
              <span>↺</span> Retry
            </button>

            <button
              className="btn btn-danger-subtle btn-sm"
              onClick={() => handleDeleteTask(selectedTask.id)}
              title="Delete task"
            >
              <span>🗑</span> Delete
            </button>
          </div>
        </aside>
      )}

      {/* ── New TaskFlow Creation Modal (Propose Plan) ── */}
      {showNewFlowModal && (
        <div className="tf-modal-scrim" onClick={() => setShowNewFlowModal(false)}>
          <div className="tf-modal-sheet" onClick={(e) => e.stopPropagation()}>
            {planStage === "input" ? (
              <>
                <h3 className="tf-modal-heading">Create TaskFlow</h3>
                <p className="tf-modal-lead">
                  What should GravityClaw accomplish?
                </p>
                <form onSubmit={handleProposePlan}>
                  <div className="tf-form-field">
                    <textarea
                      className="tf-form-textarea"
                      placeholder="e.g. Prepare GravityClaw for its first production release."
                      value={newFlowObjective}
                      onChange={(e) => setNewFlowObjective(e.target.value)}
                      rows={3}
                      required
                      autoFocus
                    />
                  </div>

                  <div className="tf-form-field">
                    <label>Workspace</label>
                    <select
                      className="tf-select-control"
                      value={newFlowWorkspace}
                      onChange={(e) => setNewFlowWorkspace(e.target.value)}
                    >
                      {workspaces.map((w) => (
                        <option key={w.id} value={w.id}>
                          {w.name} ({w.path})
                        </option>
                      ))}
                    </select>
                  </div>

                  <div className="tf-modal-footer">
                    <button
                      type="button"
                      className="btn btn-secondary"
                      onClick={() => setShowNewFlowModal(false)}
                    >
                      Cancel
                    </button>
                    <button type="submit" className="btn btn-primary">
                      Propose Plan →
                    </button>
                  </div>
                </form>
              </>
            ) : (
              <>
                <h3 className="tf-modal-heading">Proposed Plan</h3>
                <p className="tf-modal-lead">
                  GravityClaw decomposed your objective into {proposedPlan.length} structured tasks:
                </p>
                <div className="tf-proposed-plan-list">
                  {proposedPlan.map((p, idx) => (
                    <div key={idx} className="tf-proposed-item">
                      <span className="tf-item-num">{idx + 1}.</span>
                      <div className="tf-item-content">
                        <strong>{p.title}</strong>
                        <div className="tf-item-criteria">
                          {p.criteria.map((c, ci) => (
                            <span key={ci}>✓ {c}</span>
                          ))}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>

                <div className="tf-modal-footer">
                  <button
                    type="button"
                    className="btn btn-secondary"
                    onClick={() => setPlanStage("input")}
                  >
                    Edit Objective
                  </button>
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={handleCreateFlowWithPlan}
                  >
                    Start Flow
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* ── New Task Modal ── */}
      {showNewTaskModal && activeFlow && (
        <div className="tf-modal-scrim" onClick={() => setShowNewTaskModal(false)}>
          <div className="tf-modal-sheet" onClick={(e) => e.stopPropagation()}>
            <h3 className="tf-modal-heading">Add Task</h3>
            <form onSubmit={handleCreateTask}>
              <div className="tf-form-field">
                <label>Title</label>
                <input
                  type="text"
                  className="tf-form-input"
                  placeholder="e.g. Run database migration tests"
                  value={newTaskTitle}
                  onChange={(e) => setNewTaskTitle(e.target.value)}
                  required
                  autoFocus
                />
              </div>

              <div className="tf-form-field">
                <label>Objective & Instructions</label>
                <textarea
                  className="tf-form-textarea"
                  placeholder="Detailed work items for AGY worker…"
                  value={newTaskBody}
                  onChange={(e) => setNewTaskBody(e.target.value)}
                  rows={2}
                />
              </div>

              <div className="tf-form-row">
                <div className="tf-form-field">
                  <label>Priority</label>
                  <select
                    className="tf-select-control"
                    value={newTaskPriority}
                    onChange={(e) => setNewTaskPriority(e.target.value as TaskPriority)}
                  >
                    <option value="LOW">Low</option>
                    <option value="MEDIUM">Medium</option>
                    <option value="HIGH">High</option>
                    <option value="URGENT">Urgent</option>
                  </select>
                </div>

                <div className="tf-form-field">
                  <label>Assignee</label>
                  <input
                    type="text"
                    className="tf-form-input"
                    value={newTaskAssignee}
                    onChange={(e) => setNewTaskAssignee(e.target.value)}
                  />
                </div>
              </div>

              {/* Upstream Dependencies */}
              <div className="tf-form-field">
                <label>Depends On (Parent Tasks)</label>
                <div className="tf-dep-checkboxes">
                  {tasks.map((t) => (
                    <label key={t.id} className="tf-checkbox-row">
                      <input
                        type="checkbox"
                        checked={newTaskParentIds.includes(t.id)}
                        onChange={(e) => {
                          if (e.target.checked) {
                            setNewTaskParentIds([...newTaskParentIds, t.id]);
                          } else {
                            setNewTaskParentIds(newTaskParentIds.filter((id) => id !== t.id));
                          }
                        }}
                      />
                      <span>{t.title} ({t.status})</span>
                    </label>
                  ))}
                  {tasks.length === 0 && <span className="text-muted small">No other tasks to depend on.</span>}
                </div>
              </div>

              <div className="tf-modal-footer">
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={() => setShowNewTaskModal(false)}
                >
                  Cancel
                </button>
                <button type="submit" className="btn btn-primary">
                  Create Task
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* ── Block Task Modal ── */}
      {showBlockModal && selectedTask && (
        <div className="tf-modal-scrim" onClick={() => setShowBlockModal(false)}>
          <div className="tf-modal-sheet" onClick={(e) => e.stopPropagation()}>
            <h3 className="tf-modal-heading">Block Task</h3>
            <div className="tf-form-field">
              <label>Reason</label>
              <select
                className="tf-select-control"
                value={blockReason}
                onChange={(e) => setBlockReason(e.target.value as BlockReason)}
              >
                {Object.entries(BLOCK_REASON_LABELS).map(([k, v]) => (
                  <option key={k} value={k}>
                    {v}
                  </option>
                ))}
              </select>
            </div>
            <div className="tf-form-field">
              <label>Details / What is needed?</label>
              <textarea
                className="tf-form-textarea"
                placeholder="e.g. Which hostname or credentials should be used?"
                value={blockDetail}
                onChange={(e) => setBlockDetail(e.target.value)}
                rows={3}
              />
            </div>
            <div className="tf-modal-footer">
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => setShowBlockModal(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-danger"
                onClick={handleBlockTask}
              >
                Confirm Block
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Unblock / Respond Modal ── */}
      {showUnblockModal && selectedTask && (
        <div className="tf-modal-scrim" onClick={() => setShowUnblockModal(false)}>
          <div className="tf-modal-sheet" onClick={(e) => e.stopPropagation()}>
            <h3 className="tf-modal-heading">Respond & Unblock</h3>
            <p className="tf-modal-lead">
              Provide the required input to resume this task:
            </p>
            <div className="tf-form-field">
              <textarea
                className="tf-form-textarea"
                placeholder="Enter response, instructions, or resolution…"
                value={unblockComment}
                onChange={(e) => setUnblockComment(e.target.value)}
                rows={3}
                autoFocus
              />
            </div>
            <div className="tf-modal-footer">
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => setShowUnblockModal(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={handleUnblockTask}
              >
                Resume Task
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Manual Override Complete Modal ── */}
      {showOverrideModal && (
        <div className="tf-modal-scrim" onClick={() => setShowOverrideModal(false)}>
          <div className="tf-modal-sheet" onClick={(e) => e.stopPropagation()}>
            <h3 className="tf-modal-heading">Mark Task Complete?</h3>
            <p className="tf-modal-lead">
              Acceptance criteria remain pending. Force complete will be logged in the audit trail.
            </p>
            <div className="tf-form-field">
              <label>Override Reason</label>
              <input
                type="text"
                className="tf-form-input"
                placeholder="e.g. Manually verified staging release"
                value={overrideReason}
                onChange={(e) => setOverrideReason(e.target.value)}
                autoFocus
              />
            </div>
            <div className="tf-modal-footer">
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => setShowOverrideModal(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={handleConfirmOverrideDone}
              >
                Force Complete
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

// ─── Native Task Card Component ─────────────────────────────────────────────

interface TaskCardProps {
  task: FlowTask;
  onSelect: () => void;
  onRespondBlocked?: () => void;
  onRetry?: () => void;
}

const TaskCard: React.FC<TaskCardProps> = ({
  task,
  onSelect,
  onRespondBlocked,
}) => {
  const isRunning = task.status === "RUNNING";
  const isBlocked = task.status === "BLOCKED";
  const isDone = task.status === "DONE";
  const depsCount = task.parent_ids?.length || 0;

  const handleDragStart = (e: React.DragEvent) => {
    e.dataTransfer.setData("text/plain", task.id);
  };

  return (
    <div
      className={`tf-card tf-card-compact tf-card-${task.status.toLowerCase()} ${isBlocked ? "is-blocked" : ""} ${isRunning ? "is-running" : ""}`}
      onClick={onSelect}
      draggable
      onDragStart={handleDragStart}
    >
      <div className="tf-card-header">
        <h4 className="tf-card-title">{task.title}</h4>
        <span className="tf-card-dots">⋮</span>
      </div>

      {/* Live running activity indicator */}
      {isRunning && (
        <div className="tf-card-live-row">
          <span className="tf-live-dot" />
          <span className="tf-live-text">Running · 08:42</span>
          <span className="tf-live-subagent">↳ 1 subagent</span>
        </div>
      )}

      {/* Blocked alert */}
      {isBlocked && (
        <div className="tf-card-blocked-row">
          <span className="tf-blocked-chip">⚠ Blocked</span>
          <span className="tf-blocked-detail">{task.block_detail || "Needs input"}</span>
          <button
            className="tf-card-respond-btn"
            onClick={(e) => {
              e.stopPropagation();
              if (onRespondBlocked) onRespondBlocked();
            }}
          >
            Respond
          </button>
        </div>
      )}

      {/* Compact Metadata Row */}
      <div className="tf-card-meta-row">
        <span className={`tf-priority-badge priority-${task.priority.toLowerCase()}`}>
          {task.priority}
        </span>
        <span className="tf-meta-dot">·</span>
        <span className="tf-meta-worker">gravityclaw</span>
        {depsCount > 0 && (
          <>
            <span className="tf-meta-dot">·</span>
            <span className="tf-meta-deps">{depsCount} dep{depsCount > 1 ? "s" : ""}</span>
          </>
        )}
        {isDone && (
          <>
            <span className="tf-meta-dot">·</span>
            <span className="tf-meta-done">✓ Done</span>
          </>
        )}
      </div>
    </div>
  );
};

// ─── Timeline View Component ────────────────────────────────────────────────

interface TimelineViewProps {
  tasks: FlowTask[];
  onSelectTask: (task: FlowTask) => void;
}

const TimelineView: React.FC<TimelineViewProps> = ({ tasks, onSelectTask }) => {
  return (
    <div className="tf-timeline-canvas">
      <div className="tf-timeline-date-group">
        <div className="tf-timeline-date-header">Timeline</div>
        <div className="tf-timeline-events-list">
          {tasks.map((task) => {
            const isDone = task.status === "DONE";
            const isRunning = task.status === "RUNNING";
            const isBlocked = task.status === "BLOCKED";
            return (
              <div
                key={task.id}
                className="tf-tline-event"
                onClick={() => onSelectTask(task)}
                style={{ cursor: "pointer" }}
              >
                <span className="tf-tline-time">
                  {new Date(task.updated_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                </span>
                <div
                  className={`tf-tline-marker ${
                    isDone
                      ? "completed"
                      : isRunning
                      ? "active"
                      : isBlocked
                      ? "blocked"
                      : "ready"
                  }`}
                />
                <div className="tf-tline-body">
                  <strong>{task.title}</strong>
                  <p>
                    {isBlocked
                      ? `Blocked: ${task.block_detail || "Needs input"}`
                      : isRunning
                      ? "In progress with AGY worker"
                      : isDone
                      ? "Completed successfully"
                      : `${task.status} · Priority: ${task.priority}`}
                  </p>
                </div>
              </div>
            );
          })}
          {tasks.length === 0 && (
            <div className="text-muted small">No tasks registered in this flow yet.</div>
          )}
        </div>
      </div>
    </div>
  );
};

// ─── Dependencies DAG View Component ────────────────────────────────────────

interface DependenciesDAGViewProps {
  tasks: FlowTask[];
  onSelectTask: (task: FlowTask) => void;
}

const DependenciesDAGView: React.FC<DependenciesDAGViewProps> = ({ tasks, onSelectTask }) => {
  return (
    <div className="tf-dag-canvas">
      <div className="tf-dag-note">
        Click any task node to inspect dependencies and execution details.
      </div>
      <div className="tf-dag-nodes-flow">
        {tasks.map((task) => (
          <div
            key={task.id}
            className={`tf-dag-node node-${task.status.toLowerCase()}`}
            onClick={() => onSelectTask(task)}
          >
            <div className="tf-dag-node-status">
              {task.status === "DONE" ? "✓" : task.status === "RUNNING" ? "◉" : "◻"}
            </div>
            <div className="tf-dag-node-content">
              <strong>{task.title}</strong>
              <small>{task.status} · {task.priority}</small>
            </div>
            {task.child_ids?.length > 0 && (
              <div className="tf-dag-arrow">──►</div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
};

// ─── Activity Feed View Component ───────────────────────────────────────────

interface ActivityFeedViewProps {
  tasks: FlowTask[];
  onSelectTask: (task: FlowTask) => void;
  onNavigateToRun?: (runId: string) => void;
}

const ActivityFeedView: React.FC<ActivityFeedViewProps> = ({ tasks, onSelectTask }) => {
  return (
    <div className="tf-activity-feed-canvas">
      <div className="tf-activity-feed-list">
        {tasks.map((t) => (
          <div
            key={t.id}
            className="tf-feed-card"
            onClick={() => onSelectTask(t)}
          >
            <div className="tf-feed-card-header">
              <span className={`tf-insp-status status-${t.status.toLowerCase()}`}>
                ● {t.status}
              </span>
              <strong>{t.title}</strong>
            </div>
            <p className="tf-feed-card-body">{t.body || "No description."}</p>
            <div className="tf-feed-card-meta">
              <span>Updated: {new Date(t.updated_at).toLocaleTimeString()}</span>
              <span>{t.parent_ids?.length || 0} parents · {t.child_ids?.length || 0} children</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};
