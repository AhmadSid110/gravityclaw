"""Durable TaskFlow / Kanban Orchestration Layer for GravityClaw.

TaskFlow orchestrates multi-task projects and long-running objectives above
the job manager and AGY runtime. Every task execution maps to a genuine
GravityClaw Run with full token accounting, isolation, artifacts, and timeline.

Features:
- Two-level hierarchy: TaskFlow (objective) -> FlowTask (durable cards).
- Deterministic DAG dependencies with cycle detection & auto-promotion.
- Task comments as durable handoff protocol across worker runs.
- Atomic lease claiming and heartbeat tracking.
- Recurrence guard on task blocking to avoid autonomous loops.
- Idempotent task creation for automation/webhook retries.
- TaskFlow Dispatcher for automatic reconciliation, claiming, and dispatching.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping, Sequence

from .context import PROFILES
from .manager import RunManager
from .store import (
    BLOCK_REASONS,
    FLOW_STATUSES,
    TASK_PRIORITIES,
    TASK_STATUSES,
    TERMINAL_FLOW_STATUSES,
    TERMINAL_RUN_STATUSES,
    TERMINAL_TASK_STATUSES,
    FlowTaskRecord,
    RunRecord,
    Store,
    TaskAttemptRecord,
    TaskClaimRecord,
    TaskCommentRecord,
    TaskFlowRecord,
    VersionConflict,
    utc_now,
)


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TaskResultContract:
    task_outcome: str  # "completed", "blocked", "failed"
    summary: str
    artifacts: list[str] = field(default_factory=list)
    block_reason: str | None = None
    block_detail: str | None = None
    new_tasks: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskResultContract":
        outcome = str(data.get("task_outcome", "completed")).lower().strip()
        if outcome not in {"completed", "blocked", "failed"}:
            outcome = "completed"
        reason = data.get("block_reason")
        if reason and str(reason).lower().strip() in BLOCK_REASONS:
            reason = str(reason).lower().strip()
        else:
            reason = None
        artifacts = [str(a) for a in data.get("artifacts", []) if a]
        new_tasks = [t for t in data.get("new_tasks", []) if isinstance(t, dict)]
        return cls(
            task_outcome=outcome,
            summary=str(data.get("summary", "")).strip(),
            artifacts=artifacts,
            block_reason=reason,
            block_detail=str(data.get("block_detail", "")).strip() or None,
            new_tasks=new_tasks,
        )


@dataclass(frozen=True, slots=True)
class DispatcherReport:
    reconciled_claims: int = 0
    promoted_tasks: int = 0
    dispatched_tasks: int = 0
    retried_tasks: int = 0
    completed_tasks: int = 0
    blocked_tasks: int = 0
    failed_tasks: int = 0
    updated_flows: int = 0


def detect_dag_cycle(
    existing_edges: Sequence[tuple[str, str]], new_parent: str, new_child: str
) -> bool:
    """Return True if adding edge (new_parent -> new_child) would introduce a cycle."""
    if new_parent == new_child:
        return True

    # Graph adjacency: parent -> list of children
    adj: dict[str, list[str]] = defaultdict(list)
    for p, c in existing_edges:
        adj[p].append(c)
    adj[new_parent].append(new_child)

    # Check reachable from new_child back to new_parent (BFS)
    queue = deque([new_child])
    visited = {new_child}
    while queue:
        curr = queue.popleft()
        if curr == new_parent:
            return True
        for nxt in adj.get(curr, []):
            if nxt not in visited:
                visited.add(nxt)
                queue.append(nxt)
    return False


def build_taskflow_worker_prompt(
    task: FlowTaskRecord,
    flow: TaskFlowRecord,
    parent_handoffs: Sequence[tuple[FlowTaskRecord, Sequence[TaskCommentRecord]]],
    previous_attempts: Sequence[TaskAttemptRecord],
) -> str:
    """Format an execution prompt for a TASKFLOW_WORKER run."""
    acceptance_lines: list[str] = []
    if isinstance(task.acceptance_json, list):
        for item in task.acceptance_json:
            if isinstance(item, dict):
                text = item.get("text") or item.get("criterion") or str(item)
                acceptance_lines.append(f"- [ ] {text}")
            else:
                acceptance_lines.append(f"- [ ] {item}")

    handoff_sections: list[str] = []
    for parent_task, comments in parent_handoffs:
        comment_texts = [f"  > ({c.author_type}) {c.body}" for c in comments[-3:]]
        handoff_sections.append(
            f"### Parent Task: {parent_task.title} (`{parent_task.id}`)\n"
            f"Status: {parent_task.status}\n"
            + ("Recent Handoff Comments:\n" + "\n".join(comment_texts) if comment_texts else "No handoff notes.")
        )

    attempt_lines: list[str] = []
    for att in previous_attempts:
        attempt_lines.append(
            f"- Attempt {att.attempt_no} ({att.outcome or 'failed'}): {att.summary or 'No summary recorded.'}"
        )

    sections = [
        f"# TASKFLOW WORKER: {task.title}",
        f"**Flow Objective**: {flow.objective} (Flow `{flow.id}`)",
        f"**Task ID**: `{task.id}` | **Priority**: {task.priority} | **Assignee**: {task.assignee_profile}",
        "",
        "## Task Description",
        task.body,
    ]

    if acceptance_lines:
        sections.extend([
            "",
            "## Acceptance Criteria",
            "\n".join(acceptance_lines),
        ])

    if handoff_sections:
        sections.extend([
            "",
            "## Parent Task Handoffs & Upstream Context",
            "\n\n".join(handoff_sections),
        ])

    if attempt_lines:
        sections.extend([
            "",
            "## Previous Attempt History (Retry Context)",
            "\n".join(attempt_lines),
        ])

    sections.extend([
        "",
        "## Output Contract & Result Reporting",
        "When your work is complete, provide a structured conclusion or JSON artifact with:",
        "```json",
        "{",
        '  "task_outcome": "completed", // or "blocked" or "failed"',
        '  "summary": "Brief summary of what was accomplished and handoff details.",',
        '  "artifacts": ["path/to/generated/artifact.md"],',
        '  "block_reason": "needs_user_input", // only if task_outcome is "blocked"',
        '  "block_detail": "Explanation of what is blocking progress" // only if blocked',
        "}",
        "```",
        "Focus on executing the task acceptance criteria with high accuracy.",
    ])

    return "\n".join(sections)


class TaskFlowService:
    """High-level orchestration for project flows, durable tasks, and handoffs."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def create_flow(
        self,
        title: str,
        objective: str,
        workspace_id: str,
        *,
        context_profile: str = "TASKFLOW_WORKER",
        state_json: dict[str, Any] | None = None,
        flow_id: str | None = None,
    ) -> TaskFlowRecord:
        title = title.strip()
        objective = objective.strip()
        if not title:
            raise ValueError("flow title must not be empty")
        if not objective:
            raise ValueError("flow objective must not be empty")
        return self.store.create_task_flow(
            title=title,
            objective=objective,
            workspace_id=workspace_id,
            context_profile=context_profile,
            state_json=state_json,
            flow_id=flow_id,
        )

    def get_flow(self, flow_id: str) -> TaskFlowRecord | None:
        return self.store.get_task_flow(flow_id)

    def list_flows(
        self, *, workspace_id: str | None = None, status: str | None = None
    ) -> list[TaskFlowRecord]:
        return self.store.list_task_flows(workspace_id=workspace_id, status=status)

    def update_flow_status(
        self,
        flow_id: str,
        status: str,
        *,
        expected_version: int | None = None,
        state_json: dict[str, Any] | None = None,
    ) -> TaskFlowRecord:
        status = status.upper().strip()
        if status not in FLOW_STATUSES:
            raise ValueError(f"invalid flow status: {status}")
        return self.store.update_task_flow_status(
            flow_id, status, expected_version=expected_version, state_json=state_json
        )

    def delete_flow(self, flow_id: str) -> bool:
        return self.store.delete_task_flow(flow_id)

    def create_task(
        self,
        flow_id: str,
        title: str,
        body: str,
        workspace_id: str,
        *,
        acceptance_criteria: list[dict[str, Any]] | list[str] | None = None,
        priority: str = "MEDIUM",
        assignee_profile: str = "default",
        idempotency_key: str | None = None,
        max_attempts: int = 3,
        parent_ids: Sequence[str] | None = None,
        task_id: str | None = None,
    ) -> FlowTaskRecord:
        title = title.strip()
        if not title:
            raise ValueError("task title must not be empty")
        priority = priority.upper().strip()
        if priority not in TASK_PRIORITIES:
            raise ValueError(f"invalid task priority: {priority}")

        # Idempotency check
        if idempotency_key:
            existing = self.store.find_flow_task_by_idempotency(flow_id, idempotency_key)
            if existing is not None:
                return existing

        # Cycle check if parents are provided
        parent_ids = list(parent_ids or [])
        if parent_ids and task_id:
            edges = self.store.get_all_flow_dependencies(flow_id)
            for p_id in parent_ids:
                if detect_dag_cycle(edges, p_id, task_id):
                    raise ValueError(f"adding dependency {p_id} -> {task_id} would cause a cycle")

        task = self.store.create_flow_task(
            flow_id=flow_id,
            title=title,
            body=body,
            workspace_id=workspace_id,
            acceptance_criteria=acceptance_criteria,
            priority=priority,
            assignee_profile=assignee_profile,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            parent_ids=parent_ids,
            task_id=task_id,
        )

        # Evaluate if the newly created task is immediately READY (no parents or all parents DONE)
        self.evaluate_task_promotion(task.id)
        return self.store.get_flow_task(task.id) or task

    def get_task(self, task_id: str) -> FlowTaskRecord | None:
        return self.store.get_flow_task(task_id)

    def list_tasks(
        self, *, flow_id: str | None = None, status: str | None = None
    ) -> list[FlowTaskRecord]:
        return self.store.list_flow_tasks(flow_id=flow_id, status=status)

    def add_dependency(self, parent_task_id: str, child_task_id: str) -> None:
        if parent_task_id == child_task_id:
            raise ValueError("task cannot depend on itself")
        parent = self.store.get_flow_task(parent_task_id)
        child = self.store.get_flow_task(child_task_id)
        if not parent or not child:
            raise ValueError("parent and child tasks must both exist")
        if parent.flow_id != child.flow_id:
            raise ValueError("dependencies must be within the same flow")

        edges = self.store.get_all_flow_dependencies(parent.flow_id)
        if detect_dag_cycle(edges, parent_task_id, child_task_id):
            raise ValueError(f"dependency {parent_task_id} -> {child_task_id} creates a cycle")

        self.store.add_task_dependency(parent_task_id, child_task_id)
        self.evaluate_task_promotion(child_task_id)

    def remove_dependency(self, parent_task_id: str, child_task_id: str) -> None:
        self.store.remove_task_dependency(parent_task_id, child_task_id)
        self.evaluate_task_promotion(child_task_id)

    def block_task(
        self,
        task_id: str,
        reason: str,
        detail: str | None = None,
        *,
        author_type: str = "system",
        author_id: str = "system",
    ) -> FlowTaskRecord:
        reason = reason.lower().strip()
        if reason not in BLOCK_REASONS:
            raise ValueError(f"invalid block reason: {reason}. Expected one of {BLOCK_REASONS}")

        task = self.store.get_flow_task(task_id)
        if not task:
            raise ValueError(f"task not found: {task_id}")

        # Recurrence guard
        new_recurrence = (task.block_recurrence_count + 1) if task.block_reason == reason else 1
        if new_recurrence >= 3:
            # Escalate to TRIAGE to prevent endless autonomous loops
            updated = self.store.update_flow_task_status(
                task_id,
                "TRIAGE",
                block_reason=reason,
                block_detail=detail,
                increment_block_recurrence=True,
            )
            self.store.add_task_comment(
                task_id,
                "system",
                "recurrence_guard",
                f"[RECURRENCE GUARD] Task repeatedly blocked for '{reason}' ({new_recurrence} times). Escalated to TRIAGE for manual intervention.",
            )
            return updated

        updated = self.store.update_flow_task_status(
            task_id,
            "BLOCKED",
            block_reason=reason,
            block_detail=detail,
            increment_block_recurrence=True,
        )
        if detail:
            self.store.add_task_comment(
                task_id, author_type, author_id, f"Blocked ({reason}): {detail}"
            )
        return updated

    def unblock_task(
        self,
        task_id: str,
        *,
        comment: str | None = None,
        author_type: str = "user",
        author_id: str = "user",
    ) -> FlowTaskRecord:
        task = self.store.get_flow_task(task_id)
        if not task:
            raise ValueError(f"task not found: {task_id}")

        if comment:
            self.store.add_task_comment(
                task_id, author_type, author_id, f"Unblocked: {comment}"
            )

        # Check if parents are done to determine if it should become READY or TODO
        parents_done = True
        for p_id in task.parent_ids:
            p = self.store.get_flow_task(p_id)
            if not p or p.status != "DONE":
                parents_done = False
                break

        target_status = "READY" if parents_done else "TODO"
        return self.store.update_flow_task_status(
            task_id, target_status, block_reason=task.block_reason, block_detail=None
        )

    def retry_task(
        self,
        task_id: str,
        *,
        comment: str | None = None,
        author_type: str = "user",
        author_id: str = "user",
    ) -> FlowTaskRecord:
        task = self.store.get_flow_task(task_id)
        if not task:
            raise ValueError(f"task not found: {task_id}")

        self.store.release_task_claim(task_id)
        if comment:
            self.store.add_task_comment(
                task_id, author_type, author_id, f"Manual retry initiated: {comment}"
            )

        return self.store.update_flow_task_status(
            task_id, "READY", block_reason=None, block_detail=None
        )

    def evaluate_task_promotion(self, task_id: str) -> bool:
        """Promote a task from TODO/TRIAGE to READY if all its parent dependencies are DONE."""
        task = self.store.get_flow_task(task_id)
        if not task or task.status not in {"TODO", "TRIAGE"}:
            return False

        if not task.parent_ids:
            if task.status == "TODO":
                self.store.update_flow_task_status(task_id, "READY")
                return True
            return False

        for p_id in task.parent_ids:
            parent = self.store.get_flow_task(p_id)
            if not parent or parent.status != "DONE":
                return False

        self.store.update_flow_task_status(task_id, "READY")
        return True

    def auto_promote_flow(self, flow_id: str) -> int:
        """Check all tasks in a flow and promote eligible TODO tasks to READY."""
        tasks = self.store.list_flow_tasks(flow_id=flow_id)
        promoted = 0
        for task in tasks:
            if task.status == "TODO" and self.evaluate_task_promotion(task.id):
                promoted += 1
        return promoted

    def add_comment(
        self,
        task_id: str,
        author_type: str,
        author_id: str,
        body: str,
    ) -> TaskCommentRecord:
        body = body.strip()
        if not body:
            raise ValueError("comment body must not be empty")
        return self.store.add_task_comment(task_id, author_type, author_id, body)

    def get_task_handoffs(
        self, task_id: str
    ) -> list[tuple[FlowTaskRecord, list[TaskCommentRecord]]]:
        """Gather parent tasks and their comments to supply as durable handoffs."""
        task = self.store.get_flow_task(task_id)
        if not task:
            return []
        handoffs: list[tuple[FlowTaskRecord, list[TaskCommentRecord]]] = []
        for p_id in task.parent_ids:
            parent = self.store.get_flow_task(p_id)
            if parent:
                comments = self.store.list_task_comments(p_id)
                handoffs.append((parent, comments))
        return handoffs


class TaskFlowDispatcher:
    """Autonomous dispatcher that claims READY tasks, orchestrates Runs, and manages leases."""

    def __init__(
        self,
        store: Store,
        manager: RunManager,
        service: TaskFlowService,
        *,
        poll_interval: float = 1.0,
        lease_seconds: int = 300,
        max_concurrent_workers: int = 4,
        owner: str | None = None,
    ) -> None:
        self.store = store
        self.manager = manager
        self.service = service
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self.max_concurrent_workers = max_concurrent_workers
        self.owner = owner or f"taskflow_dispatcher:{uuid.uuid4().hex[:8]}"
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop(), name="taskflow-dispatcher")

    async def close(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run_loop(self) -> None:
        LOGGER.info("TaskFlow Dispatcher started (owner=%s)", self.owner)
        while not self._stopping:
            try:
                await self.tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                LOGGER.exception("Error in TaskFlow dispatcher tick: %s", exc)
            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break

    async def tick(self) -> DispatcherReport:
        """Single reconciliation, promotion, claim, and dispatch cycle."""
        # 1. Reconcile running tasks & active claims against actual Run states
        reconciled, completed, retried, blocked, failed = await self._reconcile_running_tasks()

        # 2. Auto-promote TODO tasks whose dependencies are satisfied
        promoted = self._promote_all_eligible_tasks()

        # 3. Dispatch READY tasks up to concurrency limits
        dispatched = await self._dispatch_ready_tasks()

        # 4. Evaluate and update overall Flow statuses
        updated_flows = self._evaluate_all_flow_statuses()

        return DispatcherReport(
            reconciled_claims=reconciled,
            promoted_tasks=promoted,
            dispatched_tasks=dispatched,
            retried_tasks=retried,
            completed_tasks=completed,
            blocked_tasks=blocked,
            failed_tasks=failed,
            updated_flows=updated_flows,
        )

    async def _reconcile_running_tasks(
        self,
    ) -> tuple[int, int, int, int, int]:
        """Check all RUNNING tasks, evaluate completed runs, and handle retries or timeouts."""
        reconciled = 0
        completed_count = 0
        retried_count = 0
        blocked_count = 0
        failed_count = 0

        running_tasks = self.store.list_flow_tasks(status="RUNNING")
        for task in running_tasks:
            attempts = self.store.list_task_attempts(task.id)
            if not attempts:
                # No attempt registered yet, check claim lease
                claim = self.store.get_task_claim(task.id)
                if claim and claim.lease_until < utc_now():
                    self.store.release_task_claim(task.id)
                    self.store.update_flow_task_status(task.id, "READY")
                    reconciled += 1
                continue

            latest_attempt = attempts[-1]
            run = self.store.get_run(latest_attempt.run_id)
            if not run:
                continue

            if run.status in TERMINAL_RUN_STATUSES:
                reconciled += 1
                if run.status == "completed":
                    # Inspect result contract from events / artifacts
                    contract = self._extract_result_contract(run)
                    if contract and contract.task_outcome == "blocked":
                        blocked_count += 1
                        self.store.update_task_attempt(
                            latest_attempt.id,
                            finished_at=run.finished_at or utc_now(),
                            outcome="BLOCKED",
                            summary=contract.summary or "Task reported blocked",
                        )
                        self.store.release_task_claim(task.id)
                        self.service.block_task(
                            task.id,
                            contract.block_reason or "needs_user_input",
                            contract.block_detail or contract.summary,
                            author_type="agent",
                            author_id=run.id,
                        )
                    elif contract and contract.task_outcome == "failed":
                        # Worker explicitly reported failure
                        failed_count += 1
                        self.store.update_task_attempt(
                            latest_attempt.id,
                            finished_at=run.finished_at or utc_now(),
                            outcome="FAILED",
                            summary=contract.summary or "Worker reported failure",
                        )
                        self._handle_task_failure(task, latest_attempt, run.error or contract.summary)
                    else:
                        # Task completed successfully!
                        completed_count += 1
                        summary = contract.summary if contract else "Task completed successfully"
                        self.store.update_task_attempt(
                            latest_attempt.id,
                            finished_at=run.finished_at or utc_now(),
                            outcome="COMPLETED",
                            summary=summary,
                        )
                        # Record handoff comment
                        if summary:
                            self.store.add_task_comment(
                                task.id, "agent", run.id, f"Completed: {summary}"
                            )
                        self.store.release_task_claim(task.id)
                        self.store.update_flow_task_status(task.id, "DONE")
                        # Auto-promote downstream tasks
                        self.service.auto_promote_flow(task.flow_id)
                else:
                    # Run failed, cancelled, or interrupted
                    outcome = run.status.upper()
                    self.store.update_task_attempt(
                        latest_attempt.id,
                        finished_at=run.finished_at or utc_now(),
                        outcome=outcome,
                        summary=run.error or f"Run ended with status {run.status}",
                    )
                    if self._handle_task_failure(task, latest_attempt, run.error):
                        retried_count += 1
                    else:
                        failed_count += 1

            else:
                # Run is still queued or running — check heartbeat and lease extension
                claim = self.store.get_task_claim(task.id)
                if claim:
                    # Extend heartbeat/lease if run is active
                    self.store.heartbeat_task_claim(
                        task.id, self.owner, message=f"Run {run.id} ({run.status})", extend_seconds=self.lease_seconds
                    )

        return reconciled, completed_count, retried_count, blocked_count, failed_count

    def _handle_task_failure(
        self, task: FlowTaskRecord, latest_attempt: TaskAttemptRecord, error_msg: str | None
    ) -> bool:
        """Handle attempt failure: retry if attempts < max_attempts, else mark FAILED."""
        attempts = self.store.list_task_attempts(task.id)
        attempt_count = len(attempts)
        self.store.release_task_claim(task.id)

        if attempt_count < task.max_attempts:
            # Re-enqueue to READY
            self.store.update_flow_task_status(task.id, "READY")
            self.store.add_task_comment(
                task.id,
                "system",
                "dispatcher",
                f"Attempt {attempt_count}/{task.max_attempts} failed: {error_msg or 'Execution error'}. Auto-retrying.",
            )
            return True
        else:
            # Mark FAILED
            self.store.update_flow_task_status(task.id, "FAILED")
            self.store.add_task_comment(
                task.id,
                "system",
                "dispatcher",
                f"Task failed after {attempt_count} attempts: {error_msg or 'Exceeded retry limit'}.",
            )
            # Dependent tasks may be blocked
            self._handle_parent_task_failed(task)
            return False

    def _handle_parent_task_failed(self, failed_task: FlowTaskRecord) -> None:
        """When a task fails, mark unstarted dependent children as blocked by dependency."""
        flow_tasks = self.store.list_flow_tasks(flow_id=failed_task.flow_id)
        for t in flow_tasks:
            if failed_task.id in t.parent_ids and t.status in {"TODO", "READY"}:
                self.service.block_task(
                    t.id,
                    "dependency",
                    f"Parent task '{failed_task.title}' ({failed_task.id}) failed.",
                    author_type="system",
                    author_id="dispatcher",
                )

    def _promote_all_eligible_tasks(self) -> int:
        """Promote TODO tasks to READY across all active flows."""
        active_flows = self.store.list_task_flows()
        total_promoted = 0
        for flow in active_flows:
            if flow.status in {"RUNNING", "QUEUED", "WAITING", "BLOCKED"}:
                total_promoted += self.service.auto_promote_flow(flow.id)
        return total_promoted

    async def _dispatch_ready_tasks(self) -> int:
        """Claim READY tasks and dispatch them as Runs."""
        running_tasks = self.store.list_flow_tasks(status="RUNNING")
        available_slots = max(0, self.max_concurrent_workers - len(running_tasks))
        if available_slots <= 0:
            return 0

        claimed_tasks = self.store.claim_ready_tasks(
            owner=self.owner, limit=available_slots, lease_seconds=self.lease_seconds
        )
        if not claimed_tasks:
            return 0

        dispatched = 0
        for task in claimed_tasks:
            try:
                await self._spawn_task_run(task)
                dispatched += 1
            except Exception as exc:
                LOGGER.exception("Failed to dispatch task run for %s: %s", task.id, exc)
                # Release claim and leave in READY
                self.store.release_task_claim(task.id)
                self.store.update_flow_task_status(task.id, "READY")

        return dispatched

    async def _spawn_task_run(self, task: FlowTaskRecord) -> RunRecord:
        """Build the TASKFLOW_WORKER prompt and submit a Run."""
        flow = self.store.get_task_flow(task.flow_id)
        if not flow:
            raise ValueError(f"flow not found: {task.flow_id}")

        parent_handoffs = self.service.get_task_handoffs(task.id)
        previous_attempts = self.store.list_task_attempts(task.id)

        prompt = build_taskflow_worker_prompt(
            task=task,
            flow=flow,
            parent_handoffs=parent_handoffs,
            previous_attempts=previous_attempts,
        )

        # Create or reuse a dedicated conversation for this task
        channel_key = f"taskflow:{task.flow_id}:{task.id}"
        conversation = self.store.get_conversation_by_channel_key("taskflow", channel_key)
        if not conversation:
            conversation = self.store.create_conversation(
                workspace_id=task.workspace_id,
                channel="taskflow",
                channel_key=channel_key,
                title=f"Task: {task.title}",
            )

        attempt_no = len(previous_attempts) + 1
        request_payload = {
            "prompt": prompt,
            "context_profile": "TASKFLOW_WORKER",
            "task_id": task.id,
            "flow_id": task.flow_id,
            "attempt_no": attempt_no,
        }

        run = await self.manager.submit(conversation.id, request_payload)

        self.store.record_task_attempt(
            task_id=task.id,
            run_id=run.id,
            attempt_no=attempt_no,
            started_at=utc_now(),
        )

        # Make sure the parent flow is in RUNNING state
        if flow.status in {"QUEUED", "WAITING", "BLOCKED"}:
            self.store.update_task_flow_status(flow.id, "RUNNING")

        return run

    def _extract_result_contract(self, run: RunRecord) -> TaskResultContract | None:
        """Extract structured result contract from run events or assistant messages."""
        events = self.store.list_events(run.id)

        # 1. Check for artifact creation events or assistant response text
        for event in reversed(events):
            if event.event_type == "agent.completed":
                resp = event.payload.get("response") or ""
                contract = self._try_parse_contract(resp)
                if contract:
                    return contract
            elif event.event_type == "tool.completed":
                output = event.payload.get("output") or ""
                contract = self._try_parse_contract(str(output))
                if contract:
                    return contract

        # 2. Check conversation messages
        messages = self.store.list_messages(run.conversation_id)
        for msg in reversed(messages):
            if msg.role == "assistant" and msg.source_run_id == run.id:
                contract = self._try_parse_contract(msg.content)
                if contract:
                    return contract

        return None

    def _try_parse_contract(self, text: str) -> TaskResultContract | None:
        """Search text for JSON block with task_outcome."""
        if not text or "task_outcome" not in text:
            return None

        # Look for code block ```json ... ```
        if "```" in text:
            parts = text.split("```")
            for i in range(1, len(parts), 2):
                block = parts[i]
                if block.startswith("json"):
                    block = block[4:]
                try:
                    data = json.loads(block.strip())
                    if isinstance(data, dict) and "task_outcome" in data:
                        return TaskResultContract.from_dict(data)
                except Exception:
                    pass

        # Try raw json
        try:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end > start:
                data = json.loads(text[start : end + 1])
                if isinstance(data, dict) and "task_outcome" in data:
                    return TaskResultContract.from_dict(data)
        except Exception:
            pass

        return None

    def _evaluate_all_flow_statuses(self) -> int:
        """Compute and update the holistic status of all active TaskFlows."""
        flows = self.store.list_task_flows()
        updated = 0
        for flow in flows:
            if flow.status in TERMINAL_FLOW_STATUSES:
                continue

            tasks = self.store.list_flow_tasks(flow_id=flow.id)
            if not tasks:
                continue

            all_done = all(t.status in {"DONE", "ARCHIVED"} for t in tasks)
            any_running = any(t.status == "RUNNING" for t in tasks)
            any_ready = any(t.status == "READY" for t in tasks)
            any_blocked = any(t.status == "BLOCKED" for t in tasks)
            any_failed = any(t.status == "FAILED" for t in tasks)
            all_blocked = all(t.status in {"BLOCKED", "DONE", "ARCHIVED"} for t in tasks)

            new_status = flow.status
            if all_done:
                new_status = "SUCCEEDED"
            elif any_running:
                new_status = "RUNNING"
            elif any_ready:
                new_status = "RUNNING"
            elif all_blocked and any_blocked:
                new_status = "BLOCKED"
            elif any_failed and not any_ready and not any_running:
                new_status = "FAILED"
            elif any_blocked:
                new_status = "BLOCKED"
            else:
                new_status = "WAITING"

            if new_status != flow.status:
                self.store.update_task_flow_status(flow.id, new_status)
                updated += 1

        return updated
