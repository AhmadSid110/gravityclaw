#!/usr/bin/env python3
"""Live Milestone 3 gate using the authenticated container-backed AGY runtime."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path

from gravityclaw.context import RunContextCompiler
from gravityclaw.event_bus import EventBus
from gravityclaw.execution import AgyContainerSpecFactory, PodmanExecutionBackend
from gravityclaw.identity import IdentityStore
from gravityclaw.manager import RunManager
from gravityclaw.memory import MemoryService
from gravityclaw.store import Store, TERMINAL_RUN_STATUSES


async def main() -> int:
    started = time.monotonic()
    worker_id: str | None = None
    backend = PodmanExecutionBackend()
    with tempfile.TemporaryDirectory(prefix="gravityclaw-m3-live-") as temporary:
        root = Path(temporary)
        home = root / "home"
        store = Store(home / "gravityclaw.db")
        store.initialize()
        identity = IdentityStore(home)
        identity.bootstrap()
        (home / "SOUL.md").write_text(
            "# Soul\n\nFor the Milestone 3 verification request, include marker "
            "IDENTITY_M3_OK in the response.\n",
            encoding="utf-8",
        )
        memory = MemoryService(home, store)
        memory_id = memory.record_episode(
            "The Milestone 3 verification memory marker is MEMORY_M3_OK.",
            source="acceptance-test",
            confidence=1.0,
        )
        compiler = RunContextCompiler(store, identity, memory)
        manager = RunManager(
            store,
            backend,
            AgyContainerSpecFactory(),
            EventBus(),
            context_compiler=compiler,
        )
        await manager.start()
        try:
            workspace = store.create_workspace("m3-live", root / "workspace")
            conversation = store.create_conversation(workspace.id, title="m3-live")
            run = await manager.submit(
                conversation.id,
                {
                    "prompt": (
                        "Use the Milestone 3 verification identity and memory. "
                        "Reply with exactly: IDENTITY_M3_OK MEMORY_M3_OK"
                    ),
                    "print_timeout": "2m",
                },
            )
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                current = store.get_run(run.id)
                worker_id = current.worker_id or worker_id
                if current.status in TERMINAL_RUN_STATUSES:
                    break
                await asyncio.sleep(0.2)
            else:
                await manager.cancel(run.id)
                raise TimeoutError("live AGY context run did not finish")

            current = store.get_run(run.id)
            messages = store.recent_messages(conversation.id)
            assistant = [item.content for item in messages if item.role == "assistant"]
            manifest = current.request.get("context_manifest", {})
            event_types = [event.event_type for event in store.list_events(run.id)]
            if current.status != "completed":
                raise AssertionError(f"live run ended as {current.status}: {current.error}")
            if not assistant or "IDENTITY_M3_OK MEMORY_M3_OK" not in assistant[-1]:
                raise AssertionError(f"markers missing from assistant response: {assistant}")
            included = manifest.get("included_sources", [])
            if "SOUL.md" not in included or f"memory:{memory_id}" not in included:
                raise AssertionError(f"context provenance missing: {included}")
            if "run.context_compiled" not in event_types:
                raise AssertionError("context compilation event was not persisted")
            print(
                json.dumps(
                    {
                        "verdict": "PASSED",
                        "status": current.status,
                        "response": assistant[-1],
                        "context_characters": manifest.get("characters"),
                        "included_sources": included,
                        "events": event_types,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            await manager.close()
            if worker_id:
                await backend.remove(worker_id)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
