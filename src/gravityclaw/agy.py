"""Async adapter for the official ``agy --output-format stream-json`` protocol."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .events import AgentEvent


TERMINAL_STATUSES = {
    "SUCCESS",
    "ERROR",
    "CANCELED",
    "INTERRUPTED",
    "INVALID",
    "WAITING",
    "RUNNING",
}


@dataclass(frozen=True, slots=True)
class AgyRunRequest:
    prompt: str
    cwd: Path
    conversation_id: str | None = None
    model: str | None = None
    effort: str | None = None
    agent: str | None = None
    print_timeout: str | None = None
    wall_timeout_seconds: float | None = None
    sandbox: bool = False
    allow_all: bool = False
    extra_args: tuple[str, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise ValueError("prompt must not be empty")
        if self.effort not in {None, "low", "medium", "high"}:
            raise ValueError("effort must be low, medium, or high")
        if not self.cwd.is_dir():
            raise ValueError(f"workspace does not exist: {self.cwd}")
        if self.wall_timeout_seconds is not None and self.wall_timeout_seconds <= 0:
            raise ValueError("wall_timeout_seconds must be positive")


class AgyProtocolError(RuntimeError):
    """Raised for a structurally invalid AGY stream."""


class AgyRun:
    """One running AGY subprocess and its normalized event stream."""

    def __init__(
        self,
        *,
        run_id: str,
        process: asyncio.subprocess.Process,
        command: Sequence[str],
        wall_timeout_seconds: float | None,
    ) -> None:
        self.run_id = run_id
        self.process = process
        self.command = tuple(command)
        self.wall_timeout_seconds = wall_timeout_seconds
        self._cancel_requested = False
        self._timed_out = False

    async def cancel(self, grace_seconds: float = 3.0) -> None:
        """Interrupt the complete AGY process group, escalating if necessary."""
        if self.process.returncode is not None:
            return
        self._cancel_requested = True
        try:
            os.killpg(self.process.pid, signal.SIGINT)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self.process.wait(), timeout=grace_seconds)
            return
        except TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(self.process.wait(), timeout=grace_seconds)
            return
        except TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGKILL)
        await self.process.wait()

    async def events(self) -> AsyncIterator[AgentEvent]:
        """Drain stdout/stderr concurrently and emit backend-neutral events."""
        queue: asyncio.Queue[tuple[str, bytes | None]] = asyncio.Queue()

        async def drain(name: str, stream: asyncio.StreamReader) -> None:
            while line := await stream.readline():
                await queue.put((name, line))
            await queue.put((name, None))

        assert self.process.stdout is not None
        assert self.process.stderr is not None
        readers = [
            asyncio.create_task(drain("stdout", self.process.stdout)),
            asyncio.create_task(drain("stderr", self.process.stderr)),
        ]
        watchdog: asyncio.Task[None] | None = None

        async def enforce_wall_timeout() -> None:
            assert self.wall_timeout_seconds is not None
            await asyncio.sleep(self.wall_timeout_seconds)
            if self.process.returncode is None:
                self._timed_out = True
                await self.cancel()

        if self.wall_timeout_seconds is not None:
            watchdog = asyncio.create_task(enforce_wall_timeout())
        closed: set[str] = set()
        conversation_id: str | None = None
        saw_init = False
        saw_result = False

        try:
            while len(closed) < 2:
                source, line = await queue.get()
                if line is None:
                    closed.add(source)
                    continue
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if source == "stderr":
                    yield AgentEvent(
                        type="backend.diagnostic",
                        run_id=self.run_id,
                        conversation_id=conversation_id,
                        data={"text": text},
                    )
                    continue
                try:
                    raw = json.loads(text)
                except json.JSONDecodeError as exc:
                    yield AgentEvent(
                        type="backend.protocol_error",
                        run_id=self.run_id,
                        conversation_id=conversation_id,
                        data={"message": str(exc), "line": text},
                    )
                    continue
                if not isinstance(raw, dict):
                    yield AgentEvent(
                        type="backend.protocol_error",
                        run_id=self.run_id,
                        conversation_id=conversation_id,
                        data={"message": "AGY event was not an object", "value": raw},
                    )
                    continue

                event_name = raw.get("event")
                payload = raw.get(event_name, {}) if isinstance(event_name, str) else {}
                if isinstance(payload, dict):
                    discovered = payload.get("conversation_id") or raw.get("conversation_id")
                    if isinstance(discovered, str) and discovered:
                        conversation_id = discovered

                if event_name == "init":
                    saw_init = True
                elif event_name == "result":
                    saw_result = True

                normalized = _normalize_event(
                    raw,
                    run_id=self.run_id,
                    conversation_id=conversation_id,
                )
                if (
                    event_name == "result"
                    and self._cancel_requested
                    and normalized.type == "agent.failed"
                ):
                    normalized = AgentEvent(
                        type="agent.interrupted",
                        run_id=normalized.run_id,
                        conversation_id=normalized.conversation_id,
                        data={**normalized.data, "cancellation_requested": True},
                        raw=normalized.raw,
                    )
                yield normalized
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            if watchdog is not None and not watchdog.done():
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)
            if self.process.returncode is None:
                await self.process.wait()

        if self._timed_out:
            yield AgentEvent(
                type="backend.timeout",
                run_id=self.run_id,
                conversation_id=conversation_id,
                data={
                    "message": "GravityClaw wall-clock timeout expired",
                    "timeout_seconds": self.wall_timeout_seconds,
                    "exit_code": self.process.returncode,
                },
            )
        # AGY can emit an ERROR result before init for startup failures such as
        # missing authentication. Only a stream with neither is malformed.
        if not saw_init and not saw_result:
            yield AgentEvent(
                type="backend.protocol_error",
                run_id=self.run_id,
                conversation_id=conversation_id,
                data={"message": "AGY stream ended without an init event"},
            )
        if not saw_result and not self._timed_out:
            if self._cancel_requested:
                event_type = "agent.interrupted"
            else:
                event_type = "backend.protocol_error"
            yield AgentEvent(
                type=event_type,
                run_id=self.run_id,
                conversation_id=conversation_id,
                data={
                    "message": "AGY stream ended without a result event",
                    "exit_code": self.process.returncode,
                },
            )


class AgyAdapter:
    """Construct and supervise official AGY CLI subprocesses."""

    def __init__(self, binary: str | Path = "agy") -> None:
        self.binary = str(binary)

    def build_command(self, request: AgyRunRequest) -> list[str]:
        command = [
            self.binary,
            "-p",
            request.prompt,
            "--output-format",
            "stream-json",
        ]
        if request.conversation_id:
            command.extend(["--conversation", request.conversation_id])
        if request.model:
            command.extend(["--model", request.model])
        if request.effort:
            command.extend(["--effort", request.effort])
        if request.agent:
            command.extend(["--agent", request.agent])
        if request.print_timeout:
            command.extend(["--print-timeout", request.print_timeout])
        if request.sandbox:
            command.append("--sandbox")
        if request.allow_all:
            command.append("--dangerously-skip-permissions")
        command.extend(request.extra_args)
        return command

    async def start(self, request: AgyRunRequest) -> AgyRun:
        command = self.build_command(request)
        environment = os.environ.copy()
        environment.update(request.environment)
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=request.cwd,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        return AgyRun(
            run_id=str(uuid.uuid4()),
            process=process,
            command=command,
            wall_timeout_seconds=request.wall_timeout_seconds,
        )


def _normalize_event(
    raw: dict[str, Any], *, run_id: str, conversation_id: str | None
) -> AgentEvent:
    event_name = raw.get("event")
    if event_name == "init":
        payload = _dict_payload(raw, "init")
        return AgentEvent("agent.started", run_id, conversation_id, payload, raw)

    if event_name == "step_update":
        payload = _dict_payload(raw, "step_update")
        step_type = payload.get("step_type")
        state = payload.get("state")
        if step_type == "agent_response" and payload.get("text_delta") is not None:
            return AgentEvent("message.delta", run_id, conversation_id, payload, raw)
        if step_type == "tool":
            if state == "DONE":
                kind = "tool.finished"
            elif state in {"ERROR", "CANCELED", "INTERRUPTED"}:
                kind = "tool.failed"
            else:
                kind = "tool.started"
            return AgentEvent(kind, run_id, conversation_id, payload, raw)
        if payload.get("subagent_info") is not None:
            return AgentEvent("subagent.updated", run_id, conversation_id, payload, raw)
        return AgentEvent("agent.step", run_id, conversation_id, payload, raw)

    if event_name == "result":
        payload = _dict_payload(raw, "result")
        status = str(payload.get("status", "INVALID")).upper()
        if status == "SUCCESS":
            kind = "agent.completed"
        elif status in {"CANCELED", "INTERRUPTED"}:
            kind = "agent.interrupted"
        else:
            kind = "agent.failed"
        payload = {**payload, "status": status}
        return AgentEvent(kind, run_id, conversation_id, payload, raw)

    return AgentEvent(
        "backend.event",
        run_id,
        conversation_id,
        {"backend_event": event_name, "payload": raw},
        raw,
    )


def _dict_payload(raw: dict[str, Any], key: str) -> dict[str, Any]:
    payload = raw.get(key)
    if isinstance(payload, dict):
        return payload
    return {"protocol_error": f"{key} payload was not an object", "value": payload}
