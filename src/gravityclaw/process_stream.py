"""Streaming Process and SSH Executor with real-time telemetry streaming."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .telemetry import TelemetryEmitter


@dataclass(frozen=True, slots=True)
class ProcessExecutionResult:
    exit_code: int
    duration_seconds: float
    output_lines: list[str]
    error_lines: list[str]


class StreamingProcessExecutor:
    """Executes local or remote/SSH commands while streaming stdout/stderr telemetry in real-time."""

    def __init__(self, heartbeat_interval_seconds: float = 4.0) -> None:
        self.heartbeat_interval_seconds = heartbeat_interval_seconds

    async def execute(
        self,
        command: Sequence[str] | str,
        *,
        run_id: str,
        emitter: TelemetryEmitter,
        operation_id: str | None = None,
        parent_operation_id: str | None = None,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        shell: bool = False,
        is_ssh: bool = False,
    ) -> ProcessExecutionResult:
        """Run a process, stream stdout/stderr lines as telemetry events, and return the result."""
        env_dict = os.environ.copy()
        if env:
            env_dict.update(env)

        working_dir = str(cwd) if cwd else os.getcwd()
        cmd_str = command if isinstance(command, str) else " ".join(command)
        tool_name = "ssh" if is_ssh or "ssh" in cmd_str else "terminal"
        event_prefix = "ssh" if tool_name == "ssh" else "process"

        # Emit start event
        await emitter.emit(
            f"{event_prefix}.started" if event_prefix == "process" else "ssh.command_started",
            source=tool_name,
            operation_id=operation_id,
            parent_operation_id=parent_operation_id,
            tool=tool_name,
            data={"command": cmd_str, "cwd": working_dir},
        )

        start_time = time.monotonic()
        if shell:
            proc = await asyncio.create_subprocess_shell(
                cmd_str,
                cwd=working_dir,
                env=env_dict,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            cmd_list = [command] if isinstance(command, str) else list(command)
            proc = await asyncio.create_subprocess_exec(
                *cmd_list,
                cwd=working_dir,
                env=env_dict,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        async def pump_stream(stream: asyncio.StreamReader, stream_name: str, collector: list[str]) -> None:
            while True:
                line_bytes = await stream.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                collector.append(line)
                # Emit line telemetry immediately
                await emitter.emit(
                    f"{event_prefix}.output",
                    source=tool_name,
                    operation_id=operation_id,
                    parent_operation_id=parent_operation_id,
                    tool=tool_name,
                    data={"stream": stream_name, "text": line, "bytes": len(line_bytes)},
                )

        async def heartbeat_loop() -> None:
            while proc.returncode is None:
                await asyncio.sleep(self.heartbeat_interval_seconds)
                if proc.returncode is None:
                    await emitter.emit(
                        f"{event_prefix}.heartbeat",
                        source=tool_name,
                        operation_id=operation_id,
                        parent_operation_id=parent_operation_id,
                        tool=tool_name,
                        data={"pid": proc.pid, "elapsed": time.monotonic() - start_time},
                    )

        assert proc.stdout is not None
        assert proc.stderr is not None

        stdout_task = asyncio.create_task(pump_stream(proc.stdout, "stdout", stdout_lines))
        stderr_task = asyncio.create_task(pump_stream(proc.stderr, "stderr", stderr_lines))
        hb_task = asyncio.create_task(heartbeat_loop())

        try:
            exit_code = await proc.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        finally:
            hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb_task

        duration = time.monotonic() - start_time

        await emitter.emit(
            f"{event_prefix}.exited" if event_prefix == "process" else "ssh.command_exited",
            source=tool_name,
            operation_id=operation_id,
            parent_operation_id=parent_operation_id,
            tool=tool_name,
            data={"exit_code": exit_code, "duration_seconds": duration},
        )

        return ProcessExecutionResult(
            exit_code=exit_code,
            duration_seconds=duration,
            output_lines=stdout_lines,
            error_lines=stderr_lines,
        )
