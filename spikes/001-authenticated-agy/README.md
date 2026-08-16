# Authenticated AGY feasibility spike

Date: 2026-08-16
CLI: official Antigravity CLI 1.1.13, Linux x86-64
Account tier observed: Google AI Pro

## Verdict: VALIDATED

Question: Can GravityClaw use the official authenticated `agy` headless CLI as
a resumable, streamable, cancellable execution backend without reading or
exporting AGY credentials?

Evidence: authenticated live runs exercised conversation resume, tool events,
permission denial, subagent events, process-tree cancellation, two concurrent
workspaces, a forced backend crash, and SQLite restart recovery. The deterministic
suite passes 14 tests.

What worked:

- `init`, `step_update`, and `result` form a usable NDJSON protocol boundary.
- Explicit `--conversation` resume retained prior conversational state.
- Tools emit `ACTIVE`, `DONE`, and `ERROR` states with parameters and output/error.
- Subagents emit `step_type: subagent`, state transitions, role, child conversation
  ID, and transcript URI.
- SIGINT to the AGY process group stopped a live `sleep 120` run in 6.5 seconds.
- Two independent workspaces completed concurrently in 4.3 seconds with distinct
  conversation IDs and correct working directories.
- SIGKILL of a live process group produced a truncated stream that the adapter
  reported as `backend.protocol_error`.
- SQLite recovery marks active runs interrupted after restart while retaining the
  GravityClaw-to-AGY conversation binding.

What failed or surprised us:

- Headless permission prompts are soft-denied. The tool emits `state: ERROR`, but
  AGY may still return overall `status: SUCCESS` with an empty response.
- A user cancellation can produce AGY `status: ERROR` and exit code 1. The adapter
  must overlay its own cancellation intent and emit `agent.interrupted`.
- With `--sandbox`, two terminal-sandbox connection resets occurred before AGY
  retried successfully. Despite the successful tool output and assistant response,
  the final result remained `status: ERROR` because an earlier retry failed.
- The same safe command succeeded immediately without `--sandbox`. Native terminal
  sandboxing should therefore be treated as defense in depth, not the sole worker
  boundary.
- `result.duration_seconds`, `num_turns`, and aggregate usage on a resumed
  conversation are cumulative, not metrics for only the latest invocation.
- Some `agent_response` steps contain usage but no `text_delta`; consumers must not
  assume every response step carries visible text.

Recommendation: proceed with GravityClaw Core. Preserve every raw event, derive
tool/run health from the full event sequence rather than only the final result,
and put autonomous AGY workers inside an OS/container capability boundary before
enabling allow-all. Do not rely on AGY's terminal sandbox alone.

## Stable normalized contract

| AGY input | GravityClaw event |
|---|---|
| `init` | `agent.started` |
| visible `agent_response` delta | `message.delta` |
| non-visible/unknown step | `agent.step` |
| tool `ACTIVE` | `tool.started` |
| tool `DONE` | `tool.finished` |
| tool `ERROR`, `CANCELED`, or `INTERRUPTED` | `tool.failed` |
| step containing `subagent_info` | `subagent.updated` |
| successful result | `agent.completed` |
| failed result | `agent.failed` |
| canceled result or failed result after gateway cancellation | `agent.interrupted` |
| unknown top-level event | `backend.event` |
| malformed/truncated stream | `backend.protocol_error` |
| gateway wall-clock expiry | `backend.timeout` |

All normalized events retain the original raw AGY object. Unknown event and step
types are preserved rather than rejected.

## Reproduction

The stress harness requires an already-authenticated official `agy` binary:

```bash
PYTHONPATH=src python3 spikes/001-authenticated-agy/run_live.py cancellation
PYTHONPATH=src python3 spikes/001-authenticated-agy/run_live.py parallel
PYTHONPATH=src python3 spikes/001-authenticated-agy/run_live.py killed-stream
```

The harness uses temporary workspaces. Cancellation and crash probes use
`--dangerously-skip-permissions` only for a fixed `sleep 120` command and request
AGY's terminal sandbox. This spike does not claim that terminal sandboxing is a
complete security boundary.
