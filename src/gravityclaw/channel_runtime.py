"""Channel-neutral durable polling, delivery, presentation, and cancellation."""

from __future__ import annotations

import asyncio
import logging

from .channel_store import ChannelStore, OutboxRecord
from .channels import (
    AmbiguousDeliveryError,
    ChannelAdapter,
    ChannelDeliveryError,
    InboundMessage,
)
from .manager import RunManager
from .presentation import PresentationReducer
from .store import TERMINAL_RUN_STATUSES


LOGGER = logging.getLogger(__name__)


class ChannelRuntime:
    """Run one adapter against GravityClaw's durable channel state."""

    def __init__(
        self,
        manager: RunManager,
        channel_store: ChannelStore,
        adapter: ChannelAdapter,
        *,
        authorized_sender_id: str,
        default_workspace_alias: str | None = None,
        poll_timeout: int = 20,
        delivery_interval: float = 0.2,
        presentation_interval: float = 0.25,
    ) -> None:
        self.manager = manager
        self.channel_store = channel_store
        self.adapter = adapter
        self.authorized_sender_id = authorized_sender_id
        self.default_workspace_alias = default_workspace_alias
        self.poll_timeout = poll_timeout
        self.delivery_interval = delivery_interval
        self.presentation_interval = presentation_interval
        self.reducer = PresentationReducer()
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        self.channel_store.recover_deliveries()
        self.channel_store.recover_cancellations()
        self._tasks = [
            asyncio.create_task(self._poll_loop(), name=f"{self.adapter.name}-poll"),
            asyncio.create_task(
                self._delivery_loop(), name=f"{self.adapter.name}-delivery"
            ),
            asyncio.create_task(
                self._presentation_loop(), name=f"{self.adapter.name}-presentation"
            ),
            asyncio.create_task(self._cancellation_loop(), name="channel-cancellation"),
        ]

    async def close(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.adapter.close()

    async def ingest_authorized(self, message: InboundMessage) -> None:
        if message.sender_id != self.authorized_sender_id:
            self.channel_store.advance_cursor(message.channel, message.provider_update_id)
            return
        outcome = self.channel_store.ingest(
            message, default_workspace_alias=self.default_workspace_alias
        )
        if outcome.run_id and not outcome.duplicate:
            await self.manager.activate(outcome.run_id)

    async def deliver_once(self) -> int:
        delivered = 0
        for record in self.channel_store.claim_outbox(self.adapter.name):
            try:
                await self._deliver(record)
                delivered += 1
            except Exception:
                LOGGER.exception("unexpected channel delivery failure for %s", record.id)
                self.channel_store.mark_retry(
                    record.id, "unexpected delivery failure", ambiguous=False
                )
        return delivered

    async def reduce_once(self) -> int:
        updated = 0
        for presentation in self.channel_store.list_presentations(self.adapter.name):
            if not presentation.run_id:
                continue
            run = self.manager.store.get_run(presentation.run_id)
            events = self.manager.store.list_events(run.id)
            text, sequence = self.reducer.reduce(run, events)
            if sequence > presentation.event_sequence or text != presentation.desired_text:
                self.channel_store.update_presentation(
                    run.id,
                    text,
                    sequence,
                    throttle_seconds=0 if run.status in TERMINAL_RUN_STATUSES else 1,
                )
                updated += 1
        return updated

    async def cancel_once(self) -> int:
        handled = 0
        for request in self.channel_store.claim_cancellations():
            try:
                await self.manager.cancel(request.run_id)
                self.channel_store.finish_cancellation(request.id)
            except Exception as exc:
                self.channel_store.finish_cancellation(request.id, str(exc))
            handled += 1
        return handled

    async def _poll_loop(self) -> None:
        while not self._stopping:
            try:
                offset = self.channel_store.cursor(self.adapter.name) + 1
                updates = await self.adapter.poll(offset, self.poll_timeout)
                for update in sorted(updates, key=lambda item: item.update_id):
                    if update.message is None:
                        self.channel_store.advance_cursor(self.adapter.name, update.update_id)
                    else:
                        await self.ingest_authorized(update.message)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("channel poll failed")
                await asyncio.sleep(1)

    async def _delivery_loop(self) -> None:
        while not self._stopping:
            await self.deliver_once()
            await asyncio.sleep(self.delivery_interval)

    async def _presentation_loop(self) -> None:
        while not self._stopping:
            await self.reduce_once()
            await asyncio.sleep(self.presentation_interval)

    async def _cancellation_loop(self) -> None:
        while not self._stopping:
            await self.cancel_once()
            await asyncio.sleep(0.2)

    async def _deliver(self, record: OutboxRecord) -> None:
        try:
            if record.provider_message_id is None:
                result = await self.adapter.send_message(
                    record.chat_id,
                    record.desired_text,
                    thread_id=record.thread_key or None,
                )
                self.channel_store.mark_delivered(
                    record.id,
                    record.delivery_version,
                    record.desired_text,
                    result.message_id,
                )
            else:
                await self.adapter.edit_message(
                    record.chat_id, record.provider_message_id, record.desired_text
                )
                self.channel_store.mark_delivered(
                    record.id, record.delivery_version, record.desired_text
                )
        except ChannelDeliveryError as exc:
            if exc.already_applied and record.provider_message_id is not None:
                self.channel_store.mark_delivered(
                    record.id, record.delivery_version, record.desired_text
                )
            elif exc.retryable:
                self.channel_store.mark_retry(
                    record.id,
                    str(exc),
                    ambiguous=False,
                    retry_after=exc.retry_after or _backoff(record.attempt_count),
                )
            else:
                self.channel_store.mark_retry(
                    record.id, str(exc), ambiguous=False, retry_after=0, max_attempts=1
                )
        except AmbiguousDeliveryError as exc:
            self.channel_store.mark_retry(
                record.id,
                str(exc),
                ambiguous=True,
                retry_after=_backoff(record.attempt_count),
            )


def _backoff(attempt: int) -> float:
    return min(60.0, float(2 ** max(0, min(attempt, 6))))
