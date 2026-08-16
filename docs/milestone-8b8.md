# Milestone 8B.8 — Browser Torture, Reconnect, and Performance Gate

M8B.8 freezes the product surface and verifies that the browser remains a
projection of durable GravityClaw state under disconnects, gateway crashes,
concurrent runs, mobile layouts, and large historical event streams. Search and
the command palette remain intentionally deferred.

## Delivered

- Disposable Chromium gate covering Web runs, Telegram-originated activity,
  conversation switching, reconnect, gateway SIGKILL, and recovery.
- Browser-visible redaction checks for control tokens, secret-shaped values, and
  hidden reasoning fields.
- Responsive checks at phone, tablet, desktop, and ultrawide widths with zero
  horizontal overflow in the tested surfaces.
- Optional historical context and capability manifests now return an empty
  success response when absent, avoiding noisy inspector 404s while preserving
  404 for unknown runs.
- Timeline read models are cursor-paginated. The inspector renders a bounded
  initial window and provides `Load more events` without putting the full event
  history in the DOM.
- Timeline pagination is cursor-tested across a 10,003-event fixture.

## Verification

- 77 backend tests pass.
- Python source and acceptance scripts compile successfully.
- Frontend TypeScript/Vite production build passes.
- Concurrent Web/Telegram recovery probe passed with no event crossover,
  duplicate UI state, token leakage, or post-recovery HTTP errors.
- Gateway SIGKILL recovery reconstructed active conversations and preserved
  server-side execution continuity.
- 10,003 persisted events were traversed in 11 bounded pages; the first
  1,000-event inspector render completed in approximately 1.1 seconds.
- Mobile overflow measured at zero in the live disposable fixture.
- SQLite and prior M2–M8A regression suites remain green.

## Acceptance boundary

The deliberate crash window may produce transient connection failures while the
gateway is absent. The gate requires clean replay and no errors after health
recovers. Generation, scheduling, capabilities, and execution paths were not
expanded in this milestone.
