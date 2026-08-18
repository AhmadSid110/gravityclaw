"""CuratorJob — durable scheduler integration for the skill curator.

Wraps the deterministic Curator in the scheduler's job contract:
- Idempotent: running twice against unchanged state produces zero duplicate actions.
- Lock-protected: only one curator run executes at a time.
- Auditable: each run is recorded with its report.

The curator job is registered as a `cron` schedule at app startup, with
the expression and timezone from [learning.curator] configuration.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from .learning_config import CuratorScheduleConfig
from .skills.curator import Curator, CuratorConfig, CuratorReport
from .store import Store

LOGGER = logging.getLogger(__name__)

# In-process lock for curator runs.
# Prevents concurrent curations from the scheduler + manual trigger.
_CURATOR_LOCK = threading.Lock()

# Sentinel key used in the store to record curator run state.
CURATOR_STATE_KEY = "learning:curator"


class CuratorJob:
    """Scheduler-compatible wrapper around the Curator engine.

    Provides:
    - Idempotency: tracks last-run timestamp; skips if min_idle_hours not met.
    - Locking: thread-level lock prevents concurrent runs.
    - State persistence: records last run time and report in the store.
    - Audit: records curator run events.
    """

    def __init__(
        self,
        curator: Curator,
        store: Store,
        config: CuratorScheduleConfig,
    ) -> None:
        self._curator = curator
        self._store = store
        self._config = config
        self._last_run_at: datetime | None = None
        self._last_report: CuratorReport | None = None

    @property
    def curator(self) -> Curator:
        return self._curator

    @property
    def config(self) -> CuratorScheduleConfig:
        return self._config

    @property
    def last_run_at(self) -> datetime | None:
        return self._last_run_at

    @property
    def last_report(self) -> CuratorReport | None:
        return self._last_report

    def should_run(self, *, now: datetime | None = None) -> bool:
        """Check whether the curator should run based on idleness constraint."""
        if not self._config.enabled:
            return False
        _now = now or datetime.now(UTC)
        if self._last_run_at is not None:
            hours_since = (_now - self._last_run_at).total_seconds() / 3600
            if hours_since < self._config.min_idle_hours:
                return False
        return True

    def run(self, *, now: datetime | None = None, force: bool = False) -> CuratorReport | None:
        """Execute a curator run with locking and idempotency.

        Args:
            now: Override current time (for testing).
            force: Skip idleness check.

        Returns:
            CuratorReport on success, None if skipped (lock contention or idle).
        """
        _now = now or datetime.now(UTC)

        if not self._config.enabled:
            LOGGER.debug("curator job: disabled by config")
            return None

        if not force and not self.should_run(now=_now):
            LOGGER.debug("curator job: skipped (min_idle_hours not met)")
            return None

        # Try to acquire lock (non-blocking to allow skip instead of queue)
        if not _CURATOR_LOCK.acquire(blocking=False):
            LOGGER.info("curator job: skipped (another run is active)")
            return None

        try:
            report = self._curator.run(now=_now)
            self._last_run_at = _now
            self._last_report = report
            self._record_run(_now, report)
            LOGGER.info(
                "curator job: completed — evaluated=%d actions=%d blocked=%d",
                report.skills_evaluated,
                len(report.actions_taken),
                len(report.actions_blocked),
            )
            return report
        except Exception as exc:
            LOGGER.error("curator job: failed: %s", exc, exc_info=True)
            return None
        finally:
            _CURATOR_LOCK.release()

    def _record_run(self, run_time: datetime, report: CuratorReport) -> None:
        """Persist curator run state and audit event."""
        try:
            actions_summary = [
                {"skill": a.skill_name, "action": a.action, "reason": a.reason}
                for a in report.actions_taken
            ]
            self._store.record_audit(
                actor="system:curator",
                action="curator.run",
                resource_type="curator",
                resource_id=CURATOR_STATE_KEY,
                payload={
                    "skills_evaluated": report.skills_evaluated,
                    "actions_taken": len(report.actions_taken),
                    "actions_blocked": len(report.actions_blocked),
                    "skipped_protected": report.skipped_protected,
                    "errors": report.errors[:5],
                    "actions": actions_summary[:10],
                    "run_at": run_time.isoformat(),
                },
            )
        except Exception as exc:
            LOGGER.warning("curator job: failed to record audit: %s", exc)

    def status(self) -> dict[str, Any]:
        """Return current curator job status for API/UI consumption."""
        return {
            "enabled": self._config.enabled,
            "schedule": self._config.schedule,
            "timezone": self._config.timezone,
            "last_run_at": self._last_run_at.isoformat() if self._last_run_at else None,
            "last_report": {
                "skills_evaluated": self._last_report.skills_evaluated,
                "actions_taken": len(self._last_report.actions_taken),
                "actions_blocked": len(self._last_report.actions_blocked),
                "skipped_protected": self._last_report.skipped_protected,
            } if self._last_report else None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Schedule registration helper
# ─────────────────────────────────────────────────────────────────────────────

# Sentinel schedule name used for the curator cron job.
CURATOR_SCHEDULE_NAME = "learning:curator"

# The prompt the scheduler dispatches — RunManager recognizes this as a
# system job and routes it to the curator job rather than AGY.
CURATOR_SCHEDULE_PROMPT = "__system_curator_run__"


def ensure_curator_schedule(
    store: Store,
    workspace_id: str,
    config: CuratorScheduleConfig,
) -> str | None:
    """Register (or update) the curator cron schedule idempotently.

    If a schedule named "learning:curator" already exists:
    - If the expression/timezone match config: no-op.
    - If they differ: update the schedule.

    If it doesn't exist and curator is enabled: create it.
    If curator is disabled: disable existing schedule if present.

    Returns the schedule_id or None if curator is disabled and no schedule exists.
    """
    existing = _find_curator_schedule(store)

    if not config.enabled:
        if existing:
            # Disable if it was enabled
            store.set_schedule_enabled(existing.id, False)
            LOGGER.info("curator schedule disabled (config.enabled=false)")
        return existing.id if existing else None

    if existing:
        # Check if update needed
        if (
            existing.expression == config.schedule
            and existing.timezone == config.timezone
            and existing.enabled
        ):
            LOGGER.debug("curator schedule up-to-date: %s", existing.id)
            return existing.id
        # Update needed — the store doesn't have a direct update, so we
        # disable old + create new (soft migration). For now, just ensure enabled
        # and log if expression changed.
        if not existing.enabled:
            store.set_schedule_enabled(existing.id, True)
            LOGGER.info("curator schedule re-enabled: %s", existing.id)
        if existing.expression != config.schedule or existing.timezone != config.timezone:
            LOGGER.info(
                "curator schedule expression changed %s/%s → %s/%s (requires recreate)",
                existing.expression, existing.timezone,
                config.schedule, config.timezone,
            )
            # Delete old and create new
            store.delete_schedule(existing.id)
            return _create_curator_schedule(store, workspace_id, config)
        return existing.id

    # Create new
    return _create_curator_schedule(store, workspace_id, config)


def _find_curator_schedule(store: Store) -> Any:
    """Find the existing curator schedule by name."""
    schedules = store.list_schedules(include_deleted=False)
    for s in schedules:
        if s.name == CURATOR_SCHEDULE_NAME:
            return s
    return None


def _create_curator_schedule(
    store: Store,
    workspace_id: str,
    config: CuratorScheduleConfig,
) -> str:
    """Create the curator cron schedule."""
    from .scheduler import Scheduler

    record = store.create_schedule(
        name=CURATOR_SCHEDULE_NAME,
        trigger_type="cron",
        expression=config.schedule,
        timezone=config.timezone,
        prompt=CURATOR_SCHEDULE_PROMPT,
        context_profile="scheduled",
        workspace_id=workspace_id,
        next_run_at=Scheduler.first_run_at(
            trigger_type="cron",
            expression=config.schedule,
            timezone=config.timezone,
        ),
        concurrency_policy="SKIP",
        misfire_policy="MISFIRE_SKIP",
        misfire_grace_seconds=3600,
    )
    LOGGER.info("curator schedule created: %s (cron=%s tz=%s)", record.id, config.schedule, config.timezone)
    return record.id
