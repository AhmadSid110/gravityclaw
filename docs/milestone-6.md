# Milestone 6 — Heartbeat and scheduling

Status: verified

M6 adds proactive execution without introducing a second agent runtime. The
scheduler decides when GravityClaw should act; `RunManager` still owns how a
run is compiled, isolated, dispatched, monitored, cancelled, and reconciled.

## Delivered

- Schema v6 with durable schedules and immutable trigger occurrences.
- One-shot, interval, cron, and heartbeat schedules.
- IANA timezone validation through `zoneinfo` and maintained `croniter` parsing,
  including DST coverage.
- Trigger identities of the form
  `schedule_id:generation:scheduled_for_utc` with a unique SQLite constraint.
- Lease-based claims with startup recovery for claims abandoned by a gateway
  crash.
- Atomic claimed-trigger → queued-run creation. A crash cannot create a run
  without a trigger link or a trigger without a durable run link.
- `SKIP`, `QUEUE`, and `REPLACE` concurrency policies.
- `MISFIRE_SKIP`, `MISFIRE_RUN_ONCE`, and `MISFIRE_CATCH_UP` policies with a
  configurable grace window.
- Heartbeat backlog collapse and default-silent notification behavior.
- Actionable proactive results use the normal durable channel outbox when a
  notification target is configured.
- Heartbeat context profile includes `HEARTBEAT.md` as operational policy while
  keeping it outside ordinary execution identity.
- Schedule API endpoints for creation, listing, enable/disable, deletion, and
  trigger inspection. No dashboard or natural-language schedule editor is part
  of this milestone.

## Reliability invariant

Every intended occurrence receives exactly one durable decision: dispatched,
skipped, missed, cancelled, or failed. The scheduler may evaluate at least
once, but the occurrence uniqueness key and atomic run link prevent duplicate
logical executions.

Trigger state is separate from run state. A completed run may still have a
pending channel delivery, and scheduler/notification failures cannot rewrite
execution truth.

## Verification

```text
Existing regression suite                 PASS
M6 scheduler unit tests                   PASS
100-schedule SQLite load gate             PASS
SIGKILL after CLAIMED                     PASS
SIGKILL after atomic scheduled dispatch   PASS
Lease recovery and idempotent re-run      PASS
Timezone/DST validation                   PASS
SQLite WAL and integrity                  PASS
```

The deterministic gate is [m6_scheduler.py](../acceptance/m6_scheduler.py).

## Deferred

Natural-language scheduling, DAGs, webhooks, distributed scheduler
coordination, complex retry workflows, graphical editing, and multi-user
scheduling remain outside M6.
