from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from gravityclaw.channel_store import ChannelStore
from gravityclaw.channel_runtime import ChannelRuntime
from gravityclaw.channels import (
    AmbiguousDeliveryError,
    ChannelDeliveryError,
    InboundMessage,
    PolledUpdate,
    ProviderMessage,
)
from gravityclaw.presentation import PresentationReducer
from gravityclaw.store import Store
from gravityclaw.telegram import TelegramAdapter


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
        run = self.store.get_run(run_id)
        if run.status in {"queued", "running"}:
            self.store.transition_run(
                run_id, "cancelled", expected=(run.status,), error="test cancellation"
            )
        return self.store.get_run(run_id)


class FakeAdapter:
    name = "telegram"

    def __init__(self) -> None:
        self.updates: list[PolledUpdate] = []
        self.sent: list[tuple[str, str, str]] = []
        self.remote: dict[str, str] = {}
        self.edits: list[tuple[str, str]] = []
        self.send_ambiguous = False
        self.edit_ambiguous_once = False
        self.closed = False

    async def poll(self, offset: int, timeout: int) -> list[PolledUpdate]:
        return [update for update in self.updates if update.update_id >= offset]

    async def send_message(
        self, chat_id: str, text: str, *, thread_id: str | None = None
    ) -> ProviderMessage:
        message_id = str(len(self.sent) + 1)
        self.sent.append((chat_id, message_id, text))
        self.remote[message_id] = text
        if self.send_ambiguous:
            raise AmbiguousDeliveryError("accepted then connection lost")
        return ProviderMessage(message_id)

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> None:
        self.edits.append((message_id, text))
        self.remote[message_id] = text
        if self.edit_ambiguous_once:
            self.edit_ambiguous_once = False
            raise AmbiguousDeliveryError("edit accepted then connection lost")

    async def close(self) -> None:
        self.closed = True


class ChannelStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gravityclaw-channel-")
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "gravityclaw.db")
        self.store.initialize()
        workspace = self.store.create_workspace("GravityClaw", self.root / "workspace")
        self.channels = ChannelStore(self.store)
        self.channels.set_workspace_alias("gravityclaw", workspace.id)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def message(self, update_id: int, text: str, sender: str = "42") -> InboundMessage:
        return InboundMessage(
            "telegram", update_id, sender, "100", text, provider_message_id=str(update_id)
        )

    def test_inbound_dedupe_cursor_and_run_creation_are_atomic(self) -> None:
        first = self.channels.ingest(
            self.message(10, "hello"), default_workspace_alias="gravityclaw"
        )
        duplicate = self.channels.ingest(
            self.message(10, "hello"), default_workspace_alias="gravityclaw"
        )
        self.assertFalse(first.duplicate)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(first.run_id, duplicate.run_id)
        self.assertEqual(self.channels.cursor("telegram"), 10)
        self.assertEqual(len(self.store.list_runs()), 1)
        run = self.store.get_run(first.run_id)
        self.assertEqual(run.request["channel_inbox_id"], first.inbox_id)
        self.assertEqual(
            [
                (item.role, item.content)
                for item in self.store.recent_messages(first.conversation_id)
            ],
            [("user", "hello")],
        )

    def test_workspace_paths_are_never_accepted_from_telegram(self) -> None:
        outcome = self.channels.ingest(self.message(1, "/workspace /home/ubuntu"))
        outbox = self.channels.get_outbox(outcome.outbox_id)
        self.assertIn("Unknown workspace", outbox.desired_text)
        self.assertIsNone(outcome.conversation_id)
        self.assertEqual(self.store.list_runs(), [])

    def test_new_status_and_workspace_commands_do_not_enter_agent_pipeline(self) -> None:
        selected = self.channels.ingest(self.message(1, "/workspace gravityclaw"))
        created = self.channels.ingest(self.message(2, "/new"))
        status = self.channels.ingest(self.message(3, "/status"))
        self.assertNotEqual(selected.conversation_id, created.conversation_id)
        self.assertEqual(self.channels.get_outbox(status.outbox_id).desired_text,
                         "No runs in the current conversation.")
        self.assertEqual(self.store.list_runs(), [])

    def test_stop_is_durable_before_worker_signal(self) -> None:
        run_outcome = self.channels.ingest(
            self.message(1, "long task"), default_workspace_alias="gravityclaw"
        )
        stop = self.channels.ingest(self.message(2, "/stop"))
        self.assertEqual(stop.run_id, run_outcome.run_id)
        self.assertEqual(self.store.get_run(run_outcome.run_id).status, "queued")
        with self.store._connect() as connection:
            status = connection.execute(
                "SELECT status FROM cancellation_requests WHERE run_id=?",
                (run_outcome.run_id,),
            ).fetchone()[0]
        self.assertEqual(status, "PENDING")

    def test_expired_delivery_lease_uses_safe_recovery_policy(self) -> None:
        outcome = self.channels.ingest(
            self.message(1, "hello"), default_workspace_alias="gravityclaw"
        )
        claimed = self.channels.claim_outbox("telegram", lease_seconds=-1)[0]
        self.assertIsNone(claimed.provider_message_id)
        self.assertEqual(self.channels.claim_outbox("telegram"), [])
        self.assertEqual(self.channels.get_outbox(outcome.outbox_id).status, "UNCERTAIN")


class ChannelRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gravityclaw-runtime-")
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "gravityclaw.db")
        self.store.initialize()
        workspace = self.store.create_workspace("GravityClaw", self.root / "workspace")
        self.channels = ChannelStore(self.store)
        self.channels.set_workspace_alias("gravityclaw", workspace.id)
        self.manager = FakeManager(self.store)
        self.adapter = FakeAdapter()
        self.runtime = ChannelRuntime(
            self.manager,
            self.channels,
            self.adapter,
            authorized_sender_id="42",
            default_workspace_alias="gravityclaw",
        )

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    def message(self, update_id: int, text: str, sender: str = "42") -> InboundMessage:
        return InboundMessage("telegram", update_id, sender, "100", text)

    async def test_authorization_precedes_persistence_and_duplicate_activation(self) -> None:
        await self.runtime.ingest_authorized(self.message(1, "steal", sender="666"))
        with self.store._connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM channel_inbox").fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(self.channels.cursor("telegram"), 1)

        message = self.message(2, "hello")
        await self.runtime.ingest_authorized(message)
        await self.runtime.ingest_authorized(message)
        self.assertEqual(len(self.manager.activated), 1)

    async def test_ambiguous_initial_send_is_not_blindly_retried(self) -> None:
        await self.runtime.ingest_authorized(self.message(1, "hello"))
        outbox = self.channels.list_presentations("telegram")[0]
        self.adapter.send_ambiguous = True
        await self.runtime.deliver_once()
        self.assertEqual(self.channels.get_outbox(outbox.id).status, "UNCERTAIN")
        await self.runtime.deliver_once()
        self.assertEqual(len(self.adapter.sent), 1)

    async def test_final_edit_after_lost_ack_retries_without_duplicate_message(self) -> None:
        await self.runtime.ingest_authorized(self.message(1, "hello"))
        await self.runtime.deliver_once()
        outbox = self.channels.list_presentations("telegram")[0]
        self.assertEqual(outbox.status, "DELIVERED")
        self.channels.update_presentation(
            outbox.run_id, "✓ Completed\n\nAgent:\ndone", 99, throttle_seconds=0
        )
        self.adapter.edit_ambiguous_once = True
        await self.runtime.deliver_once()
        retry = self.channels.get_outbox(outbox.id)
        self.assertEqual(retry.status, "RETRY_WAIT")
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE channel_outbox SET available_at='2000-01-01T00:00:00+00:00' "
                "WHERE id=?",
                (outbox.id,),
            )
        await self.runtime.deliver_once()
        delivered = self.channels.get_outbox(outbox.id)
        self.assertEqual(delivered.status, "DELIVERED")
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(self.adapter.remote[delivered.provider_message_id], delivered.desired_text)

    async def test_cancellation_request_recovers_and_completes_idempotently(self) -> None:
        await self.runtime.ingest_authorized(self.message(1, "long task"))
        await self.runtime.ingest_authorized(self.message(2, "/stop"))
        self.channels.recover_cancellations()
        self.assertEqual(await self.runtime.cancel_once(), 1)
        self.assertEqual(len(self.manager.cancelled), 1)
        self.assertEqual(await self.runtime.cancel_once(), 0)
        self.assertEqual(self.store.get_run(self.manager.cancelled[0]).status, "cancelled")

    async def test_presentation_reducer_uses_persisted_events(self) -> None:
        await self.runtime.ingest_authorized(self.message(1, "hello"))
        run_id = self.manager.activated[0]
        self.store.claim_run(run_id)
        from gravityclaw.events import AgentEvent

        self.store.append_event(
            run_id, AgentEvent("tool.started", run_id, data={"tool_name": "run_command"})
        )
        self.store.append_event(
            run_id, AgentEvent("message.delta", run_id, data={"text_delta": "working"})
        )
        self.assertEqual(await self.runtime.reduce_once(), 1)
        presentation = self.channels.list_presentations("telegram")[0]
        self.assertIn("⚙ run_command", presentation.desired_text)
        self.assertIn("working", presentation.desired_text)


class TelegramAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_poll_normalizes_only_supported_text_updates(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertNotIn("secret-token", str(request.content))
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 7,
                            "message": {
                                "message_id": 3,
                                "date": 1,
                                "text": "hello",
                                "from": {"id": 42},
                                "chat": {"id": 100, "type": "private"},
                            },
                        },
                        {"update_id": 8, "edited_message": {}},
                    ],
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = TelegramAdapter("secret-token", client, api_root="https://test")
        updates = await adapter.poll(7, 1)
        self.assertEqual([item.update_id for item in updates], [7, 8])
        self.assertEqual(updates[0].message.sender_id, "42")
        self.assertIsNone(updates[1].message)
        await client.aclose()

    async def test_delivery_errors_never_expose_token(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadError("connection lost", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = TelegramAdapter("secret-token", client, api_root="https://test")
        with self.assertRaises(AmbiguousDeliveryError) as raised:
            await adapter.send_message("100", "hello")
        self.assertNotIn("secret-token", str(raised.exception))
        await client.aclose()

    async def test_not_modified_is_classified_as_already_applied(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: message is not modified",
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = TelegramAdapter("secret-token", client, api_root="https://test")
        with self.assertRaises(ChannelDeliveryError) as raised:
            await adapter.edit_message("100", "1", "same")
        self.assertTrue(raised.exception.already_applied)
        await client.aclose()


if __name__ == "__main__":
    unittest.main()
