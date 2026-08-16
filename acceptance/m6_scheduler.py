"""Deterministic Milestone 6 scheduler crash/restart acceptance gate.

This gate deliberately kills child processes at the two SQLite boundaries that
matter most: after an occurrence lease is claimed, and after the occurrence is
atomically linked to a queued run but before the dispatcher is notified. The
parent then performs normal startup reconciliation against the same WAL DB.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from gravityclaw.channel_store import ChannelStore
from gravityclaw.scheduler import Scheduler
from gravityclaw.store import Store


class FakeManager:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.activated: list[str] = []
        self.cancelled: list[str] = []

    async def activate(self, run_id: str):
        self.activated.append(run_id)
        return self.store.get_run(run_id)

    async def cancel(self, run_id: str):
        self.cancelled.append(run_id)
        self.store.transition_run(
            run_id, "cancelled", expected=("queued", "running"), error="acceptance replacement"
        )
        return self.store.get_run(run_id)


def _kill_after_claim(database: str, trigger_id: str) -> None:
    store = Store(Path(database))
    store.initialize()
    store.claim_trigger(trigger_id, "crashed-scheduler", lease_seconds=1)
    os.kill(os.getpid(), signal.SIGKILL)


def _kill_after_dispatch(database: str, trigger_id: str, conversation_id: str) -> None:
    store = Store(Path(database))
    store.initialize()
    trigger = store.claim_trigger(trigger_id, "crashed-dispatcher", lease_seconds=60)
    assert trigger is not None
    store.submit_scheduled_run(
        trigger.id, "crashed-dispatcher", conversation_id,
        {"prompt": "crash after durable dispatch", "context_profile": "scheduled"},
    )
    os.kill(os.getpid(), signal.SIGKILL)


def _run_child(function, *args: str) -> None:
    import multiprocessing

    process = multiprocessing.Process(target=function, args=args)
    process.start()
    process.join(10)
    if process.exitcode != -signal.SIGKILL:
        raise AssertionError(f"crash boundary child did not receive SIGKILL: {process.exitcode}")


async def gate(root: Path) -> dict[str, int | bool]:
    store = Store(root / "gravityclaw.db")
    store.initialize()
    workspace = store.create_workspace("acceptance", root / "workspace")
    channels = ChannelStore(store)
    manager = FakeManager(store)
    scheduler = Scheduler(store, manager, channels, owner="acceptance", lease_seconds=1)
    due = datetime(2026, 8, 16, 10, 0, 0, tzinfo=UTC)

    # 100 independent due schedules exercise SQLite locking and trigger
    # identity generation without creating a second execution runtime.
    schedules = [
        scheduler.create_schedule(
            name=f"load-{index}", trigger_type="one_shot",
            expression=due.isoformat(), timezone="UTC", prompt=f"load {index}",
            context_profile="scheduled", workspace_id=workspace.id,
            misfire_grace_seconds=60,
        )
        for index in range(100)
    ]
    report = await scheduler.tick(due)
    assert report.dispatched == 100
    assert len(store.list_runs()) == 100
    assert len(store.list_triggers(states=("DISPATCHED",))) == 100
    await scheduler.tick(due)
    assert len(store.list_runs()) == 100, "repeat evaluation duplicated a run"

    # Crash after CLAIMED: the lease is recovered and the occurrence dispatches once.
    claim_schedule = scheduler.create_schedule(
        name="claim-crash", trigger_type="one_shot", expression=due.isoformat(),
        timezone="UTC", prompt="claim crash", context_profile="scheduled",
        workspace_id=workspace.id, misfire_grace_seconds=60,
    )
    store.materialize_triggers(
        claim_schedule.id, 1, [(due.isoformat(), "PENDING", None)],
        next_run_at=None, last_run_at=due.isoformat(),
    )
    claim_trigger = store.list_triggers(schedule_id=claim_schedule.id)[0]
    _run_child(_kill_after_claim, str(store.path), claim_trigger.id)
    time.sleep(1.2)
    reopened = Scheduler(store, manager, channels, owner="restarted", lease_seconds=1)
    assert store.recover_trigger_leases() == 1
    await reopened.tick(due)
    assert store.list_triggers(schedule_id=claim_schedule.id)[0].state == "DISPATCHED"

    # Crash after atomic scheduled-run creation: restart sees the queued run and
    # activates it, while the trigger cannot create a second run.
    dispatch_schedule = scheduler.create_schedule(
        name="dispatch-crash", trigger_type="one_shot", expression=due.isoformat(),
        timezone="UTC", prompt="dispatch crash", context_profile="scheduled",
        workspace_id=workspace.id, conversation_policy="resume", misfire_grace_seconds=60,
    )
    store.materialize_triggers(
        dispatch_schedule.id, 1, [(due.isoformat(), "PENDING", None)],
        next_run_at=None, last_run_at=due.isoformat(),
    )
    dispatch_trigger = store.list_triggers(schedule_id=dispatch_schedule.id)[0]
    conversation = store.create_conversation(
        workspace.id, channel="scheduler", channel_key=f"acceptance:{dispatch_schedule.id}"
    )
    queued_before_dispatch = len(store.list_runs(statuses=("queued",)))
    _run_child(_kill_after_dispatch, str(store.path), dispatch_trigger.id, conversation.id)
    queued = store.list_runs(statuses=("queued",))
    assert len(queued) == queued_before_dispatch + 1
    await manager.activate(queued[-1].id)
    assert store.list_triggers(schedule_id=dispatch_schedule.id)[0].state == "DISPATCHED"

    # Deleted schedules are soft-deleted and cannot resurrect on another tick.
    deleted = scheduler.create_schedule(
        name="deleted", trigger_type="one_shot", expression=due.isoformat(),
        timezone="UTC", prompt="never", context_profile="scheduled",
        workspace_id=workspace.id, misfire_grace_seconds=60,
    )
    store.delete_schedule(deleted.id)
    await reopened.tick(due)
    assert not store.list_triggers(schedule_id=deleted.id)

    with store._connect() as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        wal = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert integrity == "ok" and wal.lower() == "wal"
    return {
        "load_schedules": len(schedules),
        "total_runs": len(store.list_runs()),
        "total_trigger_decisions": len(store.list_triggers(limit=1000)),
        "sqlite_integrity": integrity == "ok",
        "wal": wal.lower() == "wal",
        "no_duplicate_load_runs": len(store.list_runs()) == 102,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    if args.root:
        args.root.mkdir(parents=True, exist_ok=True)
        result = asyncio.run(gate(args.root))
        print(result)
        return
    with tempfile.TemporaryDirectory(prefix="gravityclaw-m6-acceptance-") as directory:
        result = asyncio.run(gate(Path(directory)))
        print(result)
    print("M6_SCHEDULER_GATE_PASSED")


if __name__ == "__main__":
    main()
