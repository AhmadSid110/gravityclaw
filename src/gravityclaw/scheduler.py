"""Durable heartbeat and schedule orchestration.

The scheduler only decides *when* to create work. Every dispatched occurrence
becomes a normal GravityClaw queued run and therefore uses the existing context,
conversation, isolation, and AGY execution path.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from .channel_store import ChannelStore
from .manager import RunManager
from .store import (
    MISFIRE_POLICIES,
    SCHEDULE_CONCURRENCY,
    SCHEDULE_TYPES,
    ScheduleRecord,
    RunRecord,
    Store,
    TriggerRecord,
)


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SchedulerReport:
    materialized: int = 0
    dispatched: int = 0
    skipped: int = 0
    missed: int = 0
    cancelled_for_replace: int = 0
    notifications: int = 0


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("scheduler timestamps must include a timezone")
    return parsed.astimezone(UTC)


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("scheduler timestamps must include a timezone")
    return value.astimezone(UTC).isoformat()


class Scheduler:
    def __init__(
        self,
        store: Store,
        manager: RunManager,
        channel_store: ChannelStore | None = None,
        *,
        poll_interval: float = 1.0,
        lease_seconds: int = 60,
        owner: str | None = None,
    ) -> None:
        self.store = store
        self.manager = manager
        self.channel_store = channel_store
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self.owner = owner or f"scheduler:{uuid.uuid4()}"
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    @staticmethod
    def validate(
        *, trigger_type: str, expression: str, timezone: str,
        context_profile: str,
    ) -> None:
        if trigger_type not in SCHEDULE_TYPES:
            raise ValueError(f"invalid schedule type: {trigger_type}")
        if context_profile not in {"chat", "coding", "scheduled", "heartbeat"}:
            raise ValueError(f"invalid context profile: {context_profile}")
        if trigger_type == "heartbeat" and context_profile != "heartbeat":
            raise ValueError("heartbeat schedules must use the heartbeat context profile")
        if trigger_type != "heartbeat" and context_profile == "heartbeat":
            raise ValueError("heartbeat context profile is reserved for heartbeat schedules")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {timezone}") from exc
        expression = expression.strip()
        if trigger_type in {"interval", "heartbeat"}:
            if int(expression) <= 0:
                raise ValueError("interval seconds must be positive")
        elif trigger_type == "cron" and not croniter.is_valid(expression):
            raise ValueError("invalid cron expression")
        elif trigger_type == "one_shot":
            parse_time(expression)

    @classmethod
    def first_run_at(
        cls, *, trigger_type: str, expression: str, timezone: str,
        start_at: str | None = None, now: datetime | None = None,
    ) -> str | None:
        cls.validate(
            trigger_type=trigger_type, expression=expression,
            timezone=timezone, context_profile=("heartbeat" if trigger_type == "heartbeat" else "scheduled"),
        )
        if trigger_type == "one_shot":
            return utc_iso(parse_time(expression))
        if start_at is not None:
            return utc_iso(parse_time(start_at))
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if trigger_type in {"interval", "heartbeat"}:
            return utc_iso(current + timedelta(seconds=int(expression)))
        local = current.astimezone(ZoneInfo(timezone))
        return utc_iso(croniter(expression, local).get_next(datetime))

    def create_schedule(self, **kwargs: Any) -> ScheduleRecord:
        trigger_type = str(kwargs["trigger_type"])
        expression = str(kwargs["expression"])
        timezone = str(kwargs["timezone"])
        profile = str(kwargs.get("context_profile", "heartbeat" if trigger_type == "heartbeat" else "scheduled"))
        self.validate(
            trigger_type=trigger_type, expression=expression,
            timezone=timezone, context_profile=profile,
        )
        if kwargs.get("next_run_at") is None:
            kwargs["next_run_at"] = self.first_run_at(
                trigger_type=trigger_type, expression=expression,
                timezone=timezone, start_at=kwargs.get("start_at"),
            )
        kwargs["context_profile"] = profile
        kwargs.pop("start_at", None)
        if trigger_type == "heartbeat":
            kwargs.setdefault("misfire_policy", "MISFIRE_SKIP")
            kwargs.setdefault("concurrency_policy", "SKIP")
        return self.store.create_schedule(**kwargs)

    def update_schedule(self, schedule_id: str, **kwargs: Any) -> ScheduleRecord:
        trigger_type = str(kwargs["trigger_type"])
        profile = str(kwargs.get("context_profile") or ("heartbeat" if trigger_type == "heartbeat" else "scheduled"))
        self.validate(
            trigger_type=trigger_type, expression=str(kwargs["expression"]),
            timezone=str(kwargs["timezone"]), context_profile=profile,
        )
        next_run_at = self.first_run_at(
            trigger_type=trigger_type, expression=str(kwargs["expression"]),
            timezone=str(kwargs["timezone"]), start_at=kwargs.get("start_at"),
        )
        if trigger_type == "heartbeat":
            kwargs["misfire_policy"] = "MISFIRE_SKIP"
            kwargs["concurrency_policy"] = "SKIP"
        kwargs["context_profile"] = profile
        kwargs.pop("start_at", None)
        return self.store.update_schedule(schedule_id, next_run_at=next_run_at, **kwargs)

    async def run_now(self, schedule_id: str, request_id: str) -> tuple[TriggerRecord, RunRecord | None]:
        if not request_id.strip():
            raise ValueError("run-now request id must not be empty")
        self.store.recover_trigger_leases()
        self.store.sync_trigger_states()
        self.store.create_manual_trigger(schedule_id, request_id.strip())
        await self._dispatch_due()
        self.store.sync_trigger_states()
        candidates = self.store.list_triggers(schedule_id=schedule_id, limit=10000)
        selected = next(
            item for item in candidates
            if item.execution_key.endswith(f":manual:{request_id.strip()}")
        )
        return selected, self.store.get_run(selected.run_id) if selected.run_id else None

    async def start(self) -> SchedulerReport:
        self.store.initialize()
        self._stopping = False
        report = await self.tick()
        self._task = asyncio.create_task(self._loop(), name="gravityclaw-scheduler")
        return report

    async def close(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("scheduler tick failed")
            await asyncio.sleep(self.poll_interval)

    async def tick(self, now: datetime | None = None) -> SchedulerReport:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        report = SchedulerReport()
        self.store.recover_trigger_leases()
        self.store.sync_trigger_states()
        for schedule in self.store.list_schedules():
            if not schedule.enabled or schedule.next_run_at is None:
                continue
            if parse_time(schedule.next_run_at) > current:
                continue
            materialized = self._materialize(schedule, current)
            report = _add_report(
                report, materialized=materialized.materialized,
                skipped=materialized.skipped, missed=materialized.missed,
            )
        dispatched, skipped, cancelled = await self._dispatch_due()
        report = _add_report(report, dispatched=dispatched, skipped=skipped,
                             cancelled_for_replace=cancelled)
        notifications = await self._process_notifications()
        return _add_report(report, notifications=notifications)

    def _materialize(self, schedule: ScheduleRecord, now: datetime) -> SchedulerReport:
        cursor = parse_time(schedule.next_run_at or utc_iso(now))
        due: list[datetime] = []
        for _ in range(1001):
            if cursor > now:
                break
            due.append(cursor)
            following = _next_fire(schedule, cursor)
            if following is None:
                cursor = now + timedelta(seconds=1)
                break
            cursor = following
        if len(due) > 1000:
            raise RuntimeError(f"schedule {schedule.id} exceeded occurrence safety limit")
        decisions: list[tuple[str, str, str | None]] = []
        if schedule.misfire_policy == "MISFIRE_SKIP":
            # A normal occurrence is still runnable. Only genuinely late
            # occurrences are skipped; heartbeat backlog is collapsed to its
            # newest occurrence so it never accumulates.
            for index, item in enumerate(due):
                late = _outside_grace(item, now, schedule)
                if schedule.trigger_type == "heartbeat" and index < len(due) - 1:
                    decisions.append((utc_iso(item), "SKIPPED", "heartbeat backlog collapsed"))
                else:
                    decisions.append((
                        utc_iso(item), "MISSED" if late else "PENDING",
                        "misfire grace exceeded" if late else None,
                    ))
        elif schedule.misfire_policy == "MISFIRE_RUN_ONCE":
            for item in due[:-1]:
                decisions.append((utc_iso(item), "MISSED", "coalesced by run-once misfire policy"))
            if due:
                state = "MISSED" if _outside_grace(due[-1], now, schedule) else "PENDING"
                decisions.append((utc_iso(due[-1]), state, "outside misfire grace" if state == "MISSED" else None))
        else:
            decisions = [
                (utc_iso(item), "MISSED" if _outside_grace(item, now, schedule) else "PENDING",
                 "outside misfire grace" if _outside_grace(item, now, schedule) else None)
                for item in due
            ]
        next_run = None if schedule.trigger_type == "one_shot" else utc_iso(cursor)
        inserted = self.store.materialize_triggers(
            schedule.id, schedule.generation, decisions,
            next_run_at=next_run, last_run_at=utc_iso(due[-1]) if due else schedule.last_run_at,
        )
        return SchedulerReport(
            materialized=len(inserted),
            skipped=sum(1 for item in inserted if item.state == "SKIPPED"),
            missed=sum(1 for item in inserted if item.state == "MISSED"),
        )

    async def _dispatch_due(self) -> tuple[int, int, int]:
        dispatched = skipped = cancelled = 0
        schedules = self.store.list_schedules()
        for schedule in schedules:
            if not schedule.enabled:
                continue
            pending = self.store.list_triggers(schedule_id=schedule.id, states=("PENDING",))
            if not pending:
                continue
            active = self.store.list_triggers(
                schedule_id=schedule.id, states=("CLAIMED", "DISPATCHED", "RUNNING")
            )
            if active and schedule.concurrency_policy == "SKIP":
                for trigger in pending:
                    if self.store.decide_trigger(trigger.id, "SKIPPED", "concurrency policy skips overlap"):
                        skipped += 1
                continue
            if active and schedule.concurrency_policy == "QUEUE":
                continue
            if active and schedule.concurrency_policy == "REPLACE":
                for trigger in active:
                    if trigger.run_id:
                        await self.manager.cancel(trigger.run_id)
                        cancelled += 1
                self.store.sync_trigger_states()
                active = self.store.list_triggers(
                    schedule_id=schedule.id, states=("CLAIMED", "DISPATCHED", "RUNNING")
                )
                if active:
                    continue
            for trigger in pending:
                if self.store.count_active_triggers(schedule.id) and schedule.concurrency_policy != "QUEUE":
                    break
                claimed = self.store.claim_trigger(
                    trigger.id, self.owner, lease_seconds=self.lease_seconds
                )
                if claimed is None:
                    continue
                try:
                    conversation = self._conversation(schedule, trigger)
                    run = self.store.submit_scheduled_run(
                        trigger.id, self.owner, conversation.id,
                        {
                            "prompt": schedule.prompt,
                            "context_profile": schedule.context_profile,
                            "schedule_id": schedule.id,
                            "trigger_type": schedule.trigger_type,
                        },
                    )
                    await self.manager.activate(run.id)
                    dispatched += 1
                    if schedule.concurrency_policy == "QUEUE":
                        break
                except Exception as exc:
                    LOGGER.exception("scheduled dispatch failed for %s", trigger.id)
                    self.store.decide_trigger(trigger.id, "FAILED", f"dispatch failed: {exc}", expected=("CLAIMED",))
        return dispatched, skipped, cancelled

    def _conversation(self, schedule: ScheduleRecord, trigger: TriggerRecord):
        key = (f"schedule:{schedule.id}" if schedule.conversation_policy == "resume"
               else f"schedule:{schedule.id}:{trigger.id}")
        existing = self.store.get_conversation_by_channel_key("scheduler", key)
        if existing is not None:
            return existing
        workspace = self.store.get_workspace(schedule.workspace_id)
        return self.store.create_conversation(
            workspace.id, channel="scheduler", channel_key=key, title=schedule.name
        )

    async def _process_notifications(self) -> int:
        created = 0
        for trigger in self.store.list_triggers(states=("COMPLETED",), limit=1000):
            if trigger.run_id is None or not self.store.claim_trigger_notification(trigger.id):
                continue
            schedule = self.store.get_schedule(trigger.schedule_id, include_deleted=True)
            outbox_id: str | None = None
            try:
                run = self.store.get_run(trigger.run_id)
                response = _completed_response(self.store, run.id)
                actionable = (
                    schedule.notification_policy == "actionable" and bool(response.strip())
                    and response.strip().upper() not in {"HEARTBEAT_OK", "NO_ACTION", "NO ACTION"}
                )
                if actionable and self.channel_store and schedule.notification_channel and schedule.notification_chat_id:
                    outbox_id = self.channel_store.enqueue_scheduled_notification(
                        channel=schedule.notification_channel,
                        chat_id=schedule.notification_chat_id,
                        text=response.strip(), logical_key=f"trigger:{trigger.id}:notification",
                    )
                    created += 1
                self.store.finish_trigger_notification(trigger.id, outbox_id)
            except Exception:
                LOGGER.exception("notification policy failed for %s", trigger.id)
                # Leave PROCESSING durable; reconciliation can inspect and retry it.
        return created


def _next_fire(schedule: ScheduleRecord, current: datetime) -> datetime | None:
    if schedule.trigger_type == "one_shot":
        return None
    if schedule.trigger_type in {"interval", "heartbeat"}:
        return current + timedelta(seconds=int(schedule.expression))
    local = current.astimezone(ZoneInfo(schedule.timezone))
    return croniter(schedule.expression, local).get_next(datetime).astimezone(UTC)


def _outside_grace(item: datetime, now: datetime, schedule: ScheduleRecord) -> bool:
    return (now - item).total_seconds() > schedule.misfire_grace_seconds


def _completed_response(store: Store, run_id: str) -> str:
    for event in reversed(store.list_events(run_id)):
        if event.event_type == "agent.completed":
            return str(event.payload.get("response", ""))
    return ""


def _add_report(report: SchedulerReport, **updates: int) -> SchedulerReport:
    values = {field: getattr(report, field) for field in report.__dataclass_fields__}
    for key, value in updates.items():
        values[key] += value
    return SchedulerReport(**values)
