# Milestone 4: Durable Channel Layer

Status: implementation and deterministic hard gate complete on 2026-08-16.
Live Telegram-account verification is pending bot credentials.

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

## Remaining live gate

Configure a dedicated Telegram bot token and Ahmad's numeric Telegram user ID,
approve one workspace alias, then verify `/workspace`, ordinary chat, streaming
edits, `/status`, `/new`, and `/stop` against Telegram's production Bot API.
