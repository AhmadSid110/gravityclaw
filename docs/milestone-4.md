# Milestone 4: Durable Channel Layer

Status: verified on 2026-08-16. The deterministic forced-crash gate and the
live Telegram production gate both passed.

> Telegram transports messages; GravityClaw remains the agent.

## Delivered

- Channel-neutral inbound, polled-update, provider-message, adapter, and delivery
  error contracts.
- SQLite schema v4 with durable polling cursors, authorized inbox rows,
  workspace aliases, channel bindings, cancellation requests, presentations,
  delivery leases, retries, and provider acknowledgements.
- Transactional inbound flow: dedupe, routing, user message, queued run,
  presentation outbox, and cursor advancement commit together.
- Single-user Telegram authorization before conversation lookup, context
  compilation, event creation, or agent execution.
- Server-approved workspace aliases. Telegram input never becomes a filesystem
  path.
- `/new`, `/status`, `/stop`, and `/workspace <alias>` through the same durable
  ingestion path as ordinary text.
- Durable cancellation requests committed before container signalling and
  reconciled idempotently after restart.
- Presentation reducer that converts persisted execution events into throttled
  plain-text status edits without exposing private reasoning.
- Durable outbox state machine: `PENDING`, `SENDING`, `RETRY_WAIT`, `DELIVERED`,
  `UNCERTAIN`, and `FAILED`.
- Initial sends that lose acknowledgement become `UNCERTAIN` and are not
  blindly retried. Edits to known provider message IDs are safely retried;
  Telegram's “message is not modified” response counts as delivered.
- AGY job status and channel delivery status remain independent.
- Bot token loaded from an environment variable or preferred secret file. It
  is excluded from settings representations, SQLite, normalized events, and
  application/HTTP-client logs.

## Deterministic forced-crash gate

The gate runs a real GravityClaw gateway process and rootless worker containers
against a local Telegram Bot API simulator. The simulator deliberately accepts
requests and drops acknowledgements.

Verified scenarios:

- Authorized inbound update creates exactly one run.
- Unauthorized sender advances the polling cursor without entering the inbox.
- Gateway `SIGKILL` while a Telegram-created run is active.
- Inbound update persisted while the gateway is down, then dispatched after
  restart.
- Queued follow-up after durable cancellation of the preceding run.
- `/stop` request persisted before worker termination and completed after
  restart.
- Initial `sendMessage` accepted with lost acknowledgement becomes `UNCERTAIN`
  without a duplicate retry.
- Final edit accepted with lost acknowledgement retries against the same
  provider message and becomes `DELIVERED`.
- A second restart is idempotent and creates no Telegram messages.
- SQLite integrity, WAL mode, and token non-leakage.

Latest result:

```json
{
  "verdict": "PASSED",
  "authorized_inbox_rows": 1,
  "unauthorized_inbox_rows": 0,
  "polling_cursor": 5,
  "persisted_before_start": "cancelled",
  "queued_followup": "completed",
  "durable_cancellation": "COMPLETED",
  "initial_send_boundary": "UNCERTAIN",
  "final_edit_boundary": "DELIVERED",
  "token_leak": false,
  "sqlite": {"integrity": "ok", "journal_mode": "wal"}
}
```

Run it with:

```bash
.venv/bin/python acceptance/m4_channel_crash.py --skip-build
```

## Explicitly deferred

- Attachments and multimodal Telegram messages
- Multiple Telegram users
- Webhooks
- Scheduler and heartbeat
- Automatic memory extraction
- Additional channel adapters
- Dashboard and channel administration UI

## Live Telegram production gate

Verified against Telegram's production Bot API with a dedicated private bot:

- Authorized single-user routing and an approved server-side workspace alias.
- `/workspace`, `/status`, `/new`, ordinary chat, and throttled streaming edits.
- Live AGY execution completed with the expected response.
- `/stop` durably recorded a cancellation request before worker signalling.
- The AGY worker process/container exited, the run reached `cancelled`, and the
  cancellation request reached `COMPLETED` after one attempt.
- The final cancelled presentation was delivered against its existing Telegram
  message ID; no duplicate event sequence or pending delivery remained.
- The gateway was terminated with `SIGKILL` and restarted against the same WAL
  database. Startup reconciliation was clean and idempotent, the Telegram
  polling cursor was preserved, and no managed worker remained active.
- SQLite `integrity_check` returned `ok`; no bot token was stored in SQLite or
  committed to the repository.

The live gate intentionally records no bot token, account ID, or chat ID in the
repository.
