from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from gravityclaw.context import RunContextCompiler
from gravityclaw.capabilities import CapabilityManager
from gravityclaw.execution import ContainerSpec, WorkerSnapshot
from gravityclaw.identity import IdentityStore
from gravityclaw.manager import RunManager
from gravityclaw.memory import MemoryService
from gravityclaw.store import Store


class SlowBackend:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.stopped: list[str] = []

    async def start(self, spec: ContainerSpec) -> WorkerSnapshot:
        self.started.set()
        await self.release.wait()
        return WorkerSnapshot(
            external_id="worker-slow",
            name="worker-slow",
            state="running",
            running=True,
            exit_code=None,
            labels={},
        )

    async def inspect(self, external_id: str) -> WorkerSnapshot | None:
        return None

    async def logs(self, external_id: str) -> list[object]:
        return []

    async def list_managed(self) -> list[WorkerSnapshot]:
        return []

    async def stop(self, external_id: str) -> WorkerSnapshot | None:
        self.stopped.append(external_id)
        return WorkerSnapshot(external_id, external_id, "exited", False, 143, {})

    async def remove(self, external_id: str) -> None:
        return None


class Factory:
    def __init__(self) -> None:
        self.last_request: dict[str, object] | None = None

    def build(self, run: object, conversation: object, workspace: object) -> ContainerSpec:
        self.last_request = dict(run.request)
        return ContainerSpec(
            run_id=run.id,
            workspace_id=workspace.id,
            workspace=workspace.path,
            image="test",
            command=("true",),
        )


class ManagerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gravityclaw-manager-")
        root = Path(self.temporary.name)
        self.store = Store(root / "gravityclaw.db")
        self.store.initialize()
        workspace = self.store.create_workspace("test", root / "workspace")
        self.conversation = self.store.create_conversation(workspace.id)
        self.identity = IdentityStore(root)
        self.identity.bootstrap()
        self.memory = MemoryService(root, self.store)
        self.compiler = RunContextCompiler(
            self.store, self.identity, self.memory
        )
        self.capabilities = CapabilityManager(root, self.store)
        self.backend = SlowBackend()
        self.factory = Factory()
        self.manager = RunManager(
            self.store,
            self.backend,
            self.factory,
            poll_interval=0.01,
            context_compiler=self.compiler,
            capability_manager=self.capabilities,
        )
        await self.manager.start()

    async def asyncTearDown(self) -> None:
        await self.manager.close()
        self.temporary.cleanup()

    async def test_cancel_during_container_start_cleans_late_worker(self) -> None:
        run = await self.manager.submit(self.conversation.id, {"prompt": "slow"})
        await asyncio.wait_for(self.backend.started.wait(), timeout=1)
        self.assertEqual(self.store.get_run(run.id).status, "running")
        cancelled = await self.manager.cancel(run.id)
        self.assertEqual(cancelled.status, "cancelled")
        self.backend.release.set()

        async def late_worker_was_cleaned() -> bool:
            return bool(self.backend.stopped)

        for _ in range(100):
            if await late_worker_was_cleaned():
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.backend.stopped, ["worker-slow"])
        self.assertEqual(self.store.get_worker("worker-slow").state, "terminated")

    async def test_context_is_compiled_and_persisted_before_worker_start(self) -> None:
        (self.identity.home / "SOUL.md").write_text(
            "# Soul\n\nBe concise and unflappable.\n", encoding="utf-8"
        )
        self.memory.record_episode(
            "Use SQLite WAL for lifecycle state.", source="user", confidence=1.0
        )
        run = await self.manager.submit(
            self.conversation.id, {"prompt": "Review SQLite lifecycle durability"}
        )
        await asyncio.wait_for(self.backend.started.wait(), timeout=1)
        prepared = self.store.get_run(run.id)
        self.assertIn("execution_prompt", prepared.request)
        self.assertIn("context_manifest", prepared.request)
        self.assertIn("Be concise and unflappable", prepared.request["execution_prompt"])
        self.assertIn("Use SQLite WAL", prepared.request["execution_prompt"])
        self.assertEqual(
            self.factory.last_request["execution_prompt"],
            prepared.request["execution_prompt"],
        )
        self.assertIn(
            "run.context_compiled",
            [event.event_type for event in self.store.list_events(run.id)],
        )
        self.assertEqual(
            self.store.get_capability_manifest(run.id)["workspace_id"],
            self.conversation.workspace_id,
        )
        self.backend.release.set()


if __name__ == "__main__":
    unittest.main()
