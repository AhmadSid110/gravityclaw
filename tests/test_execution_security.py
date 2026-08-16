from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gravityclaw.execution import ContainerSpec, PodmanExecutionBackend


class ProbePodman(PodmanExecutionBackend):
    def __init__(self) -> None:
        super().__init__(binary="podman")
        self.commands: list[list[str]] = []
        self.env_file_content: str | None = None

    async def _run(self, command: list[str]) -> str:
        self.commands.append(command)
        if command[1] == "run":
            env_file = Path(command[command.index("--env-file") + 1])
            self.env_file_content = env_file.read_text(encoding="utf-8")
            return "container-id\n"
        return json.dumps([{
            "Id": "container-id", "Name": "/gravityclaw-run",
            "State": {"Status": "running", "Running": True},
            "Config": {"Labels": {"io.gravityclaw.managed": "true"}},
        }])


class MissingLogsPodman(ProbePodman):
    async def _run(self, command: list[str]) -> str:
        if command[1] == "logs":
            raise RuntimeError("command failed (125): podman logs missing: Error: no container with name or ID missing found")
        return await super()._run(command)


class ExecutionSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_command_has_isolation_and_secret_not_in_arguments(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gravityclaw-exec-") as temporary:
            root = Path(temporary)
            spec = ContainerSpec(
                run_id="run-1", workspace_id="workspace-1", workspace=root,
                image="test:image", command=("echo", "ok"),
                environment={"GRAVITYCLAW_SECRET_TOKEN": "must-not-be-an-argument"},
            )
            backend = ProbePodman()
            snapshot = await backend.start(spec)
            command = backend.commands[0]
            rendered = " ".join(command)
            self.assertEqual(snapshot.external_id, "container-id")
            self.assertNotIn("must-not-be-an-argument", rendered)
            self.assertIn("--env-file", command)
            self.assertEqual(backend.env_file_content, "GRAVITYCLAW_SECRET_TOKEN=must-not-be-an-argument\n")
            self.assertFalse(Path(command[command.index("--env-file") + 1]).exists())
            for flag in ("--read-only", "--cap-drop=all", "--security-opt=no-new-privileges", "--ipc=private", "--uts=private", "--pids-limit", "--memory", "--cpus"):
                self.assertIn(flag, command)

    async def test_environment_newlines_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gravityclaw-exec-") as temporary:
            root = Path(temporary)
            spec = ContainerSpec(
                run_id="run-1", workspace_id="workspace-1", workspace=root,
                image="test:image", command=("echo", "ok"),
                environment={"TOKEN": "line1\nline2"},
            )
            with self.assertRaises(ValueError):
                await ProbePodman().start(spec)

    async def test_disappeared_worker_logs_are_reconciled_by_following_inspect(self) -> None:
        self.assertEqual(await MissingLogsPodman().logs("missing"), [])


if __name__ == "__main__":
    unittest.main()
