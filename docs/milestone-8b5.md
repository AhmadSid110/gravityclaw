# Milestone 8B.5 — Context + Memory Studio

M8B.5 exposes the M3–M5 persistence layer as a transparent control-center
surface. It does not create a second memory or context engine.

## Delivered

- Memory Studio with indexed records, FTS search, source/confidence metadata,
  and memory-to-run provenance.
- Identity and curated-memory editor for `SOUL.md`, `USER.md`, `AGENTS.md`,
  `TOOLS.md`, `MEMORY.md`, and `HEARTBEAT.md`.
- SQLite-backed identity revision history with optimistic version checks and
  HTTP 409 conflict behavior.
- Daily journal browsing and hash-guarded source editing.
- Context Inspector with immutable manifest lifecycle, token budget, source
  trust, inclusion/exclusion reasons, hashes, and provenance.
- Read-only context simulation through the existing `ContextBuilder`; preview
  compilation creates no run, AGY process, watermark, summary, or memory write.
- Control-plane aliases for run context/capability inspection and dev-server
  proxy coverage for existing conversation/run actions.

## Verification

- 74 backend tests pass.
- Production frontend build passes.
- Disposable Chromium interaction pass covers login, Memory, Identity edit/
  discard/history, Daily Journal, FTS search, Context Inspector, preview
  compilation, desktop layout, mobile navigation, and zero horizontal overflow.
- Browser console and HTTP error scan is clean after the context alias fix.
- Context manifests remain server-authoritative and no preview job is created.

## Boundary

`Memory is durable knowledge; context is a reproducible selection made for one
run.` UI actions call GravityClaw Core APIs, and never write identity files or
recompute persisted manifests in the browser.

The broader `m8b-verified` tag remains reserved for the final reconnect,
performance, accessibility, and crash gate.
