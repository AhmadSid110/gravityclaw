"""Acceptance tests for Progress Engine & Execution Telemetry Architecture."""

import asyncio
import pytest
from pathlib import Path
import tempfile

from gravityclaw.telemetry import (
    TelemetryEmitter,
    TelemetryEvent,
    ProgressSnapshot,
    ProgressStep,
    ProgressCounters,
)
from gravityclaw.progress_engine import ProgressEngine
from gravityclaw.process_stream import StreamingProcessExecutor
from gravityclaw.store import Store


@pytest.mark.asyncio
async def test_progress_engine_deterministic_consumption():
    engine = ProgressEngine(run_id="run_test_123")
    emitter = TelemetryEmitter("run_test_123")

    # 1. Run started
    evt1 = await emitter.emit("run.started", data={"prompt": "Use Tailscale"})
    snap1 = engine.consume(evt1)
    assert snap1.status == "running"
    assert snap1.current_label == "Use Tailscale"

    # 2. SSH Command started
    evt2 = await emitter.emit(
        "ssh.command_started",
        operation_id="op_ssh_1",
        tool="ssh",
        data={"command": "ssh -i /key ubuntu@vps 'tailscale up'"},
    )
    snap2 = engine.consume(evt2)
    assert snap2.active_operation_id == "op_ssh_1"
    assert snap2.active_operation_kind == "ssh"
    assert snap2.current_label == "Execute SSH"
    assert snap2.counters.commands == 1

    # 3. Live stdout streaming
    evt3 = await emitter.emit(
        "ssh.output",
        operation_id="op_ssh_1",
        tool="ssh",
        data={"stream": "stdout", "text": "Starting tailscaled daemon...\nAuthentication URL: https://login.tailscale.com/a/123", "bytes": 78},
    )
    snap3 = engine.consume(evt3)
    assert snap3.last_output_at == evt3.timestamp
    assert len(snap3.recent_output_tail) == 2
    assert "Starting tailscaled daemon..." in snap3.recent_output_tail[0]
    assert snap3.counters.output_lines == 2
    assert snap3.counters.output_bytes == 78

    # 4. Semantic Progress Step
    evt4 = await emitter.emit(
        "progress.step_started",
        data={"key": "configure_tailscale", "label": "Configure Tailscale on VPS"},
    )
    snap4 = engine.consume(evt4)
    assert snap4.active_step is not None
    assert snap4.active_step.key == "configure_tailscale"

    evt5 = await emitter.emit(
        "progress.step_completed",
        data={"key": "configure_tailscale"},
    )
    snap5 = engine.consume(evt5)
    assert len(snap5.completed_steps) == 1
    assert snap5.completed_steps[0].state == "completed"
    assert snap5.active_step is None

    # 5. SSH Command exited
    evt6 = await emitter.emit(
        "ssh.command_exited",
        operation_id="op_ssh_1",
        tool="ssh",
        data={"exit_code": 0, "duration_seconds": 12.4},
    )
    snap6 = engine.consume(evt6)
    assert snap6.active_operation_id is None
    assert snap6.last_progress_at == evt6.timestamp


@pytest.mark.asyncio
async def test_streaming_process_executor_live_lines():
    emitter = TelemetryEmitter("run_test_proc")
    collected_events: list[TelemetryEvent] = []
    emitter._callback = lambda e: collected_events.append(e)

    executor = StreamingProcessExecutor(heartbeat_interval_seconds=1.0)
    res = await executor.execute(
        ["echo", "Line 1"],
        run_id="run_test_proc",
        emitter=emitter,
        operation_id="op_echo",
        shell=False,
    )

    assert res.exit_code == 0
    assert len(collected_events) >= 3  # started, output(s), exited
    output_events = [e for e in collected_events if e.type in {"process.output", "ssh.output"}]
    assert len(output_events) > 0


def test_store_telemetry_and_progress_persistence():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        store = Store(db_path)
        store.initialize()

        snapshot_dict = {
            "run_id": "run_store_test",
            "status": "running",
            "current_label": "Configure VPS",
            "current_detail": "ssh ubuntu@vps",
            "active_operation_id": "op_99",
            "active_operation_kind": "ssh",
            "started_at": "2026-08-20T08:00:00.000Z",
            "last_activity_at": "2026-08-20T08:02:14.000Z",
            "last_output_at": "2026-08-20T08:02:10.000Z",
            "last_progress_at": "2026-08-20T08:02:00.000Z",
            "completed_steps": [{"key": "s1", "label": "Connect", "state": "completed"}],
            "pending_steps": [],
            "recent_output_tail": ["Output line 1", "Output line 2"],
            "counters": {"tool_calls": 3, "commands": 2, "output_lines": 14, "output_bytes": 450},
            "version": 8,
        }

        # Save snapshot
        store.save_progress_snapshot(snapshot_dict)
        retrieved = store.get_progress_snapshot("run_store_test")
        assert retrieved is not None
        assert retrieved["run_id"] == "run_store_test"
        assert retrieved["current_label"] == "Configure VPS"
        assert len(retrieved["recent_output_tail"]) == 2

        # Append telemetry event
        row_id = store.append_telemetry_event(
            run_id="run_store_test",
            event_id="evt_abc123",
            event_type="process.output",
            source="terminal",
            operation_id="op_99",
            sequence=1,
            data={"text": "Hello world", "bytes": 11},
        )
        assert row_id > 0

        events = store.list_telemetry_events("run_store_test", since_sequence=0)
        assert len(events) == 1
        assert events[0]["event_id"] == "evt_abc123"
        assert events[0]["data"]["text"] == "Hello world"
