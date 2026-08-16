"""Container execution backend and AGY/fake command factories."""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .store import Conversation, RunRecord, Workspace


MANAGED_LABEL = "io.gravityclaw.managed"
RUN_LABEL = "io.gravityclaw.run-id"
WORKSPACE_LABEL = "io.gravityclaw.workspace-id"


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    run_id: str
    workspace_id: str
    workspace: Path
    image: str
    command: tuple[str, ...]
    environment: Mapping[str, str] = field(default_factory=dict)
    network: bool = False
    home_volume: str | None = None
    memory: str = "2g"
    cpus: float = 2.0
    pids_limit: int = 256
    mounts: tuple[tuple[Path, str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class WorkerEnvelope:
    source_sequence: int
    source: str
    line: str | None = None
    exit_code: int | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class WorkerSnapshot:
    external_id: str
    name: str
    state: str
    running: bool
    exit_code: int | None
    labels: dict[str, str]


class ExecutionBackend(Protocol):
    async def start(self, spec: ContainerSpec) -> WorkerSnapshot: ...
    async def inspect(self, external_id: str) -> WorkerSnapshot | None: ...
    async def logs(self, external_id: str) -> list[WorkerEnvelope]: ...
    async def list_managed(self) -> list[WorkerSnapshot]: ...
    async def stop(self, external_id: str) -> WorkerSnapshot | None: ...
    async def remove(self, external_id: str) -> None: ...


class SpecFactory(Protocol):
    def build(
        self, run: RunRecord, conversation: Conversation, workspace: Workspace
    ) -> ContainerSpec: ...


class PodmanExecutionBackend:
    """Rootless Podman backend with labeled, externally discoverable workers."""

    def __init__(self, binary: str = "podman") -> None:
        self.binary = binary

    async def start(self, spec: ContainerSpec) -> WorkerSnapshot:
        name = f"gravityclaw-{spec.run_id}"
        command = [
            self.binary,
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            f"{MANAGED_LABEL}=true",
            "--label",
            f"{RUN_LABEL}={spec.run_id}",
            "--label",
            f"{WORKSPACE_LABEL}={spec.workspace_id}",
            "--read-only",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--pids-limit",
            str(spec.pids_limit),
            "--memory",
            spec.memory,
            "--cpus",
            str(spec.cpus),
            "--userns=keep-id:uid=1000,gid=1000",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=128m",
            "--volume",
            f"{spec.workspace.resolve()}:/workspace:rw,rprivate",
            "--workdir",
            "/workspace",
        ]
        if spec.network:
            command.extend(["--network", "slirp4netns:allow_host_loopback=false"])
        else:
            command.extend(["--network", "none"])
        if spec.home_volume:
            command.extend(
                ["--volume", f"{spec.home_volume}:/home/worker:rw,rprivate,U"]
            )
        for source, target, mode in spec.mounts:
            command.extend(["--volume", f"{source.resolve()}:{target}:{mode},rprivate"])
        for key, value in sorted(spec.environment.items()):
            command.extend(["--env", f"{key}={value}"])
        command.append(spec.image)
        command.extend(spec.command)
        output = await self._run(command)
        external_id = output.strip()
        snapshot = await self.inspect(external_id)
        if snapshot is None:
            raise RuntimeError("Podman created a worker that could not be inspected")
        return snapshot

    async def inspect(self, external_id: str) -> WorkerSnapshot | None:
        try:
            output = await self._run(
                [self.binary, "inspect", external_id, "--format", "json"]
            )
        except RuntimeError:
            return None
        values = json.loads(output)
        if not values:
            return None
        return _snapshot(values[0])

    async def logs(self, external_id: str) -> list[WorkerEnvelope]:
        output = await self._run([self.binary, "logs", external_id])
        envelopes: list[WorkerEnvelope] = []
        for raw_line in output.splitlines():
            try:
                value = json.loads(raw_line)
                envelopes.append(
                    WorkerEnvelope(
                        source_sequence=int(value["source_sequence"]),
                        source=str(value["source"]),
                        line=value.get("line"),
                        exit_code=value.get("exit_code"),
                        error=value.get("error"),
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                # A malformed worker envelope must remain observable.
                envelopes.append(
                    WorkerEnvelope(
                        source_sequence=-(len(envelopes) + 1),
                        source="protocol_error",
                        line=raw_line,
                    )
                )
        return sorted(envelopes, key=lambda envelope: envelope.source_sequence)

    async def list_managed(self) -> list[WorkerSnapshot]:
        output = await self._run(
            [
                self.binary,
                "ps",
                "--all",
                "--filter",
                f"label={MANAGED_LABEL}=true",
                "--format",
                "json",
            ]
        )
        if not output.strip():
            return []
        values = json.loads(output)
        snapshots: list[WorkerSnapshot] = []
        for value in values:
            external_id = value.get("Id") or value.get("ID")
            if external_id:
                inspected = await self.inspect(str(external_id))
                if inspected is not None:
                    snapshots.append(inspected)
        return snapshots

    async def stop(self, external_id: str) -> WorkerSnapshot | None:
        snapshot = await self.inspect(external_id)
        if snapshot is None:
            return None
        if snapshot.running:
            try:
                await self._run(
                    [self.binary, "stop", "--time", "3", external_id]
                )
            except RuntimeError:
                await self._run([self.binary, "kill", external_id])
        return await self.inspect(external_id)

    async def remove(self, external_id: str) -> None:
        await asyncio.to_thread(
            subprocess.run,
            [self.binary, "rm", "--force", external_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    async def _run(self, command: Sequence[str]) -> str:
        completed = await asyncio.to_thread(
            subprocess.run,
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"command failed ({completed.returncode}): {_redact_command(command)}: "
                + completed.stderr.decode("utf-8", errors="replace").strip()
            )
        return completed.stdout.decode("utf-8", errors="replace")


def _redact_command(command: Sequence[str]) -> str:
    values: list[str] = []
    redact_next = False
    for item in command:
        if redact_next:
            values.append("<redacted>")
            redact_next = False
        elif item in {"--env", "--env-file"}:
            values.append(item)
            redact_next = True
        elif item.startswith("--env="):
            values.append("--env=<redacted>")
        else:
            values.append(str(item))
    return " ".join(values)


class AgyContainerSpecFactory:
    def __init__(
        self,
        image: str = "localhost/gravityclaw-agy:1.1.13",
        home_volume: str = "gravityclaw-agy-home",
    ) -> None:
        self.image = image
        self.home_volume = home_volume

    def build(
        self, run: RunRecord, conversation: Conversation, workspace: Workspace
    ) -> ContainerSpec:
        prompt = str(
            run.request.get("execution_prompt") or run.request.get("prompt", "")
        ).strip()
        if not prompt:
            raise ValueError("run request has no prompt")
        command = ["agy", "-p", prompt, "--output-format", "stream-json"]
        backend_conversation = conversation.agy_conversation_id
        if backend_conversation:
            command.extend(["--conversation", backend_conversation])
        command.extend(["--print-timeout", str(run.request.get("print_timeout", "15m"))])
        if bool(run.request.get("allow_all", False)):
            command.append("--dangerously-skip-permissions")
        return ContainerSpec(
            run_id=run.id,
            workspace_id=workspace.id,
            workspace=workspace.path,
            image=self.image,
            command=tuple(command),
            network=True,
            home_volume=self.home_volume,
        )


class FakeContainerSpecFactory:
    def __init__(self, image: str = "localhost/gravityclaw-test-worker:latest") -> None:
        self.image = image

    def build(
        self, run: RunRecord, conversation: Conversation, workspace: Workspace
    ) -> ContainerSpec:
        scenario = str(run.request.get("scenario", "text"))
        command = ["python", "/opt/gravityclaw/fake_agent.py", scenario]
        if conversation.agy_conversation_id:
            command.extend(["--conversation", conversation.agy_conversation_id])
        delay = run.request.get("delay")
        if delay is not None:
            command.extend(["--delay", str(delay)])
        forbidden_path = run.request.get("forbidden_path")
        if forbidden_path is not None:
            command.extend(["--forbidden-path", str(forbidden_path)])
        return ContainerSpec(
            run_id=run.id,
            workspace_id=workspace.id,
            workspace=workspace.path,
            image=self.image,
            command=tuple(command),
            network=False,
            memory="256m",
            cpus=1,
            pids_limit=64,
        )


def _snapshot(value: Mapping[str, Any]) -> WorkerSnapshot:
    state = value.get("State") or {}
    labels = (value.get("Config") or {}).get("Labels") or {}
    name = str(value.get("Name", "")).lstrip("/")
    running = bool(state.get("Running", False))
    status = str(state.get("Status", "missing"))
    exit_code = None if running else state.get("ExitCode")
    return WorkerSnapshot(
        external_id=str(value.get("Id") or value.get("ID")),
        name=name,
        state=status,
        running=running,
        exit_code=int(exit_code) if exit_code is not None else None,
        labels={str(key): str(item) for key, item in labels.items()},
    )
