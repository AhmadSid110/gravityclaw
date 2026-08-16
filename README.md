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
[Milestone 3 report](docs/milestone-3.md).

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
authentication belongs to a later milestone.

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

## Identity and memory

On first start GravityClaw creates human-editable files under
`GRAVITYCLAW_HOME`: `SOUL.md`, `USER.md`, `AGENTS.md`, `TOOLS.md`,
`HEARTBEAT.md`, `MEMORY.md`, and `memory/`. Ordinary executions load the first
four as authoritative identity. `HEARTBEAT.md` is reserved for the later
scheduler milestone, and `MEMORY.md` is always labeled as data.

Episodic memory writes are explicit through `POST /memories`; model execution
cannot overwrite identity or curated memory files. Retrieval uses SQLite FTS5.
Context is compiled immediately before dispatch, persisted with a provenance
manifest, and bounded by deterministic character budgets. A resumed AGY
conversation does not receive duplicated channel history.

## Security status

The live spike found that AGY's terminal sandbox can reset transiently. Milestone
2 therefore adds external rootless Podman containment. The worker still needs a
separate adversarial hardening review before autonomous mode is enabled against
valuable workspaces.
