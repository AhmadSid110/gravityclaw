<p align="center">
  <img src="https://github.com/AhmadSid110/gravityclaw/actions/workflows/ci.yml/badge.svg" alt="CI">
  <img src="https://img.shields.io/badge/version-0.10.0-blue" alt="Version">
  <img src="https://img.shields.io/badge/python-%E2%89%A53.12-3776AB?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  <img src="https://img.shields.io/github/stars/AhmadSid110/gravityclaw?style=social" alt="Stars">
</p>

# GravityClaw

**Your AI agent shouldn't forget who you are every time it wakes up.**

GravityClaw is a self-hosted orchestration platform that gives AI agents persistent identity, long-term memory, durable task execution, and real-world integrations — all under your control, on your hardware.

It runs Google's official [Antigravity CLI](https://antigravity.dev) locally as the reasoning engine, adding everything a personal agent actually needs to be *useful* over time: context that survives sessions, scheduling that runs without you, and channels that meet you where you are.

---

## Not a Proxy. Not an API Wrapper.

Let's be clear about what GravityClaw **is** and **isn't**:

| | GravityClaw | Proxy/Wrapper Services |
|---|---|---|
| **Your credentials** | Stay on your machine. Never transmitted anywhere. | Often stored on third-party servers |
| **Model access** | Runs the official AGY binary directly — same as typing `agy` in your terminal | Intercepts API calls through their infrastructure |
| **Data path** | You ↔ Google. Direct. No middleman. | You ↔ Their servers ↔ Google |
| **Multi-user** | Single-user, single-machine, personal use | Built to serve many users through one API key |
| **What it adds** | Memory, scheduling, channels *around* AGY — never *between* you and Google | Adds a billing/routing layer *between* you and the model |

GravityClaw is an orchestration layer that wraps the **official, unmodified AGY binary** the same way a terminal multiplexer wraps your shell, or a cron job wraps a CLI tool. It doesn't intercept, modify, or relay your authentication. It doesn't expose Google's API to third parties. It doesn't pool tokens or resell access.

**Your AGY subscription (Pro or Ultra) works exactly as Google intended** — GravityClaw just makes the agent persistent, scheduled, and reachable.

---

## Works With Your Google AI Plan

GravityClaw is designed for users with their own Google AI subscription:

- **Google AI Pro** ($20/month) — full Antigravity CLI access with generous quotas
- **Google AI Ultra** ($100–200/month) — 20× higher usage limits, priority access

You authenticate AGY once on your machine using Google's official OAuth flow. GravityClaw bind-mounts those credentials read-only into isolated containers — it never copies, exports, or transmits them. Your usage counts against your personal quota just like running AGY directly.

> **No subscription required to try GravityClaw itself** — the platform is free and open source. You only need a Google AI plan for the AGY reasoning engine.

---

## Terms of Service Notice

Google's [Antigravity Additional Terms of Service](https://antigravity.google/terms) restrict accessing the service through third-party software. GravityClaw runs the official binary directly and does not proxy, relay, or intercept API traffic — but users should review Google's terms and make their own assessment of compliance. This project is provided as-is under MIT license; the authors make no legal guarantees regarding third-party service terms.

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
- **Data portability** — your data and orchestration state are portable. Migrate anytime.

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

## Privacy & Data Sovereignty

GravityClaw keeps your orchestration data local. The platform itself sends no telemetry.

- **No GravityClaw-hosted cloud backend** — runs entirely on your hardware (or your VPS). Model-provider connectivity is still required for reasoning.
- **GravityClaw itself sends no telemetry** — we don't collect usage data, crash reports, or analytics. The configured reasoning provider (Google) receives model requests as part of normal AGY operation.
- **No account required** — no sign-up, no SaaS backend, no "free tier" upsell
- **Memory stays on-disk** — SQLite database under your control, your backups, your encryption
- **Credentials never leave your machine** — AGY auth is bind-mounted read-only, never copied or transmitted

Your conversations, memories, tasks, and identity files belong to you. Full stop.

---

## Security

Self-hosted means you own your data. GravityClaw takes that seriously:

- Worker processes run in **rootless Podman containers** by default (explicit opt-in required for host execution)
- AGY credentials are **bind-mounted read-only** — never copied or exported
- Systemd unit runs with `NoNewPrivileges=true` and `PrivateTmp=true`
- All secrets stored as files with `0600` permissions
- Port 8787 binds to **127.0.0.1 only** by default — use the included Caddy profile or your own reverse proxy for public access
- Identity and memory files are protected from model-execution overwrites
- No network egress from the orchestration layer — only the AGY binary talks to Google

### Container Trust Boundary

```
┌───────────────────────────────────────────────────┐
│          Trusted GravityClaw Gateway              │
│  (FastAPI server — controls scheduling, memory,   │
│   identity, channels, and worker lifecycle)       │
│                                                    │
│         ┌──────────────┐                          │
│         │ Podman Socket│ ← only the gateway has   │
│         └──────┬───────┘   access to this socket  │
│                │                                   │
└────────────────┼──────────────────────────────────┘
                 │ creates / destroys
    ┌────────────▼────────────────────────┐
    │    Untrusted AGY Worker Containers  │
    │                                      │
    │  • No access to Podman socket        │
    │  • No access to host network         │
    │  • Read-only credential mount        │
    │  • Ephemeral filesystem              │
    │  • Cannot escalate to gateway        │
    └─────────────────────────────────────┘
```

Only the trusted GravityClaw gateway may control the rootless Podman socket. AGY worker containers never receive it — they are disposable, sandboxed execution units with no ability to spawn siblings, access host resources, or modify the orchestration layer.

---

## Requirements

| Component | Version |
|-----------|---------|
| Python | ≥ 3.12 |
| Node.js | ≥ 18 (web console build) |
| Podman | Rootless (container mode) |
| AGY | Authenticated on host |
| Google AI Plan | Pro ($20/mo) or Ultra ($100–200/mo) for AGY access |

---

## Who Is This For?

- **Developers** who want a personal AI that remembers their codebase, preferences, and ongoing projects
- **Power users** on Google AI Pro/Ultra who want more from their subscription than a chat window
- **Self-hosters** who refuse to send their data to yet another SaaS platform
- **Tinkerers** who want to build custom agent workflows without a PhD in prompt engineering
- **Privacy-conscious users** who want AI capabilities without the surveillance trade-off

---

## Roadmap

- [x] **M1–M10**: Core platform, memory, scheduling, channels, console, security, XDG install
- [x] **M11**: TaskFlow orchestration, curator, telemetry, Console IA v2
- [ ] **M12** (next): Multi-channel (Discord, Signal, WhatsApp)
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
