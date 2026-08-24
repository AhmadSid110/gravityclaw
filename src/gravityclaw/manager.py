"""Durable job lifecycle, monitoring, cancellation, and reconciliation."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .agy import _normalize_event
from .attachments import AttachmentResolver, AttachmentStorage, AttachmentStore
from .capabilities import CapabilityManager
from .context import RunContextCompiler
from .event_bus import EventBus
from .events import AgentEvent
from .progress_engine import ProgressEngine
from .telemetry import TelemetryEvent
from .execution import (
    ContainerSpec,
    MANAGED_LABEL,
    RUN_LABEL,
    WORKSPACE_LABEL,
    ExecutionBackend,
    SpecFactory,
    WorkerEnvelope,
    WorkerSnapshot,
)
from .goals import GoalEvaluator, build_continuation_prompt
from .learning import LearningEngine
from .curator import MemoryCurator
from .store import GoalRecord, RunRecord, Store, TERMINAL_RUN_STATUSES


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    reattached: int = 0
    finalized: int = 0
    interrupted: int = 0
    orphaned: int = 0
    queued_dispatched: int = 0


class RunManager:
    def __init__(
        self,
        store: Store,
        backend: ExecutionBackend,
        spec_factory: SpecFactory,
        event_bus: EventBus | None = None,
        poll_interval: float = 0.2,
        context_compiler: RunContextCompiler | None = None,
        capability_manager: CapabilityManager | None = None,
        goal_evaluator: GoalEvaluator | None = None,
        learning_engine: LearningEngine | None = None,
        memory_curator: MemoryCurator | None = None,
        attachment_store: AttachmentStore | None = None,
        attachment_resolver: AttachmentResolver | None = None,
        attachment_storage: AttachmentStorage | None = None,
    ) -> None:
        self.store = store
        self.backend = backend
        self.spec_factory = spec_factory
        self.event_bus = event_bus or EventBus()
        self.poll_interval = poll_interval
        self.context_compiler = context_compiler
        self.capability_manager = capability_manager
        self.goal_evaluator = goal_evaluator
        self.learning_engine = learning_engine
        self.memory_curator = memory_curator
        self.attachment_store = attachment_store
        self.attachment_resolver = attachment_resolver or AttachmentResolver()
        self.attachment_storage = attachment_storage
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._dispatch_tasks: set[asyncio.Task[None]] = set()
        self._conversation_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._progress_engines: dict[str, ProgressEngine] = {}
        self._stopping = False

    async def start(self) -> ReconciliationReport:
        self.store.initialize()
        self._stopping = False
        return await self.reconcile()

    async def close(self) -> None:
        """Stop gateway monitors without stopping externally owned workers."""
        self._stopping = True
        tasks = list(self._tasks.values())
        tasks.extend(self._dispatch_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._dispatch_tasks.clear()
        if self.learning_engine is not None:
            await self.learning_engine.close()

    async def submit(self, conversation_id: str, request: dict[str, Any]) -> RunRecord:
        prompt = str(request.get("prompt", "")).strip()
        if not prompt:
            raise ValueError("prompt must not be empty")
        # The user message, queued run, and queued event are one transaction.
        run = self.store.submit_run(conversation_id, request)
        await self.event_bus.notify(run.id)
        self._schedule_dispatch(conversation_id)
        return self.store.get_run(run.id)

    async def activate(self, run_id: str) -> RunRecord:
        """Wake a run atomically ingested by another durable frontend."""
        run = self.store.get_run(run_id)
        await self.event_bus.notify(run.id)
        if run.status == "queued":
            self._schedule_dispatch(run.conversation_id)
        return run

    async def cancel(self, run_id: str) -> RunRecord:
        run = self.store.get_run(run_id)
        if run.status == "queued":
            self.store.append_event(
                run.id,
                AgentEvent("run.cancel_requested", run.id, data={}),
            )
            self.store.transition_run(
                run.id, "cancelled", expected=("queued",), error="cancelled before start"
            )
            await self.event_bus.notify(run.id)
            self._schedule_dispatch(run.conversation_id)
            return self.store.get_run(run.id)
        if run.status != "running":
            return run
        self.store.append_event(
            run.id,
            AgentEvent("run.cancel_requested", run.id, data={}),
        )
        await self.event_bus.notify(run.id)
        if run.worker_id:
            await self.backend.stop(run.worker_id)
            await self._ingest(run)
        self.store.transition_run(
            run.id,
            "cancelled",
            expected=("running",),
            error="cancellation confirmed",
        )
        if run.worker_id:
            self.store.update_worker_state(run.worker_id, "terminated")
        await self.event_bus.notify(run.id)
        self._schedule_dispatch(run.conversation_id)
        return self.store.get_run(run.id)

    async def reconcile(self) -> ReconciliationReport:
        snapshots = await self.backend.list_managed()
        snapshots_by_id = {snapshot.external_id: snapshot for snapshot in snapshots}
        runs = {run.id: run for run in self.store.list_runs()}
        reattached = finalized = interrupted = orphaned = 0

        for snapshot in snapshots:
            run_id = snapshot.labels.get(RUN_LABEL)
            workspace_id = snapshot.labels.get(WORKSPACE_LABEL)
            run = runs.get(run_id or "")
            if (
                snapshot.labels.get(MANAGED_LABEL) != "true"
                or run is None
                or workspace_id is None
            ):
                if snapshot.running:
                    await self.backend.stop(snapshot.external_id)
                self.store.record_worker(
                    snapshot.external_id,
                    run_id=None,
                    workspace_id=None,
                    backend="podman",
                    state="orphaned",
                    metadata={"reason": "unknown managed worker", "labels": snapshot.labels},
                )
                await self.backend.remove(snapshot.external_id)
                orphaned += 1
                continue

            conversation = self.store.get_conversation(run.conversation_id)
            if workspace_id != conversation.workspace_id or (
                run.worker_id is not None and run.worker_id != snapshot.external_id
            ):
                if snapshot.running:
                    await self.backend.stop(snapshot.external_id)
                self.store.record_worker(
                    snapshot.external_id,
                    run_id=run.id,
                    workspace_id=workspace_id,
                    backend=run.backend,
                    state="orphaned",
                    metadata={"reason": "worker identity mismatch"},
                )
                if run.status == "running":
                    self.store.transition_run(
                        run.id,
                        "orphaned",
                        expected=("running",),
                        error="worker identity mismatch during reconciliation",
                    )
                orphaned += 1
                continue

            if run.worker_id is None and run.status == "running":
                self.store.attach_worker(
                    run.id,
                    snapshot.external_id,
                    workspace_id=workspace_id,
                    backend=run.backend,
                    metadata={"recovered": True},
                )
                run = self.store.get_run(run.id)
            self.store.record_worker(
                snapshot.external_id,
                run_id=run.id,
                workspace_id=workspace_id,
                backend=run.backend,
                state="running" if snapshot.running else "exited",
                metadata={"container_name": snapshot.name},
            )

            if run.status in TERMINAL_RUN_STATUSES:
                if snapshot.running:
                    await self.backend.stop(snapshot.external_id)
                    self.store.update_worker_state(snapshot.external_id, "orphaned")
                    orphaned += 1
                continue
            if run.status != "running":
                continue
            await self._ingest(run)
            if snapshot.running:
                self._monitor(run.id)
                reattached += 1
            else:
                await self._finalize(run, snapshot, from_reconciliation=True)
                finalized += 1

        for run in self.store.list_runs(statuses=("running",)):
            if run.worker_id is None or run.worker_id not in snapshots_by_id:
                if self.store.transition_run(
                    run.id,
                    "interrupted",
                    expected=("running",),
                    error="execution disappeared during gateway restart",
                ):
                    interrupted += 1
                    await self.event_bus.notify(run.id)

        dispatched = 0
        for run in self.store.next_queued_runs():
            before = self.store.get_run(run.id).status
            await self._dispatch_conversation(run.conversation_id)
            if before == "queued" and self.store.get_run(run.id).status == "running":
                dispatched += 1
        return ReconciliationReport(
            reattached=reattached,
            finalized=finalized,
            interrupted=interrupted,
            orphaned=orphaned,
            queued_dispatched=dispatched,
        )

    async def _dispatch_conversation(self, conversation_id: str) -> None:
        lock = self._conversation_locks[conversation_id]
        async with lock:
            candidates = [
                run
                for run in self.store.next_queued_runs()
                if run.conversation_id == conversation_id
            ]
            if not candidates:
                return
            claimed = self.store.claim_run(candidates[0].id)
            if claimed is None:
                return
            await self.event_bus.notify(claimed.id)
            conversation = self.store.get_conversation(conversation_id)
            workspace = self.store.get_workspace(conversation.workspace_id)
            try:
                if self.context_compiler is not None:
                    compiled = self.context_compiler.compile(claimed, conversation)
                    claimed = self.store.prepare_run_context(
                        claimed.id, compiled.prompt, compiled.manifest()
                    )
                if self.capability_manager is not None:
                    claimed = self.capability_manager.prepare_run(
                        claimed, conversation, workspace
                    )
                spec = self.spec_factory.build(claimed, conversation, workspace)
                if self.capability_manager is not None:
                    spec = self.capability_manager.apply_to_spec(spec, claimed)
                # Mount run attachments read-only into the container
                spec = self._apply_attachments(spec, claimed)
                LOGGER.debug("starting worker for run %s", claimed.id)
                snapshot = await self.backend.start(spec)
                LOGGER.debug(
                    "worker %s started for run %s", snapshot.external_id, claimed.id
                )
                current = self.store.get_run(claimed.id)
                if current.status != "running":
                    await self.backend.stop(snapshot.external_id)
                    self.store.record_worker(
                        snapshot.external_id,
                        run_id=current.id,
                        workspace_id=workspace.id,
                        backend=current.backend,
                        state="terminated",
                        metadata={"reason": "run became terminal during worker startup"},
                    )
                    return
                self.store.attach_worker(
                    claimed.id,
                    snapshot.external_id,
                    workspace_id=workspace.id,
                    backend=claimed.backend,
                    metadata={"container_name": snapshot.name},
                )
                LOGGER.debug("worker attached for run %s", claimed.id)
                await self.event_bus.notify(claimed.id)
                self._monitor(claimed.id)
            except Exception as exc:
                LOGGER.exception("worker startup failed for run %s", claimed.id)
                self.store.transition_run(
                    claimed.id,
                    "failed",
                    expected=("running",),
                    error=f"worker startup failed: {exc}",
                )
                await self.event_bus.notify(claimed.id)
                self._schedule_dispatch(conversation_id)

    def _apply_attachments(self, spec: "ContainerSpec", run: RunRecord) -> "ContainerSpec":
        """Mount run attachments read-only into the worker container."""
        if self.attachment_store is None or self.attachment_storage is None:
            return spec
        attachments = self.attachment_store.list_for_run(run.id)
        if not attachments:
            return spec
        mounts = list(spec.mounts)
        model_id = str(run.request.get("resolved_model") or run.request.get("requested_model") or "")
        resolved = self.attachment_resolver.resolve(attachments, model_id)
        for item in resolved:
            if item.strategy == "file_mount" and item.mount_path:
                source = self.attachment_storage.read(item.record.storage_path)
                if source.is_file():
                    mounts.append((source, item.mount_path, "ro"))
        if mounts != list(spec.mounts):
            return replace(spec, mounts=tuple(mounts))
        return spec

    def _schedule_dispatch(self, conversation_id: str) -> None:
        task = asyncio.create_task(
            self._dispatch_conversation(conversation_id),
            name=f"dispatch-{conversation_id}",
        )
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._dispatch_tasks.discard)

    def _monitor(self, run_id: str) -> None:
        existing = self._tasks.get(run_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._monitor_loop(run_id), name=f"monitor-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run_id, None))

    async def _monitor_loop(self, run_id: str) -> None:
        try:
            while not self._stopping:
                run = self.store.get_run(run_id)
                if run.status != "running" or not run.worker_id:
                    return
                await self._ingest(run)
                snapshot = await self.backend.inspect(run.worker_id)
                if snapshot is None:
                    self.store.transition_run(
                        run.id,
                        "interrupted",
                        expected=("running",),
                        error="worker disappeared while gateway was running",
                    )
                    await self.event_bus.notify(run.id)
                    await self._dispatch_conversation(run.conversation_id)
                    return
                if not snapshot.running:
                    await self._ingest(run)
                    await self._finalize(run, snapshot, from_reconciliation=False)
                    await self._dispatch_conversation(run.conversation_id)
                    return
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.exception("worker monitor failed for run %s", run_id)
            try:
                run = self.store.get_run(run_id)
                if run.status == "running":
                    self.store.append_event(
                        run.id,
                        AgentEvent(
                            "backend.monitor_error",
                            run.id,
                            data={"message": str(exc)},
                        ),
                    )
                    self.store.transition_run(
                        run.id,
                        "failed",
                        expected=("running",),
                        error=f"monitor loop error: {exc}",
                    )
                    await self.event_bus.notify(run.id)
                    await self._dispatch_conversation(run.conversation_id)
            except Exception as inner_exc:
                LOGGER.exception("failed to recover from monitor error for run %s: %s", run_id, inner_exc)

    async def _ingest(self, run: RunRecord) -> None:
        if not run.worker_id:
            return
        envelopes = await self.backend.logs(run.worker_id)
        current_conversation = run.backend_conversation_id
        for envelope in envelopes:
            if envelope.source_sequence <= 0:
                event = AgentEvent(
                    "backend.protocol_error",
                    run.id,
                    current_conversation,
                    {"message": "malformed worker envelope", "line": envelope.line},
                )
            elif envelope.source == "stderr":
                event = AgentEvent(
                    "backend.diagnostic",
                    run.id,
                    current_conversation,
                    {"text": envelope.line or ""},
                )
            elif envelope.source == "worker":
                event = AgentEvent(
                    "backend.worker_exit",
                    run.id,
                    current_conversation,
                    {"exit_code": envelope.exit_code, "error": envelope.error},
                )
            elif envelope.source == "stdout":
                try:
                    raw = json.loads(envelope.line or "")
                    if not isinstance(raw, dict):
                        raise ValueError("AGY event was not an object")
                    name = raw.get("event")
                    payload = raw.get(name, {}) if isinstance(name, str) else {}
                    discovered = None
                    if isinstance(payload, dict):
                        discovered = payload.get("conversation_id") or raw.get(
                            "conversation_id"
                        )
                    if isinstance(discovered, str) and discovered:
                        current_conversation = discovered
                    event = _normalize_event(
                        raw,
                        run_id=run.id,
                        conversation_id=current_conversation,
                    )
                except (json.JSONDecodeError, ValueError) as exc:
                    event = AgentEvent(
                        "backend.protocol_error",
                        run.id,
                        current_conversation,
                        {"message": str(exc), "line": envelope.line},
                    )
            else:
                event = AgentEvent(
                    "backend.protocol_error",
                    run.id,
                    current_conversation,
                    {"message": f"unknown worker source: {envelope.source}"},
                )
            persisted = self.store.append_event(
                run.id, event, source_sequence=envelope.source_sequence
            )
            # Update Progress Engine & Telemetry
            if run.id not in self._progress_engines:
                self._progress_engines[run.id] = ProgressEngine(run.id)
            engine = self._progress_engines[run.id]
            telemetry_evt = TelemetryEvent.create(
                run_id=run.id,
                type=event.type,
                source=envelope.source if envelope else "worker",
                sequence=persisted.sequence,
                data=dict(event.data),
            )
            snapshot = engine.consume(telemetry_evt)
            self.store.save_progress_snapshot(snapshot.to_dict())
            self.store.append_telemetry_event(
                run_id=run.id,
                event_id=telemetry_evt.event_id,
                event_type=telemetry_evt.type,
                source=telemetry_evt.source,
                sequence=telemetry_evt.sequence,
                data=telemetry_evt.data,
            )
            if event.conversation_id:
                self.store.bind_backend_conversation(
                    run.conversation_id, event.conversation_id
                )
            # Duplicate source sequences return an existing event. Waking clients
            # is harmless; they still query by persisted sequence.
            await self.event_bus.notify(run.id)

    async def _finalize(
        self,
        run: RunRecord,
        snapshot: WorkerSnapshot,
        *,
        from_reconciliation: bool,
    ) -> None:
        events = self.store.list_events(run.id)
        event_types = [event.event_type for event in events]
        cancel_requested = "run.cancel_requested" in event_types
        terminal = next(
            (
                event
                for event in reversed(events)
                if event.event_type
                in {"agent.completed", "agent.failed", "agent.interrupted"}
            ),
            None,
        )
        if cancel_requested:
            status = "cancelled"
            error = "cancellation confirmed"
        elif terminal is not None and terminal.event_type == "agent.completed":
            failed_tools = [event for event in events if event.event_type == "tool.failed"]
            response = str(terminal.payload.get("response", ""))
            if failed_tools and not response.strip():
                status = "failed"
                error = "tool failed and AGY returned an empty successful result"
            else:
                status = "completed"
                error = None
        elif terminal is not None:
            status = "failed" if terminal.event_type == "agent.failed" else "interrupted"
            error = str(terminal.payload.get("error") or terminal.payload.get("response") or "")
        elif from_reconciliation:
            status = "interrupted"
            error = "worker exited without a recoverable final event"
        else:
            status = "failed"
            error = f"worker exited with code {snapshot.exit_code} without a final event"
        backend_conversation_id = next(
            (
                event.conversation_id
                for event in reversed(events)
                if event.conversation_id
            ),
            None,
        )
        response_text = ""
        if terminal is not None and terminal.payload.get("response"):
            response_text = str(terminal.payload.get("response", "")).strip()
        if not response_text:
            deltas = [
                str(e.payload.get("text_delta") or e.payload.get("content") or "")
                for e in events
                if e.event_type in {"message.delta", "agent.message"}
            ]
            response_text = "".join(deltas).strip()
        assistant_response = response_text if response_text else None
        self.store.transition_run(
            run.id,
            status,
            expected=("running",),
            backend_conversation_id=backend_conversation_id,
            error=error,
            assistant_response=assistant_response,
        )
        if run.worker_id:
            self.store.update_worker_state(run.worker_id, "exited")
        await self.event_bus.notify(run.id)

        # Goal continuation: if the completed run belongs to an active goal,
        # evaluate and potentially enqueue a follow-up run.
        if status in ("completed", "failed") and self.goal_evaluator is not None:
            await self._evaluate_goal(run, status)

        # Memory Curator: evaluate what should become durable long-term memory
        if status == "completed" and self.memory_curator is not None:
            try:
                finalized_run = self.store.get_run(run.id)
                await self.memory_curator.process_run(finalized_run)
            except Exception:
                LOGGER.exception("memory curator failed for run %s", run.id)

        # Learning: if the completed run is eligible, enqueue a background
        # learning job for async review. This is non-blocking and never
        # affects the user-facing conversation.
        if status == "completed" and self.learning_engine is not None:
            try:
                finalized_run = self.store.get_run(run.id)
                await self.learning_engine.process_run(finalized_run)
            except Exception:
                LOGGER.exception("learning engine failed for run %s", run.id)


    async def _evaluate_goal(self, run: RunRecord, status: str) -> None:
        """Check goal progress after a run completes; enqueue continuation if needed."""
        try:
            goal = self.store.get_active_goal(run.conversation_id)
            if goal is None:
                return

            # Increment the turn counter
            goal = self.store.increment_goal_turn(goal.id, run.id)

            # Evaluate acceptance
            evaluation = self.goal_evaluator.evaluate(goal, run)

            # Record the evaluation
            self.store.record_goal_evaluation(
                goal_id=goal.id,
                run_id=run.id,
                turn_number=goal.turns_used,
                verdict=evaluation.verdict,
                reason=evaluation.reason,
                acceptance_state=evaluation.acceptance_state,
            )

            if evaluation.verdict == "done":
                self.store.update_goal(goal.id, status="completed")
                LOGGER.info("goal %s completed after %d turns", goal.id, goal.turns_used)
            elif evaluation.verdict == "failed":
                self.store.update_goal(goal.id, status="failed")
                LOGGER.info("goal %s failed: %s", goal.id, evaluation.reason)
            elif evaluation.verdict == "paused":
                self.store.update_goal(goal.id, status="paused")
                LOGGER.info("goal %s paused: %s", goal.id, evaluation.reason)
            elif evaluation.verdict == "continue":
                # Enqueue a continuation run
                prompt = build_continuation_prompt(goal, evaluation)
                request = {
                    "prompt": prompt,
                    "context_profile": "chat",
                    "goal_id": goal.id,
                    "goal_continuation": True,
                }
                continuation = await self.submit(run.conversation_id, request)
                self.store.update_goal(goal.id, current_step=f"continuation run {continuation.id[:8]}")
                LOGGER.info(
                    "goal %s: enqueued continuation run %s (turn %d/%d)",
                    goal.id, continuation.id, goal.turns_used, goal.max_turns,
                )
        except Exception:
            LOGGER.exception("goal evaluation failed for run %s", run.id)
