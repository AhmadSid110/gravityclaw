# AGY feasibility spike

Date: 2026-08-16
Official CLI tested: 1.1.13, Linux x86-64
Artifact SHA-256: `edc7c32b5ab4fc2e4da03381fee83ed566dea6b56b56f9329cd13cd77947a1d9`

## Verified locally

- The official binary exposes `--output-format stream-json`,
  `--conversation`, `--model`, `--effort`, `--agent`, `--sandbox`, and
  `--dangerously-skip-permissions`.
- GravityClaw can supervise the process as an isolated process group, drain
  stdout and stderr concurrently, parse NDJSON incrementally, retain unknown
  events, and normalize documented events.
- Explicit conversation resume command construction is covered by tests;
  `--continue` is deliberately never used for routed conversations.
- Cancellation, malformed streams, startup stalls, and concurrent independent
  processes are covered by deterministic subprocess tests.
- Missing authentication produces diagnostics on stderr and an `ERROR` result
  on stdout. It may wait for OAuth even when `--print-timeout` is set, so
  GravityClaw enforces its own wall-clock timeout around the complete process.

## Authenticated live verification complete

The authenticated matrix passed on 2026-08-16. Resume, tools, permission denial,
subagents, cancellation, parallel workspaces, forced truncation, and durable
restart recovery were exercised. The evidence, surprises, normalized event
contract, and verdict are recorded in
`spikes/001-authenticated-agy/README.md`.

The result is `VALIDATED` with one important adjustment: AGY's terminal sandbox
showed transient connection resets and cannot be GravityClaw's only containment
boundary. Autonomous workers still require external OS/container isolation.

GravityClaw must not copy or inspect Antigravity credentials. Authentication
must be completed through the official CLI or configured through an officially
supported API-key/ADC mechanism.

## Existing-solutions preflight

- `shubzkothekar/antigravity-acp` is a useful reference for process/session
  translation but is an ACP editor bridge, not a persistent personal-agent
  platform.
- `sshahzaiib/agy-bridge` exposes AGY delegation through MCP and does not own
  identity, memory, scheduling, or channels.
- `jaxhemopo/tg-cli-bridge` is a thin Telegram-to-CLI bridge and does not cover
  the GravityClaw core.

The preflight supports building the GravityClaw orchestration layer while
borrowing narrowly applicable protocol and installer patterns where their
licenses permit.
