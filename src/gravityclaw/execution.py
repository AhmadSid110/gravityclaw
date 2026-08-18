"""Container execution backend and AGY/fake command factories."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import uuid
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
            "--ipc=private",
            "--uts=private",
            "--log-opt",
            "max-size=10mb",
            "--log-opt",
            "max-file=3",
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
        environment_file: Path | None = None
        if spec.environment:
            descriptor, filename = tempfile.mkstemp(prefix="gravityclaw-env-")
            environment_file = Path(filename)
            os.chmod(environment_file, 0o600)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    for key, value in sorted(spec.environment.items()):
                        if not key or any(char in key for char in "=\x00\r\n"):
                            raise ValueError("invalid container environment key")
                        if any(char in value for char in "\x00\r\n"):
                            raise ValueError("container environment values may not contain newlines")
                        handle.write(f"{key}={value}\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                command.extend(["--env-file", str(environment_file)])
            except Exception:
                environment_file.unlink(missing_ok=True)
                raise
        command.append(spec.image)
        command.extend(spec.command)
        try:
            output = await self._run(command)
        finally:
            if environment_file is not None:
                environment_file.unlink(missing_ok=True)
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
        try:
            output = await self._run([self.binary, "logs", external_id])
        except RuntimeError as exc:
            # The worker can disappear between monitor inspection and log
            # ingestion. Let the monitor's following inspect call classify it
            # as interrupted instead of turning a normal race into a stuck run.
            if "no container with name or ID" in str(exc):
                return []
            raise
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
        binary: str = "agy",
    ) -> None:
        self.image = image
        self.home_volume = home_volume
        self.binary = binary

    def build(
        self, run: RunRecord, conversation: Conversation, workspace: Workspace
    ) -> ContainerSpec:
        prompt = str(
            run.request.get("execution_prompt") or run.request.get("prompt", "")
        ).strip()
        if not prompt:
            raise ValueError("run request has no prompt")
        command = [self.binary, "-p", prompt, "--output-format", "stream-json"]
        resolved_model = run.request.get("resolved_model")
        if resolved_model:
            command.extend(["--model", str(resolved_model)])
        effort = run.request.get("effort")
        if effort:
            command.extend(["--effort", str(effort)])
        backend_conversation = conversation.agy_conversation_id
        if backend_conversation:
            command.extend(["--conversation", backend_conversation])
        command.extend(["--print-timeout", str(run.request.get("print_timeout", "120m"))])
        allow_all = run.request.get("allow_all")
        if allow_all is None or bool(allow_all):
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


class _HostWorker:
    def __init__(
        self,
        external_id: str,
        run_id: str,
        workspace_id: str,
        name: str,
        process: asyncio.subprocess.Process,
        labels: dict[str, str],
    ) -> None:
        self.external_id = external_id
        self.run_id = run_id
        self.workspace_id = workspace_id
        self.name = name
        self.process = process
        self.labels = labels
        self._envelopes: list[WorkerEnvelope] = []
        self._seq = 1
        self._lock = asyncio.Lock()
        self._pump_tasks: list[asyncio.Task[None]] = []

    def snapshot(self) -> WorkerSnapshot:
        running = self.process.returncode is None
        state = "running" if running else ("exited" if self.process.returncode == 0 else "failed")
        return WorkerSnapshot(
            external_id=self.external_id,
            name=self.name,
            state=state,
            running=running,
            exit_code=self.process.returncode,
            labels=dict(self.labels),
        )

    def start_log_pumps(self) -> None:
        if self.process.stdout is not None:
            self._pump_tasks.append(asyncio.create_task(self._pump_stdout(self.process.stdout)))
        if self.process.stderr is not None:
            self._pump_tasks.append(asyncio.create_task(self._pump_stderr(self.process.stderr)))
        self._pump_tasks.append(asyncio.create_task(self._wait_exit()))

    async def _pump_stdout(self, stream: asyncio.StreamReader) -> None:
        while True:
            line_bytes = await stream.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue
            async with self._lock:
                seq = self._seq
                self._seq += 1
                self._envelopes.append(WorkerEnvelope(source_sequence=seq, source="stdout", line=line))

    async def _pump_stderr(self, stream: asyncio.StreamReader) -> None:
        while True:
            line_bytes = await stream.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue
            async with self._lock:
                seq = self._seq
                self._seq += 1
                self._envelopes.append(WorkerEnvelope(source_sequence=seq, source="stderr", line=line))

    async def _wait_exit(self) -> None:
        exit_code = await self.process.wait()
        async with self._lock:
            seq = self._seq
            self._seq += 1
            self._envelopes.append(WorkerEnvelope(source_sequence=seq, source="worker", exit_code=exit_code))

    def get_envelopes(self) -> list[WorkerEnvelope]:
        return list(self._envelopes)

    async def stop(self, grace_seconds: float = 3.0) -> None:
        if self.process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            self.process.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(self.process.wait(), timeout=grace_seconds)
            return
        except TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError):
            self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=grace_seconds)
            return
        except TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError):
            self.process.kill()
        await self.process.wait()

    async def cleanup(self) -> None:
        await self.stop()
        for t in self._pump_tasks:
            if not t.done():
                t.cancel()


class HostExecutionBackend:
    """Direct host subprocess execution backend executing on the VPS host."""

    def __init__(self) -> None:
        self._workers: dict[str, _HostWorker] = {}
        self._lock = asyncio.Lock()

    async def start(self, spec: ContainerSpec) -> WorkerSnapshot:
        external_id = str(uuid.uuid4())
        name = f"gravityclaw-{spec.run_id}"
        env = os.environ.copy()
        env.update(spec.environment)

        # Ensure PATH includes standard user and system binary directories
        path_items: list[str] = [
            str(Path.home() / ".local" / "bin"),
            str(Path.home() / ".cargo" / "bin"),
            "/usr/local/sbin",
            "/usr/local/bin",
            "/usr/sbin",
            "/usr/bin",
            "/sbin",
            "/bin",
            "/snap/bin",
        ]
        current_path = env.get("PATH", "")
        if current_path:
            for item in current_path.split(os.pathsep):
                if item and item not in path_items:
                    path_items.append(item)
        env["PATH"] = os.pathsep.join(path_items)

        workspace_dir = spec.workspace.resolve()
        workspace_dir.mkdir(parents=True, exist_ok=True)

        executable = shutil.which(spec.command[0], path=env["PATH"]) or spec.command[0]
        exec_command = [executable, *spec.command[1:]]

        process = await asyncio.create_subprocess_exec(
            *exec_command,
            cwd=workspace_dir,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        worker = _HostWorker(
            external_id=external_id,
            run_id=spec.run_id,
            workspace_id=spec.workspace_id,
            name=name,
            process=process,
            labels={
                MANAGED_LABEL: "true",
                RUN_LABEL: spec.run_id,
                WORKSPACE_LABEL: spec.workspace_id,
            },
        )
        async with self._lock:
            self._workers[external_id] = worker

        worker.start_log_pumps()
        return worker.snapshot()

    async def inspect(self, external_id: str) -> WorkerSnapshot | None:
        async with self._lock:
            worker = self._workers.get(external_id)
        if worker is None:
            return None
        return worker.snapshot()

    async def logs(self, external_id: str) -> list[WorkerEnvelope]:
        async with self._lock:
            worker = self._workers.get(external_id)
        if worker is None:
            return []
        return worker.get_envelopes()

    async def list_managed(self) -> list[WorkerSnapshot]:
        async with self._lock:
            workers = list(self._workers.values())
        return [w.snapshot() for w in workers]

    async def stop(self, external_id: str) -> WorkerSnapshot | None:
        async with self._lock:
            worker = self._workers.get(external_id)
        if worker is None:
            return None
        await worker.stop()
        return worker.snapshot()

    async def remove(self, external_id: str) -> None:
        async with self._lock:
            worker = self._workers.pop(external_id, None)
        if worker is not None:
            await worker.cleanup()


class AgyHostSpecFactory:
    """Build command specifications for direct host AGY execution."""

    def __init__(self, binary: str = "agy") -> None:
        self.binary = binary

    def build(
        self, run: RunRecord, conversation: Conversation, workspace: Workspace
    ) -> ContainerSpec:
        prompt = str(
            run.request.get("execution_prompt") or run.request.get("prompt", "")
        ).strip()
        if not prompt:
            raise ValueError("run request has no prompt")

        resolved_binary = shutil.which(self.binary)
        if not resolved_binary:
            candidate = Path.home() / ".local" / "bin" / self.binary
            if candidate.is_file() and os.access(candidate, os.X_OK):
                resolved_binary = str(candidate)
            else:
                resolved_binary = self.binary

        command = [resolved_binary, "-p", prompt, "--output-format", "stream-json"]
        resolved_model = run.request.get("resolved_model")
        if resolved_model:
            command.extend(["--model", str(resolved_model)])
        effort = run.request.get("effort")
        if effort:
            command.extend(["--effort", str(effort)])
        backend_conversation = conversation.agy_conversation_id
        if backend_conversation:
            command.extend(["--conversation", backend_conversation])
        command.extend(["--print-timeout", str(run.request.get("print_timeout", "120m"))])
        allow_all = run.request.get("allow_all")
        if allow_all is None or bool(allow_all):
            command.append("--dangerously-skip-permissions")
        return ContainerSpec(
            run_id=run.id,
            workspace_id=workspace.id,
            workspace=workspace.path,
            image="host",
            command=tuple(command),
            network=True,
        )


class FakeHostSpecFactory:
    """Build command specifications for direct host fake runner execution."""

    def __init__(self, script_path: str = "/opt/gravityclaw/fake_agent.py") -> None:
        self.script_path = script_path

    def build(
        self, run: RunRecord, conversation: Conversation, workspace: Workspace
    ) -> ContainerSpec:
        scenario = str(run.request.get("scenario", "text"))
        command = ["python", self.script_path, scenario]
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
            image="host",
            command=tuple(command),
            network=True,
        )

