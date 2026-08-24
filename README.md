<p align="center">
  <img src="https://img.shields.io/badge/version-0.10.0-blue" alt="Version">
  <img src="https://img.shields.io/badge/python-%E2%89%A53.12-3776AB?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  <img src="https://img.shields.io/github/stars/AhmadSid110/gravityclaw?style=social" alt="Stars">
</p>

# GravityClaw

**Your AI agent shouldn't forget who you are every time it wakes up.**

GravityClaw is a self-hosted orchestration platform that gives AI agents persistent identity, long-term memory, durable task execution, and real-world integrations — all under your control, on your hardware.

It wraps Google's [Antigravity CLI](https://antigravity.dev) with everything a personal agent actually needs to be *useful* over time: context that survives sessions, scheduling that runs without you, and channels that meet you where you are.

---

## Why GravityClaw?

Most agent frameworks give you a stateless loop: prompt in, response out, amnesia. GravityClaw is different:

| Problem | GravityClaw's Answer |
|---------|---------------------|
| Agent forgets everything between sessions | **Episodic memory** with SQLite FTS5 — searchable, curated, bounded |
| No way to run tasks in the background | **TaskFlow** — durable multi-step orchestration with dependency graphs, retry, and progress tracking |
| Agent can't reach you proactively | **Scheduling engine** — cron, intervals, heartbeats, one-shots with misfire policies |
| Agent runs with full system access | **Rootless Podman isolation** — containerized execution by default |
| Can't interact from your phone | **Telegram channel** with authorized sender enforcement (more channels coming) |
| No visibility into what happened | **Web console** — live run inspection, memory studio, context inspector, capability management |
| Skills and tools are unmanaged | **Skill & MCP governance** — workspace-scoped with trust policies |

---

## What You Get

- **Persistent identity** — your agent has a name, a personality, and continuity across restarts
- **Long-term memory** — FTS5-indexed episodic memory with automatic curation and bounded context compilation
- **Durable scheduling** — cron jobs, intervals, heartbeats, and one-shot timers that survive reboots
- **TaskFlow orchestration** — complex multi-step workflows with dependency resolution, retry logic, and real-time progress
- **Learning engine** — extracts reusable knowledge from past runs; your agent gets better over time
- **Execution isolation** — rootless Podman containers keep the agent sandboxed from your system
- **Web console** — React/Vite dashboard with run inspection, memory browsing, context visualization, and task management
- **Channel integrations** — Telegram today, extensible to any messaging platform
- **Token auth** — bearer-token control plane with cookie-based web sessions (tokens never hit browser storage)
- **One-command deploy** — install script, Docker Compose, or manual setup — your choice

---

## Quick Start

### Option 1: One-liner (Ubuntu 22+, Debian 12+, Fedora 39+)

```bash
curl -sSL https://raw.githubusercontent.com/AhmadSid110/gravityclaw/main/deploy/install.sh | bash
gravityclaw doctor
gravityclaw start
# → http://localhost:8787
```

### Option 2: Docker Compose

```bash
git clone https://github.com/AhmadSid110/gravityclaw.git && cd gravityclaw
cp .env.example .env
mkdir -p secrets && openssl rand -base64 36 > secrets/control-token

docker compose up -d
# → http://localhost:8787
```

Add HTTPS with one line:

```bash
echo "GRAVITYCLAW_DOMAIN=agent.yourdomain.com" >> .env
docker compose --profile with-proxy up -d
```

### Option 3: Manual Install

```bash
git clone https://github.com/AhmadSid110/gravityclaw.git && cd gravityclaw
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'

cd web && npm install && npm run build && cd ..

# Optional: container isolation for the worker
podman build -f worker/Containerfile.agy -t localhost/gravityclaw-agy:1.1.13 .

.venv/bin/gravityclaw setup
.venv/bin/gravityclaw doctor
.venv/bin/gravityclaw start
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Web Console                           │
│              React / Vite / TypeScript                   │
│   Run Inspector • Memory Studio • TaskFlow Dashboard    │
└──────────────────────────┬──────────────────────────────┘
                           │ HTTP / WebSocket
┌──────────────────────────▼──────────────────────────────┐
│                  GravityClaw Core                        │
│            FastAPI · Python ≥3.12 · SQLite              │
│                                                          │
│  Identity & Memory ───── Context Compiler                │
│  Run Manager ──────────── Scheduler (cron/heartbeat)     │
│  TaskFlow Orchestrator ── Learning Engine & Curator       │
│  Skill Registry ───────── MCP Governance                 │
│  Channel Manager ──────── Telegram (+ extensible)        │
└──────────────────────────┬──────────────────────────────┘
                           │ subprocess / Podman
┌──────────────────────────▼──────────────────────────────┐
│                   AGY Worker                             │
│     Antigravity CLI in rootless Podman container         │
│        Reasoning • Tool Execution • Sandbox             │
└─────────────────────────────────────────────────────────┘
```

---

## Configuration

After setup, edit `~/.config/gravityclaw/gravityclaw.toml`:

```toml
[server]
host = "127.0.0.1"
port = 8787

[execution]
mode = "agy"
target = "container"       # "host" for direct execution
worker_image = "localhost/gravityclaw-agy:1.1.13"

[control]
token_file = "~/.config/gravityclaw/secrets/control-token"

[telegram]
enabled = true
token_file = "~/.config/gravityclaw/secrets/telegram-token"
allowed_user_id = "123456789"

[learning]
enabled = true
```

---

## Authentication

GravityClaw uses bearer-token authentication for its control plane:

```bash
# Generate a control token
openssl rand -base64 36 > ~/.config/gravityclaw/secrets/control-token
```

- **Web console**: login screen → validates token → sets secure cookie (never stored in localStorage)
- **API clients**: `Authorization: Bearer <token>` header
- **Health check**: `/health` is always public
- Everything else requires auth when a control token is configured

---

## Security

Self-hosted means you own your data. GravityClaw takes that seriously:

- Worker processes run in **rootless Podman containers** by default
- AGY credentials are **bind-mounted read-only** — never copied or exported
- Systemd unit runs with `NoNewPrivileges=true` and `PrivateTmp=true`
- All secrets stored as files with `0600` permissions
- Port 8787 should **never** be exposed directly — use the included Caddy profile or your own reverse proxy
- Identity and memory files are protected from model-execution overwrites

---

## Requirements

| Component | Version |
|-----------|---------|
| Python | ≥ 3.12 |
| Node.js | ≥ 18 (web console build) |
| Podman | Rootless (container mode) |
| AGY | Authenticated on host |

---

## Roadmap

- [x] **M1–M10**: Core platform, memory, scheduling, channels, console, security, XDG install
- [ ] **M11** (active): TaskFlow orchestration, curator, telemetry, Console IA v2
- [ ] **M12**: Multi-channel (Discord, Signal, WhatsApp)
- [ ] **M13**: Multi-agent coordination
- [ ] **M14**: Plugin marketplace

---

## Contributing

GravityClaw is open source under the MIT license. PRs welcome.

```bash
git clone https://github.com/AhmadSid110/gravityclaw.git
cd gravityclaw
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
pytest
```

---

## License

MIT — do what you want with it.

---

<p align="center">
  <sub>Built by <a href="https://github.com/AhmadSid110">@AhmadSid110</a></sub>
</p>
