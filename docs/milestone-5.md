# Milestone 5: Context Management Protocol

Status: verified on 2026-08-16.

> Memory is durable knowledge; context is a reproducible selection for one run.

## Delivered

- Context protocol v2 with deterministic `chat`, `coding`, `heartbeat`, and
  `scheduled` profiles.
- Explicit T0–T5 source tiers, priorities, provenance, content hashes,
  confidence, and `trusted`, `semi_trusted`, or `untrusted` classifications.
- Conservative UTF-8 token estimates plus a hard character ceiling. Mandatory
  identity and the current task fail closed rather than being silently dropped.
- Stable optional-source ranking and explicit exclusion reasons for category,
  token, or character pressure.
- Immutable per-run manifests with prompt, identity, and context fingerprints;
  included and omitted sources; invalidations; budgets; and lifecycle times.
- Atomic `COMPILED → DISPATCHED → ARCHIVED` manifest lifecycle.
- Per-conversation dispatch watermarks containing the current task message,
  backend conversation binding, identity hashes, last run, and context
  fingerprint. A watermark describes what GravityClaw dispatched, not what AGY
  internally retained.
- Hash-based identity invalidation. Recompilation reads every authoritative file
  and reports only changed sources; it does not rely on timestamps or caches.
- Versioned deterministic conversation summaries with exact first/last message
  IDs. Raw messages are never deleted. Recent messages and summary coverage do
  not overlap.
- Large artifact storage with content hashes and bounded excerpt/summary
  references. Raw logs remain in storage and cannot flood future prompts.
- Schema v4 to v5 migration and local context inspection endpoints:
  `GET /runs/{id}/context`, `GET /conversations/{id}/context-watermark`, and
  `GET /conversations/{id}/summaries`.
- Explicit artifact ingestion through `POST /runs/{id}/artifacts`.

## Trust and duplication rules

Authoritative identity and the current task are trusted. Curated/retrieved
memory is semi-trusted reference data. Channel history and artifacts are
untrusted data. Every non-authoritative section is JSON encoded inside an
explicit trust envelope; text within it cannot alter the envelope structure.

Fresh AGY conversations may receive a bounded summary plus recent history.
Resumed AGY conversations receive required identity, the new task, relevant
memory, and operational deltas, but no reconstructed history or conversation
summary.

## Deterministic acceptance gate

Run:

```bash
PYTHONPATH=src .venv/bin/python acceptance/m5_context_protocol.py
```

Latest result:

```json
{
  "verdict": "PASSED",
  "protocol_version": 2,
  "profile": "chat",
  "estimated_tokens": 1925,
  "budget_tokens": 16000,
  "deterministic": true,
  "mandatory_preserved": true,
  "relevant_memory": true,
  "irrelevant_memory_excluded": true,
  "trust_envelope": true,
  "artifact_original_characters": 1000025,
  "artifact_prompt_bounded": true,
  "summary_messages": 20,
  "summary_recent_messages": 12,
  "resume_history_duplicated": false,
  "identity_invalidation": ["USER.md"],
  "crash_before_seal_partial_writes": false,
  "manifest_lifecycle": "ARCHIVED",
  "watermark_current_message": true,
  "sqlite": {"integrity": "ok", "journal_mode": "wal"}
}
```

The crash probe compiles context in a separate process and sends it `SIGKILL`
before atomic sealing. The run request, manifests, summaries, messages, and
memory remain unchanged.

## Live authenticated AGY gate

The production-private gateway migrated from schema v4 to v5 with SQLite
integrity intact. A fresh `coding` profile run completed with
`M5_CONTEXT_OK`, archived its v2 manifest, and persisted a watermark through
the current task message. A second run resumed the same AGY conversation with
`M5_RESUME_OK`, no duplicated history/summary source, and no false identity
invalidation.

## Regression gates

- 52 unit/integration tests pass.
- Milestone 4 durable Telegram forced-crash gate: `PASSED`.
- Milestone 2 concurrent forced-gateway-crash gate: `PASSED`, including worker
  reconciliation, orphan termination, event replay, and workspace isolation.

## Deferred

- Model-driven semantic summarization or memory consolidation
- Embeddings/vector retrieval and semantic graphs
- Heartbeat and scheduled execution (Milestone 6)
- Skills/MCP management and the dashboard

