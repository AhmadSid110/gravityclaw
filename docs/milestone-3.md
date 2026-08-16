# Milestone 3: Persistent Identity and Memory

Status: implementation complete; acceptance gates passed on 2026-08-16.

## Ownership boundary

- GravityClaw owns identity, curated and episodic memory, channel messages,
  retrieval, provenance, and context assembly.
- AGY owns reasoning, native tools, subagents, and its backend conversation.
- AGY credentials remain in the private `gravityclaw-agy-home` volume. No
  identity or memory code reads or exports them.

## Delivered

- Non-destructive bootstrap for `SOUL.md`, `USER.md`, `AGENTS.md`, `TOOLS.md`,
  `HEARTBEAT.md`, `MEMORY.md`, `memory/`, and `workspaces/`.
- Explicit execution identity allowlist. `HEARTBEAT.md` is not injected into
  ordinary runs.
- Episodic Markdown journals with stable memory IDs plus canonical SQLite rows.
- SQLite FTS5 literal-token retrieval with source, confidence, timestamp, and
  optional conversation provenance.
- Curated `MEMORY.md` and retrieved memories labeled as untrusted data in the
  execution envelope.
- JSON encoding of memory and message content so untrusted text cannot alter
  envelope structure.
- Deterministic budgets for identity, curated memory, retrieved memory, history,
  current task, and total context. Oversized authoritative identity fails closed
  instead of being silently omitted.
- Dispatch-time compilation, after the prior queued run has settled and the
  current AGY conversation binding is known.
- Persisted compiled prompt and provenance manifest before container start.
- Atomic user-message/run enqueue and atomic terminal-run/assistant-message
  persistence with per-run idempotency.
- Schema v2 to v3 migration preserving existing messages.
- Local API endpoints for identity inspection and explicit memory record/search.

## Conversation duplication rule

For a fresh AGY conversation, GravityClaw includes bounded recent channel
history while excluding the current run's user message from that history. The
current task appears exactly once.

For a resumed AGY conversation, GravityClaw includes current identity, curated
memory, relevant retrieved memory, and the new task, but omits channel history
because AGY already owns that backend context.

## Trust model

Identity documents are authoritative because the human edits them. Curated and
retrieved memories and prior messages are reference data and may be stale,
incorrect, or malicious. Provenance markers aid auditing; they are not a model
security boundary. Rootless container isolation from Milestone 2 remains
mandatory.

Model execution has no path that writes `SOUL.md`, `USER.md`, `AGENTS.md`, or
`MEMORY.md`. Memory creation is an explicit GravityClaw operation and records a
source and confidence. Automatic consolidation and embeddings are intentionally
deferred.

## Verification

Focused suite:

```text
27 tests passed
```

Coverage includes non-destructive bootstrap, FTS query hardening, stable daily
journals, malicious memory/history payloads, context budgets, history omission
on resume, current-message deduplication, dispatch-time snapshot persistence,
assistant-message idempotency, API round trips, and v2-to-v3 migration.

Milestone 2 regression gate:

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
  "workspace_isolation": "passed",
  "orphan_terminated": true,
  "sqlite": {"integrity": "ok", "journal_mode": "wal"}
}
```

Live authenticated AGY gate:

```json
{
  "verdict": "PASSED",
  "status": "completed",
  "response": "IDENTITY_M3_OK MEMORY_M3_OK",
  "context_characters": 1741
}
```

The live manifest included `SOUL.md`, `USER.md`, `AGENTS.md`, `TOOLS.md`,
`MEMORY.md`, and the FTS-retrieved episodic memory. The run persisted
`run.context_compiled` before worker attachment and persisted the assistant
message on completion.

## Deferred

- Embeddings or vector databases
- Model-driven automatic memory writes
- Memory consolidation into `MEMORY.md`
- Scheduler and heartbeat execution
- Telegram and other channels
- Skills/MCP management and control dashboard
