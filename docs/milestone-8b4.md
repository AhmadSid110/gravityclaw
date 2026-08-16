# Milestone 8B.4 — Rich Run + Subagent Inspector

M8B.4 makes execution inspectable without creating a second execution path.
The inspector reads the same durable run, event, context, capability, and
artifact contracts used by the rest of GravityClaw.

## Delivered

- Shared rich inspector for live and historical runs
- Run overview with lifecycle, workspace, trigger, worker, and conversation metadata
- Unified normalized execution timeline
- Tool cards with running, completed, failed, cancelled, and soft-denied states
- First-class subagent activity tree from native AGY subagent events
- Lazy artifact metadata and on-demand content preview
- Immutable context and capability snapshot panels
- Normalized/raw event inspection with server-side secret-shaped redaction
- Historical Runs view, not only active-run inspection
- Live refresh driven by the centralized replay event stream
- Existing version-checked cancellation path retained

## State boundary

The inspector is read-only apart from the existing durable stop action. It does
not execute tools, mutate manifests, or infer authoritative lifecycle state from
browser history. Timeline refreshes replace the projection with persisted data;
the browser never becomes an execution engine.

## Verification

- Backend: 73 tests passed
- Frontend TypeScript/Vite production build passed
- Python compilation, diff checks, and inspector artifact API tests passed
- Control-plane event payloads redact secret-shaped keys before reaching the UI

Deferred to the later M8B acceptance gate: full browser visual/reconnect tests,
virtualized 10k-event navigation, complete parent/child subagent fixtures,
artifact diff viewers, and cross-page deep links.
