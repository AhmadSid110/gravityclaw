"""Canonical execution telemetry contracts and event emitters for GravityClaw.

Tools report facts. The Progress Engine interprets those facts.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator, Literal


def utc_now_iso() -> str:
    """Return an RFC 3339 / ISO 8601 UTC timestamp string with milliseconds."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class TelemetryEvent:
    """Canonical envelope for all GravityClaw execution telemetry."""

    event_id: str
    run_id: str
    type: str
    timestamp: str
    source: str
    operation_id: str | None = None
    parent_operation_id: str | None = None
    sequence: int = 0
    tool: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        type: str,
        source: str = "worker",
        operation_id: str | None = None,
        parent_operation_id: str | None = None,
        sequence: int = 0,
        tool: str | None = None,
        data: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> TelemetryEvent:
        return cls(
            event_id=f"evt_{uuid.uuid4().hex[:16]}",
            run_id=run_id,
            type=type,
            timestamp=timestamp or utc_now_iso(),
            source=source,
            operation_id=operation_id,
            parent_operation_id=parent_operation_id,
            sequence=sequence,
            tool=tool,
            data=data or {},
        )


@dataclass(slots=True)
class ProgressStep:
    """Semantic task step / milestone."""

    key: str
    label: str
    state: str = "pending"  # "pending", "active", "completed", "failed"
    started_at: str | None = None
    completed_at: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ProgressCounters:
    """Deterministic telemetry execution counters."""

    tool_calls: int = 0
    commands: int = 0
    files_read: int = 0
    files_modified: int = 0
    output_lines: int = 0
    output_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ProgressSnapshot:
    """Materialized progress read model for GravityClaw runs."""

    run_id: str
    status: str
    current_label: str | None = None
    current_detail: str | None = None
    active_operation_id: str | None = None
    active_operation_kind: str | None = None

    started_at: str = field(default_factory=utc_now_iso)
    last_activity_at: str = field(default_factory=utc_now_iso)
    last_output_at: str | None = None
    last_progress_at: str | None = None

    completed_steps: list[ProgressStep] = field(default_factory=list)
    active_step: ProgressStep | None = None
    pending_steps: list[ProgressStep] = field(default_factory=list)

    recent_output_tail: list[str] = field(default_factory=list)
    counters: ProgressCounters = field(default_factory=ProgressCounters)
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "current_label": self.current_label,
            "current_detail": self.current_detail,
            "active_operation_id": self.active_operation_id,
            "active_operation_kind": self.active_operation_kind,
            "started_at": self.started_at,
            "last_activity_at": self.last_activity_at,
            "last_output_at": self.last_output_at,
            "last_progress_at": self.last_progress_at,
            "completed_steps": [s.to_dict() for s in self.completed_steps],
            "active_step": self.active_step.to_dict() if self.active_step else None,
            "pending_steps": [s.to_dict() for s in self.pending_steps],
            "recent_output_tail": list(self.recent_output_tail),
            "counters": self.counters.to_dict(),
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProgressSnapshot:
        completed = [
            ProgressStep(**s) if isinstance(s, dict) else s
            for s in data.get("completed_steps", [])
        ]
        active_raw = data.get("active_step")
        active = ProgressStep(**active_raw) if isinstance(active_raw, dict) else None
        pending = [
            ProgressStep(**s) if isinstance(s, dict) else s
            for s in data.get("pending_steps", [])
        ]
        counters_raw = data.get("counters", {})
        counters = ProgressCounters(**counters_raw) if isinstance(counters_raw, dict) else ProgressCounters()

        return cls(
            run_id=data["run_id"],
            status=data.get("status", "running"),
            current_label=data.get("current_label"),
            current_detail=data.get("current_detail"),
            active_operation_id=data.get("active_operation_id"),
            active_operation_kind=data.get("active_operation_kind"),
            started_at=data.get("started_at", utc_now_iso()),
            last_activity_at=data.get("last_activity_at", utc_now_iso()),
            last_output_at=data.get("last_output_at"),
            last_progress_at=data.get("last_progress_at"),
            completed_steps=completed,
            active_step=active,
            pending_steps=pending,
            recent_output_tail=data.get("recent_output_tail", []),
            counters=counters,
            version=data.get("version", 1),
        )


class TelemetryEmitter:
    """Thread-safe and async-safe telemetry event emitter for a run."""

    def __init__(self, run_id: str, callback: Any | None = None) -> None:
        self.run_id = run_id
        self._callback = callback
        self._seq = 0
        self._lock = asyncio.Lock()

    def next_sequence(self) -> int:
        self._seq += 1
        return self._seq

    async def emit(
        self,
        event_type: str,
        *,
        source: str = "worker",
        operation_id: str | None = None,
        parent_operation_id: str | None = None,
        tool: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> TelemetryEvent:
        async with self._lock:
            seq = self.next_sequence()
        event = TelemetryEvent.create(
            run_id=self.run_id,
            type=event_type,
            source=source,
            operation_id=operation_id,
            parent_operation_id=parent_operation_id,
            sequence=seq,
            tool=tool,
            data=data or {},
        )
        if self._callback is not None:
            import inspect
            if inspect.iscoroutinefunction(self._callback):
                await self._callback(event)
            else:
                self._callback(event)
        return event

    @contextlib.asynccontextmanager
    async def operation(
        self,
        kind: str,
        name: str,
        *,
        source: str = "tool",
        parent_operation_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """Context manager wrapping an operation with start/complete/failed telemetry."""
        op_id = f"op_{uuid.uuid4().hex[:12]}"
        start_data = {**(data or {}), "kind": kind, "name": name}
        await self.emit(
            "tool.started",
            source=source,
            operation_id=op_id,
            parent_operation_id=parent_operation_id,
            tool=name,
            data=start_data,
        )
        start_time = time.monotonic()
        try:
            yield op_id
            elapsed = time.monotonic() - start_time
            await self.emit(
                "tool.completed",
                source=source,
                operation_id=op_id,
                parent_operation_id=parent_operation_id,
                tool=name,
                data={**(data or {}), "kind": kind, "name": name, "duration_seconds": elapsed},
            )
        except Exception as exc:
            elapsed = time.monotonic() - start_time
            await self.emit(
                "tool.failed",
                source=source,
                operation_id=op_id,
                parent_operation_id=parent_operation_id,
                tool=name,
                data={
                    **(data or {}),
                    "kind": kind,
                    "name": name,
                    "error": str(exc),
                    "duration_seconds": elapsed,
                },
            )
            raise
