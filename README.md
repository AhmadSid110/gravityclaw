# GravityClaw

> **No product-layer feature should compensate for an unreliable execution core.**

GravityClaw is a persistent personal-agent platform whose first execution
backend is Google's official Antigravity CLI (`agy`). GravityClaw owns identity,
memory, sessions, scheduling, channels, and orchestration; `agy` owns reasoning
and tool execution.

Phase 1, the authenticated AGY feasibility spike, has a `VALIDATED` verdict.
Milestone 2, the reliable container-backed core, passes its forced-gateway-crash
gate. Milestone 3 adds GravityClaw-owned identity, explicit episodic memory,
SQLite FTS retrieval, bounded context compilation, and durable channel history.
See the [spike report](spikes/001-authenticated-agy/README.md),
[Milestone 2 report](docs/milestone-2.md), and
[Milestone 3 report](docs/milestone-3.md). Milestone 4 adds the durable channel
layer and Telegram adapter; both deterministic and live Telegram gates pass.
Milestone 5 adds the Context Management Protocol, profiles, manifests,
watermarks, summaries, artifact references, and hash invalidation. See the
[Milestone 4 report](docs/milestone-4.md) and
[Milestone 5 report](docs/milestone-5.md). Milestone 6 adds durable one-shot,
interval, cron, and heartbeat scheduling with leases, misfire policies,
timezone-aware recurrence, and crash-safe trigger/run linking. See the
[Milestone 6 report](docs/milestone-6.md).
Milestone 7 adds workspace-scoped native AGY skills and MCP governance with
immutable per-run capability snapshots, secret references, health state, and
atomic configuration publication. See the
[Milestone 7 report](docs/milestone-7.md).
Milestone 8A adds the authenticated control-plane contract, durable read
models, global event replay, optimistic schedule mutations, and redacted audit
records. See [the M8A report](docs/milestone-8a.md).
The M8B foundation adds the React/Vite responsive shell, browser-safe session
bridge, centralized control replay client, Home, Runs, and initial run inspector.
See [the M8B foundation report](docs/milestone-8b-foundation.md).
M8B.3 adds the durable Conversation Workspace with Web/Telegram convergence,
queued follow-ups, live activity reduction, and Focus/Inspect modes. See
[the M8B.3 report](docs/milestone-8b3.md).
M8B.4 adds the shared live/historical Run Inspector with tool cards, subagent
activity, lazy artifacts, immutable manifests, and redacted raw events. See
[the M8B.4 report](docs/milestone-8b4.md).
M8B.5 adds Context + Memory Studio with versioned identity editing, journal and
FTS memory views, run provenance, immutable context inspection, and read-only
context simulation. See [the M8B.5 report](docs/milestone-8b5.md).

## Core server

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
podman build -f worker/Containerfile.agy \
  -t localhost/gravityclaw-agy:1.1.13 .
GRAVITYCLAW_HOME="$PWD/.runtime" \
  .venv/bin/python -m gravityclaw.server
```

The server binds `127.0.0.1:8787` by default. Do not expose it publicly; channel
authentication belongs to the channel layer, and the control plane should also
be configured with `GRAVITYCLAW_CONTROL_TOKEN_FILE` before remote access.

## Local protocol probe

The official binary is intentionally not committed. Point the probe at an
installed, authenticated `agy` binary:

```bash
PYTHONPATH=src python -m gravityclaw.probe \
  --agy /path/to/agy \
  --workspace /path/to/safe/workspace \
  "Reply with one short sentence."
```

The probe prints GravityClaw's normalized NDJSON event stream. It never reads
or exports AGY credentials.

## Tests

The tests use a deterministic fake executable and require no Google account:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

The live Milestone 3 gate uses the authenticated private AGY worker volume but
does not enable allow-all or invoke tools:

```bash
.venv/bin/python acceptance/m3_live_context.py
```

The deterministic Milestone 4 Telegram crash gate uses a local Bot API
simulator and does not require a real bot token:

```bash
.venv/bin/python acceptance/m4_channel_crash.py --skip-build
```

## Identity and memory

On first start GravityClaw creates human-editable files under
`GRAVITYCLAW_HOME`: `SOUL.md`, `USER.md`, `AGENTS.md`, `TOOLS.md`,
`HEARTBEAT.md`, `MEMORY.md`, and `memory/`. Ordinary executions load the first
four as authoritative identity. `HEARTBEAT.md` is reserved for the later
scheduler milestone, and `MEMORY.md` is always labeled as data.

Episodic memory writes are explicit through `POST /memories`; model execution
cannot overwrite identity or curated memory files. Retrieval uses SQLite FTS5.
Context is compiled immediately before dispatch, persisted with a provenance
manifest, and bounded by deterministic token estimates plus a hard character
ceiling. A resumed AGY conversation does not receive duplicated channel
history. Inspect a run with `GET /runs/{id}/context`.

## Telegram channel

Approve workspaces through server-side aliases; Telegram never accepts paths:

```bash
curl -X POST http://127.0.0.1:8787/workspace-aliases \
  -H 'content-type: application/json' \
  -d '{"alias":"gravityclaw","workspace_id":"<workspace-id>"}'
```

Configure the single-user adapter:

```text
GRAVITYCLAW_TELEGRAM_BOT_TOKEN_FILE=/run/secrets/gravityclaw-telegram-token
GRAVITYCLAW_TELEGRAM_USER_ID=<numeric-user-id>
GRAVITYCLAW_TELEGRAM_DEFAULT_WORKSPACE=gravityclaw
```

`GRAVITYCLAW_TELEGRAM_BOT_TOKEN` is also supported, but the secret-file form is
preferred. Commands are `/new`, `/status`, `/stop`, and `/workspace <alias>`.
The polling cursor, inbox dedupe, cancellation requests, presentation state,
provider message IDs, retries, and delivery acknowledgements are all durable.

## Scheduling

Schedules enter the same `RunManager` path as channel messages. The API accepts
`one_shot`, `interval`, `cron`, and `heartbeat` schedules and stores timestamps
as UTC while evaluating cron expressions in an explicit IANA timezone. Use
`/schedules/{id}/triggers` to inspect occurrence decisions. Heartbeats default
to a single bounded evaluation, skip stale backlog, and remain silent unless an
actionable notification policy and channel target are configured.

## Security status

The live spike found that AGY's terminal sandbox can reset transiently. Milestone
2 therefore adds external rootless Podman containment. The worker still needs a
separate adversarial hardening review before autonomous mode is enabled against
valuable workspaces.
