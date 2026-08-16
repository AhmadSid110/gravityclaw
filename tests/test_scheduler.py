from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from gravityclaw.channel_store import ChannelStore
from gravityclaw.context import RunContextCompiler
from gravityclaw.events import AgentEvent
from gravityclaw.identity import IdentityStore
from gravityclaw.memory import MemoryService
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
            run_id, "cancelled", expected=("queued", "running"), error="test cancellation"
        )
        return self.store.get_run(run_id)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gravityclaw-m6-")
        root = Path(self.temporary.name)
        self.store = Store(root / "gravityclaw.db")
        self.store.initialize()
        self.workspace = self.store.create_workspace("test", root / "workspace")
        self.manager = FakeManager(self.store)
        self.channels = ChannelStore(self.store)
        self.scheduler = Scheduler(
            self.store, self.manager, self.channels, owner="test-scheduler", lease_seconds=1
        )

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_occurrence_identity_is_idempotent_and_uses_normal_run_path(self) -> None:
        schedule = self.scheduler.create_schedule(
            name="one shot", trigger_type="one_shot",
            expression="2026-08-16T10:00:00+00:00", timezone="UTC",
            prompt="run once", context_profile="scheduled",
            workspace_id=self.workspace.id, misfire_grace_seconds=60,
        )
        now = datetime(2026, 8, 16, 10, 0, 1, tzinfo=UTC)
        first = await self.scheduler.tick(now)
        second = await self.scheduler.tick(now)
        self.assertEqual(first.dispatched, 1)
        self.assertEqual(second.dispatched, 0)
        triggers = self.store.list_triggers(schedule_id=schedule.id)
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0].state, "DISPATCHED")
        self.assertEqual(len(self.store.list_runs()), 1)
        self.assertEqual(self.manager.activated, [triggers[0].run_id])
        self.assertEqual(self.store.get_run(triggers[0].run_id).request["execution_key"], triggers[0].execution_key)

    async def test_run_once_misfire_coalesces_without_losing_ledger_decisions(self) -> None:
        schedule = self.scheduler.create_schedule(
            name="hourly", trigger_type="interval", expression="60", timezone="UTC",
            start_at="2026-08-16T10:00:00+00:00", prompt="check",
            context_profile="scheduled", workspace_id=self.workspace.id,
            misfire_policy="MISFIRE_RUN_ONCE", misfire_grace_seconds=1000,
        )
        await self.scheduler.tick(datetime(2026, 8, 16, 10, 5, 1, tzinfo=UTC))
        states = [item.state for item in self.store.list_triggers(schedule_id=schedule.id)]
        self.assertEqual(states.count("MISSED"), 5)
        self.assertEqual(states.count("DISPATCHED"), 1)
        self.assertEqual(len(self.store.list_runs()), 1)

    async def test_heartbeat_collapses_backlog_and_skips_overlap(self) -> None:
        schedule = self.scheduler.create_schedule(
            name="heartbeat", trigger_type="heartbeat", expression="60", timezone="UTC",
            start_at="2026-08-16T10:00:00+00:00", prompt="inspect HEARTBEAT.md",
            workspace_id=self.workspace.id, misfire_grace_seconds=1000,
        )
        await self.scheduler.tick(datetime(2026, 8, 16, 10, 5, 1, tzinfo=UTC))
        triggers = self.store.list_triggers(schedule_id=schedule.id)
        self.assertEqual(sum(item.state == "SKIPPED" for item in triggers), 5)
        self.assertEqual(sum(item.state == "DISPATCHED" for item in triggers), 1)
        await self.scheduler.tick(datetime(2026, 8, 16, 10, 6, 1, tzinfo=UTC))
        self.assertEqual(len(self.store.list_runs()), 1)
        self.assertEqual(self.store.list_triggers(schedule_id=schedule.id)[-1].state, "SKIPPED")

    async def test_expired_claim_is_recovered_after_restart(self) -> None:
        schedule = self.scheduler.create_schedule(
            name="restart", trigger_type="one_shot",
            expression="2026-08-16T10:00:00+00:00", timezone="UTC",
            prompt="recover", context_profile="scheduled", workspace_id=self.workspace.id,
            misfire_grace_seconds=1000,
        )
        await self.scheduler.tick(datetime(2026, 8, 16, 10, 0, tzinfo=UTC))
        # The normal tick dispatches it. Reset a synthetic occurrence to model
        # the crash boundary before submit_scheduled_run().
        trigger = self.store.list_triggers(schedule_id=schedule.id)[0]
        run = self.store.get_run(trigger.run_id)
        self.store.transition_run(run.id, "cancelled", expected=("queued",), error="test reset")
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE trigger_occurrences SET state='CLAIMED', run_id=NULL, claim_owner='dead', "
                "lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (trigger.id,)
            )
        reopened = Scheduler(self.store, self.manager, self.channels, owner="restarted")
        self.assertEqual(self.store.recover_trigger_leases(), 1)
        await reopened.tick(datetime(2026, 8, 16, 10, 0, 1, tzinfo=UTC))
        self.assertEqual(len(self.store.list_runs()), 2)
        self.assertEqual(sum(item.state == "DISPATCHED" for item in self.store.list_triggers(schedule_id=schedule.id)), 1)

    async def test_actionable_notification_uses_durable_channel_outbox(self) -> None:
        schedule = self.scheduler.create_schedule(
            name="notify", trigger_type="one_shot",
            expression="2026-08-16T10:00:00+00:00", timezone="UTC",
            prompt="report", context_profile="scheduled", workspace_id=self.workspace.id,
            notification_policy="actionable", notification_channel="telegram",
            notification_chat_id="123", misfire_grace_seconds=1000,
        )
        await self.scheduler.tick(datetime(2026, 8, 16, 10, 0, tzinfo=UTC))
        trigger = self.store.list_triggers(schedule_id=schedule.id)[0]
        self.store.append_event(trigger.run_id, AgentEvent(
            "agent.completed", trigger.run_id, data={"response": "Action required"}
        ))
        self.store.transition_run(trigger.run_id, "completed", expected=("queued",))
        report = await self.scheduler.tick(datetime(2026, 8, 16, 10, 0, 1, tzinfo=UTC))
        self.assertEqual(report.notifications, 1)
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM channel_outbox WHERE logical_key=?",
                (f"trigger:{trigger.id}:notification",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "PENDING")

    async def test_heartbeat_context_includes_policy_without_making_it_identity(self) -> None:
        root = Path(self.temporary.name)
        identity = IdentityStore(root)
        identity.bootstrap()
        (root / "HEARTBEAT.md").write_text("Check only explicitly requested follow-ups.\n", encoding="utf-8")
        memory = MemoryService(root, self.store)
        compiler = RunContextCompiler(self.store, identity, memory)
        conversation = self.store.create_conversation(self.workspace.id)
        run = self.store.submit_run(
            conversation.id, {"prompt": "heartbeat check", "context_profile": "heartbeat"}
        )
        compiled = compiler.compile(run, conversation)
        self.assertIn("HEARTBEAT.md", compiled.included_sources)
        self.assertIn("Check only explicitly requested", compiled.prompt)
        self.assertNotIn("HEARTBEAT.md", [item.name for item in identity.load_execution_identity()])

    async def test_queue_waits_for_the_previous_run_and_disabled_schedule_does_not_run(self) -> None:
        schedule = self.scheduler.create_schedule(
            name="queued", trigger_type="interval", expression="60", timezone="UTC",
            start_at="2026-08-16T10:00:00+00:00", prompt="queue",
            context_profile="scheduled", workspace_id=self.workspace.id,
            concurrency_policy="QUEUE", misfire_policy="MISFIRE_CATCH_UP",
            misfire_grace_seconds=1000,
        )
        await self.scheduler.tick(datetime(2026, 8, 16, 10, 1, tzinfo=UTC))
        self.assertEqual(len(self.store.list_runs()), 1)
        self.assertEqual(len(self.store.list_triggers(schedule_id=schedule.id, states=("PENDING",))), 1)
        await self.scheduler.tick(datetime(2026, 8, 16, 10, 1, 1, tzinfo=UTC))
        self.assertEqual(len(self.store.list_runs()), 1)
        first = self.store.list_triggers(schedule_id=schedule.id, states=("DISPATCHED",))[0]
        self.store.transition_run(first.run_id, "completed", expected=("queued",))
        await self.scheduler.tick(datetime(2026, 8, 16, 10, 1, 2, tzinfo=UTC))
        self.assertEqual(len(self.store.list_runs()), 2)

        disabled = self.scheduler.create_schedule(
            name="disabled", trigger_type="one_shot",
            expression="2026-08-16T10:00:00+00:00", timezone="UTC", prompt="no",
            context_profile="scheduled", workspace_id=self.workspace.id,
            misfire_grace_seconds=1000,
        )
        self.store.set_schedule_enabled(disabled.id, False)
        await self.scheduler.tick(datetime(2026, 8, 16, 10, 2, tzinfo=UTC))
        self.assertEqual(self.store.list_triggers(schedule_id=disabled.id), [])


class SchedulerPureTests(unittest.TestCase):
    def test_timezone_aware_cron_and_dst(self) -> None:
        result = Scheduler.first_run_at(
            trigger_type="cron", expression="30 9 * * *", timezone="America/New_York",
            now=datetime(2026, 3, 7, 15, 0, tzinfo=UTC),
        )
        self.assertEqual(result, "2026-03-08T13:30:00+00:00")

    def test_invalid_timezone_and_expression_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Scheduler.validate(trigger_type="cron", expression="not cron", timezone="UTC", context_profile="scheduled")
        with self.assertRaises(ValueError):
            Scheduler.validate(trigger_type="interval", expression="0", timezone="UTC", context_profile="scheduled")
        with self.assertRaises(ValueError):
            Scheduler.validate(trigger_type="interval", expression="10", timezone="Mars/Base", context_profile="scheduled")


if __name__ == "__main__":
    unittest.main()
