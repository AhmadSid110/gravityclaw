# Milestone 2: Reliable GravityClaw Core

Status: implementation complete; hard gate passed on 2026-08-16.

> No product-layer feature should compensate for an unreliable execution core.

## Delivered

- FastAPI control plane bound to localhost by default.
- WebSocket replay sourced from persisted events, not process memory.
- SQLite WAL with `synchronous=FULL`, foreign keys, busy timeout, and lifecycle
  transitions committed with their corresponding lifecycle events.
- Per-run monotonically increasing event sequence and idempotent worker-source
  sequence ingestion.
- Multiple queued messages with one running job per conversation.
- Rootless Podman execution backend with immutable base filesystem, dropped
  capabilities, no-new-privileges, PID/memory/CPU limits, and only the selected
  workspace mounted.
- Labeled worker discovery, process/container cancellation, orphan termination,
  and startup reconciliation.
- Official AGY 1.1.13 worker image. Its named home volume is the only place AGY
  may manage worker credentials; GravityClaw never reads or exports them.
- Explicit lifecycle outcomes: `queued`, `running`, `completed`, `failed`,
  `cancelled`, `interrupted`, and `orphaned`.

## Forced-crash gate

The acceptance harness starts concurrent real Podman workers for:

- Streaming text
- A long-running shell command
- An active subagent
- A conversation with a queued follow-up
- A tool failure
- A deliberately disappeared worker
- A deliberately unknown orphan

It then sends `SIGKILL` to the gateway, waits while detached workers continue,
and starts a new gateway against the same SQLite database.

Latest result:

```json
{
  "verdict": "PASSED",
  "post_crash_reconciliation": {
    "finalized": 4,
    "interrupted": 1,
    "orphaned": 1,
    "queued_dispatched": 1,
    "reattached": 1
  },
  "statuses": {
    "text": "completed",
    "shell": "completed",
    "subagent": "completed",
    "queued-first": "completed",
    "queued-follow-up": "completed",
    "tool-failure": "failed",
    "isolation": "completed",
    "cancel": "cancelled",
    "disappeared": "interrupted"
  },
  "sqlite": {"integrity": "ok", "journal_mode": "wal"},
  "workspace_isolation": "passed",
  "orphan_terminated": true,
  "idempotent_reconciliation": {
    "finalized": 0,
    "interrupted": 0,
    "orphaned": 0,
    "queued_dispatched": 0,
    "reattached": 0
  }
}
```

Additional assertions verify contiguous event ordering, exactly one terminal
lifecycle event per run, stable conversation-to-AGY mappings, correct resume of
the queued follow-up, WebSocket reconstruction from SQLite, and no running
managed containers after completion.

Run the gate:

```bash
.venv/bin/python acceptance/m2_forced_crash.py
```

## Security boundary

The execution container receives:

- The selected workspace at `/workspace`.
- A private persistent AGY home volume only for AGY-mode workers.
- Network access only in AGY mode; deterministic acceptance workers have no
  network.

It does not receive the host home, SSH directory, cloud credentials, container
socket, unrelated workspaces, or inherited application secrets. AGY allow-all
remains disabled unless a run explicitly requests it.

The consumer-login policy ambiguity remains unchanged. Keep this prototype
single-user and private.
