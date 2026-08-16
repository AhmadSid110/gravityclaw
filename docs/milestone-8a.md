# Milestone 8A — Control Plane Correctness

M8A establishes the authenticated, durable control-plane contract that the
future Web UI will consume. It does not contain UI code.

## Contract

- HTTP control routes are protected by a single-user bearer token when
  `GRAVITYCLAW_CONTROL_TOKEN` or `GRAVITYCLAW_CONTROL_TOKEN_FILE` is configured.
- `/health`, OpenAPI, and documentation remain public for local diagnostics.
- WebSockets accept the bearer header; browser clients may use the
  `access_token` query parameter when a header cannot be set. Deployments
  should prefer a same-origin cookie or reverse-proxy header injection.
- The server continues to bind localhost by default. Authentication is not a
  substitute for network isolation.
- SQLite remains the source of truth. Read models are projections over existing
  conversations, runs, schedules, events, context, memory, and capability data.
- `GET /api/v1/control/home`, `/workspaces`, `/conversations`, `/runs`,
  `/audit`, and the timeline endpoint are the initial UI read models.
- `/ws/control` sends a snapshot, then globally ordered persisted events. A
  reconnect supplies the last event cursor and replays from SQLite.
- Schedule enable/disable/delete mutations accept `expected_version`; stale
  writes return `409 Conflict`.
- Mutations write redacted audit records. Secret-shaped fields never appear in
  audit payloads.

## Authentication

```text
GRAVITYCLAW_CONTROL_TOKEN_FILE=/run/secrets/gravityclaw-control-token
```

The token is compared with `hmac.compare_digest` and is never stored in SQLite,
audit records, events, or application settings representations. If no token is
configured, the app remains in local development compatibility mode; production
deployments must configure one before exposing the control plane.

## Verification

The M8A tests cover authentication, public health, control read models, global
event cursors, optimistic schedule mutations, audit redaction, and schema
migration. The prior M1–M7 test and acceptance suites remain required before the
M8A checkpoint is tagged.
