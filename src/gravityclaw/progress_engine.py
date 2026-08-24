"""Deterministic Progress Engine for GravityClaw.

Interprets raw telemetry events and maintains a materialized ProgressSnapshot.
"""

from __future__ import annotations

from typing import Any
from .telemetry import (
    ProgressCounters,
    ProgressSnapshot,
    ProgressStep,
    TelemetryEvent,
    utc_now_iso,
)


class ProgressEngine:
    """Consumes telemetry events sequentially and materializes an up-to-date ProgressSnapshot."""

    def __init__(self, run_id: str, max_tail_lines: int = 15) -> None:
        self.run_id = run_id
        self.max_tail_lines = max_tail_lines
        self.snapshot = ProgressSnapshot(
            run_id=run_id,
            status="running",
            started_at=utc_now_iso(),
            last_activity_at=utc_now_iso(),
        )
        self._active_operations: dict[str, dict[str, Any]] = {}
        self._op_stack: list[str] = []

    def consume(self, event: TelemetryEvent) -> ProgressSnapshot:
        """Process a telemetry event and return the updated ProgressSnapshot."""
        self.snapshot.version += 1
        self.snapshot.last_activity_at = event.timestamp

        event_type = event.type
        data = event.data

        # ── 1. Run Lifecycle ──────────────────────────────────────────────
        if event_type == "run.started":
            self.snapshot.status = "running"
            if not self.snapshot.current_label:
                self.snapshot.current_label = data.get("prompt") or "Executing task"
        elif event_type == "run.completed":
            self.snapshot.status = "completed"
            self.snapshot.last_progress_at = event.timestamp
            self.snapshot.active_operation_id = None
            self.snapshot.active_operation_kind = None
        elif event_type == "run.failed":
            self.snapshot.status = "failed"
            self.snapshot.current_detail = data.get("error") or "Execution failed"
            self.snapshot.active_operation_id = None
            self.snapshot.active_operation_kind = None
        elif event_type in {"run.cancelled", "run.interrupted"}:
            self.snapshot.status = "cancelled" if "cancelled" in event_type else "interrupted"
            self.snapshot.active_operation_id = None
            self.snapshot.active_operation_kind = None

        # ── 2. Model Execution ─────────────────────────────────────────────
        elif event_type == "model.started":
            self.snapshot.current_label = "Agent is deciding next action…"
            self.snapshot.active_operation_kind = "model"
        elif event_type == "model.streaming" or event_type == "message.delta":
            self.snapshot.last_output_at = event.timestamp
            self.snapshot.current_label = "Generating response…"
        elif event_type == "model.completed":
            self.snapshot.last_progress_at = event.timestamp

        # ── 3. Tool & Process Lifecycle ───────────────────────────────────
        elif event_type in {"tool.started", "process.started", "ssh.connecting", "ssh.command_started"}:
            self.snapshot.counters.tool_calls += 1
            op_id = event.operation_id or f"op_{event.sequence}"
            tool_name = event.tool or data.get("tool_name") or data.get("command") or "Tool"
            kind = data.get("kind") or ("ssh" if "ssh" in event_type else "command" if "process" in event_type else "tool")
            
            op_info = {
                "id": op_id,
                "kind": kind,
                "tool": tool_name,
                "label": _derive_tool_label(tool_name, data),
                "started_at": event.timestamp,
            }
            self._active_operations[op_id] = op_info
            if op_id not in self._op_stack:
                self._op_stack.append(op_id)

            self.snapshot.active_operation_id = op_id
            self.snapshot.active_operation_kind = kind
            self.snapshot.current_label = op_info["label"]
            self.snapshot.current_detail = data.get("command") or data.get("path") or data.get("detail")

            if kind in {"command", "ssh"}:
                self.snapshot.counters.commands += 1

        elif event_type in {"process.output", "ssh.output"}:
            self.snapshot.last_output_at = event.timestamp
            text = str(data.get("text", ""))
            bytes_count = int(data.get("bytes", len(text)))
            self.snapshot.counters.output_bytes += bytes_count

            lines = [l for l in text.splitlines() if l.strip()]
            self.snapshot.counters.output_lines += len(lines)
            for line in lines:
                self.snapshot.recent_output_tail.append(line)
            if len(self.snapshot.recent_output_tail) > self.max_tail_lines:
                self.snapshot.recent_output_tail = self.snapshot.recent_output_tail[-self.max_tail_lines:]

        elif event_type in {"tool.completed", "process.exited", "ssh.command_exited"}:
            self.snapshot.last_progress_at = event.timestamp
            op_id = event.operation_id
            if op_id and op_id in self._active_operations:
                self._active_operations.pop(op_id, None)
            if op_id and op_id in self._op_stack:
                self._op_stack.remove(op_id)

            # Pop back to parent or previous active operation
            if self._op_stack:
                prev_op_id = self._op_stack[-1]
                prev_info = self._active_operations.get(prev_op_id, {})
                self.snapshot.active_operation_id = prev_op_id
                self.snapshot.active_operation_kind = prev_info.get("kind")
                self.snapshot.current_label = prev_info.get("label")
                self.snapshot.current_detail = None
            else:
                self.snapshot.active_operation_id = None
                self.snapshot.active_operation_kind = None
                self.snapshot.current_label = "Agent is planning next step…"
                self.snapshot.current_detail = None

        elif event_type in {"tool.failed", "process.killed"}:
            op_id = event.operation_id
            if op_id and op_id in self._active_operations:
                self._active_operations.pop(op_id, None)
            if op_id and op_id in self._op_stack:
                self._op_stack.remove(op_id)
            self.snapshot.current_detail = f"Failed: {data.get('error') or 'Error'}"

        # ── 4. File Operations ────────────────────────────────────────────
        elif event_type == "file.read":
            self.snapshot.counters.files_read += 1
        elif event_type in {"file.created", "file.modified", "file.deleted"}:
            self.snapshot.counters.files_modified += 1
            self.snapshot.last_progress_at = event.timestamp

        # ── 5. Semantic Progress & Milestones ─────────────────────────────
        elif event_type == "progress.step_started":
            key = data.get("key", f"step_{len(self.snapshot.completed_steps)}")
            label = data.get("label", key)
            new_step = ProgressStep(key=key, label=label, state="active", started_at=event.timestamp)
            self.snapshot.active_step = new_step
            self.snapshot.current_label = label

        elif event_type == "progress.step_completed":
            self.snapshot.last_progress_at = event.timestamp
            key = data.get("key")
            if self.snapshot.active_step and (not key or self.snapshot.active_step.key == key):
                completed = self.snapshot.active_step
                completed.state = "completed"
                completed.completed_at = event.timestamp
                self.snapshot.completed_steps.append(completed)
                self.snapshot.active_step = None
            elif key:
                # Check pending list
                matched = next((s for s in self.snapshot.pending_steps if s.key == key), None)
                if matched:
                    self.snapshot.pending_steps.remove(matched)
                    matched.state = "completed"
                    matched.completed_at = event.timestamp
                    self.snapshot.completed_steps.append(matched)

        elif event_type == "progress.step_failed":
            key = data.get("key")
            if self.snapshot.active_step and (not key or self.snapshot.active_step.key == key):
                failed = self.snapshot.active_step
                failed.state = "failed"
                failed.completed_at = event.timestamp
                failed.detail = data.get("error")
                self.snapshot.completed_steps.append(failed)
                self.snapshot.active_step = None

        return self.snapshot


def _derive_tool_label(tool_name: str, data: dict[str, Any]) -> str:
    name_lower = tool_name.lower()
    cmd = data.get("command") or data.get("CommandLine") or ""
    if "ssh" in name_lower or "ssh" in cmd:
        return "Execute SSH"
    if "terminal" in name_lower or "bash" in name_lower or "run_command" in name_lower or "exec" in name_lower:
        return "Execute command"
    if "read" in name_lower or "view" in name_lower:
        path = data.get("path") or data.get("AbsolutePath") or ""
        basename = path.split("/")[-1] if path else ""
        return f"Read {basename}" if basename else "Read file"
    if "write" in name_lower or "edit" in name_lower:
        path = data.get("path") or data.get("TargetFile") or ""
        basename = path.split("/")[-1] if path else ""
        return f"Edit {basename}" if basename else "Modify file"
    if "search" in name_lower or "grep" in name_lower:
        return "Search codebase"
    return tool_name.replace("_", " ").title()
