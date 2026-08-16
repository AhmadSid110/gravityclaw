# Milestone 8B.6 — Automations + Capabilities

M8B.6 exposes the M6 scheduler and M7 capability registry as durable control-plane
surfaces. The UI does not create alternate execution paths: `Run now` creates an
idempotent manual trigger, and all resulting work enters the existing scheduler,
conversation, context, job, isolation, and AGY pipeline.

## Delivered

- Versioned automation read models with schedule detail and occurrence history.
- Full schedule-generation editing with optimistic `version` checks.
- Enable/disable controls with stale-tab conflict responses.
- Idempotent `run-now` requests keyed by a client request ID.
- Workspace-scoped capability read model for skills, MCP, bindings, isolation,
  health, and immutable run snapshots.
- Capability enable/disable mutations guarded by `updated_at` concurrency tokens.
- MCP health checks and secret-reference-only presentation.
- Responsive Automations and Capabilities studios in the existing React shell.
- No capability secret values, worker environment values, or credentials are sent
  to the browser.

## Verification

- 75 backend tests pass, including scheduler, capability, control-plane, and
  M8B.6 idempotency/redaction coverage.
- Frontend TypeScript/Vite production build passes.
- Disposable Chromium verification passed automation create/edit/run-now, capability
  rendering/toggle, no console or HTTP errors, and 390px mobile layout with zero
  horizontal overflow.
- M8B.5 checkpoint remains unchanged at `m8b5-checkpoint`.

## Deferred

Marketplaces, arbitrary skill downloads, secret-value editors, OAuth account flows,
workflow DAGs, and graphical cron design remain outside this slice.
