from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gravityclaw.events import AgentEvent
from gravityclaw.store import Store


class StoreRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gravityclaw-store-")
        self.root = Path(self.temporary.name)
        self.database = self.root / "gravityclaw.db"
        self.store = Store(self.database)
        self.store.initialize()
        workspace = self.store.create_workspace("test", self.root / "workspace")
        self.conversation = self.store.create_conversation(workspace.id)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_gateway_restart_recovers_active_run_and_preserves_binding(self) -> None:
        backend_id = "agy-conversation-123"
        self.store.bind_backend_conversation(self.conversation.id, backend_id)
        run = self.store.start_run(self.conversation.id)
        self.store.append_event(
            run.id,
            AgentEvent(
                type="agent.started",
                run_id=run.id,
                conversation_id=backend_id,
                data={"cwd": str(self.root / "workspace")},
            ),
        )

        reopened = Store(self.database)
        reopened.initialize()
        self.assertEqual(reopened.recover_interrupted_runs(), 1)
        recovered = reopened.get_run(run.id)
        self.assertEqual(recovered.status, "interrupted")
        self.assertEqual(
            reopened.get_conversation(self.conversation.id).agy_conversation_id,
            backend_id,
        )

    def test_second_message_queues_while_one_run_is_active(self) -> None:
        first = self.store.enqueue_run(self.conversation.id, {"prompt": "first"})
        second = self.store.enqueue_run(self.conversation.id, {"prompt": "second"})
        self.assertEqual(self.store.claim_run(first.id).status, "running")
        self.assertIsNone(self.store.claim_run(second.id))
        self.assertEqual(self.store.get_run(second.id).status, "queued")

    def test_backend_binding_cannot_be_silently_replaced(self) -> None:
        self.store.bind_backend_conversation(self.conversation.id, "agy-first")
        with self.assertRaises(ValueError):
            self.store.bind_backend_conversation(self.conversation.id, "agy-second")

    def test_source_sequence_replay_is_idempotent_and_ordered(self) -> None:
        run = self.store.start_run(self.conversation.id)
        first = self.store.append_event(
            run.id,
            AgentEvent("message.delta", run.id, data={"text": "one"}),
            source_sequence=1,
        )
        replay = self.store.append_event(
            run.id,
            AgentEvent("message.delta", run.id, data={"text": "duplicate"}),
            source_sequence=1,
        )
        second = self.store.append_event(
            run.id,
            AgentEvent("message.delta", run.id, data={"text": "two"}),
            source_sequence=2,
        )
        self.assertEqual(first.id, replay.id)
        self.assertEqual([first.sequence, second.sequence], [3, 4])
        events = self.store.list_events(run.id)
        self.assertEqual([event.sequence for event in events], [1, 2, 3, 4])

    def test_terminal_transition_and_event_commit_together(self) -> None:
        run = self.store.start_run(self.conversation.id)
        self.assertTrue(
            self.store.transition_run(
                run.id, "completed", expected=("running",)
            )
        )
        self.assertFalse(
            self.store.transition_run(
                run.id, "failed", expected=("running",), error="late writer"
            )
        )
        self.assertEqual(self.store.get_run(run.id).status, "completed")
        terminal = [
            event.event_type
            for event in self.store.list_events(run.id)
            if event.event_type.startswith("run.")
            and event.event_type not in {"run.queued", "run.running"}
        ]
        self.assertEqual(terminal, ["run.completed"])

    def test_submit_and_completed_assistant_messages_are_run_scoped_and_idempotent(self) -> None:
        run = self.store.submit_run(self.conversation.id, {"prompt": "hello"})
        claimed = self.store.claim_run(run.id)
        self.assertIsNotNone(claimed)
        self.assertTrue(
            self.store.transition_run(
                run.id,
                "completed",
                expected=("running",),
                assistant_response="world",
            )
        )
        self.assertFalse(
            self.store.transition_run(
                run.id,
                "completed",
                expected=("running",),
                assistant_response="duplicate",
            )
        )
        messages = self.store.recent_messages(self.conversation.id)
        self.assertEqual([(item.role, item.content) for item in messages], [
            ("user", "hello"),
            ("assistant", "world"),
        ])
        self.assertTrue(all(item.source_run_id == run.id for item in messages))


if __name__ == "__main__":
    unittest.main()
