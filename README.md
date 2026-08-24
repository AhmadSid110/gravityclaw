# GravityClaw

Persistent personal-agent orchestration platform powered by the official [Antigravity CLI](https://antigravity.dev).

GravityClaw owns identity, memory, sessions, scheduling, channels, and orchestration. Google's Antigravity CLI (`agy`) handles reasoning and tool execution inside an isolated worker.

## Features

- **Durable agent sessions** — conversations with full context management, watermarks, summaries, and artifact references
- **Episodic memory** — SQLite FTS5-backed memory with bounded context compilation and curation
- **Scheduling** — one-shot, interval, cron, and heartbeat job types with misfire policies
- **Telegram channel** — interact with your agent from Telegram with authorized sender enforcement
- **Web console** — React/Vite dashboard with run inspection, memory studio, context inspector, capability management, and TaskFlow orchestration
- **Execution isolation** — rootless Podman containment for worker processes
- **Skill & MCP governance** — workspace-scoped skills and Model Context Protocol server management with trust policies
- **TaskFlow** — durable multi-step task orchestration with dependency graphs, retry, and progress tracking
- **Learning engine** — extracts reusable knowledge from agent runs with curator-driven lifecycle
- **Token-based auth** — control-plane protected by a bearer token; the web console has a login screen that establishes a secure session (never stored in browser local storage)

## Quick Start

### One-liner (Ubuntu 22+, Debian 12+, Fedora 39+)

```bash
curl -sSL https://raw.githubusercontent.com/AhmadSid110/gravityclaw/main/deploy/install.sh | bash
gravityclaw doctor
gravityclaw start
# → http://localhost:8787
```

### Docker Compose

```bash
git clone https://github.com/AhmadSid110/gravityclaw.git
cd gravityclaw
cp .env.example .env
# Edit .env — set your control token and (optional) Telegram token
mkdir -p secrets
openssl rand -base64 36 > secrets/control-token

docker compose up -d
# → http://localhost:8787
```

With automatic HTTPS via Caddy:

```bash
echo "GRAVITYCLAW_DOMAIN=gc.yourdomain.com" >> .env
docker compose --profile with-proxy up -d
```

### Manual Install

```bash
git clone https://github.com/AhmadSid110/gravityclaw.git
cd gravityclaw
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'

# Build web console
cd web && npm install && npm run build && cd ..

# Build worker image (optional, for container execution)
podman build -f worker/Containerfile.agy -t localhost/gravityclaw-agy:1.1.13 .

# Setup (creates config, DB, identity files, systemd unit)
.venv/bin/gravityclaw setup
.venv/bin/gravityclaw doctor
.venv/bin/gravityclaw start
```

## Authentication

GravityClaw protects its control plane with a bearer token. On first access, the web console presents a login screen where you paste the control token. The session is cookie-based and the token is never persisted in browser storage.

```bash
# Generate a control token
openssl rand -base64 36 > ~/.config/gravityclaw/secrets/control-token

# Or set via environment
export GRAVITYCLAW_CONTROL_TOKEN_FILE=/path/to/control-token
```

API clients authenticate with:
```
Authorization: Bearer <your-control-token>
```

The `/health` endpoint is always public. All other endpoints require authentication when a control token is configured.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Web Console                        │
│            React / Vite / TypeScript                 │
└────────────────────────┬────────────────────────────┘
                         │ HTTP / WebSocket
┌────────────────────────▼────────────────────────────┐
│               GravityClaw Core                       │
│  FastAPI control plane (Python ≥3.12)               │
│                                                      │
│  • Identity & Memory    • Scheduler                  │
│  • Context Compiler     • Channels (Telegram)        │
│  • Run Manager          • Learning Engine            │
│  • Capability Manager   • TaskFlow Orchestration     │
│  • Skill & MCP Registry • Curator                    │
└────────────────────────┬────────────────────────────┘
                         │ subprocess / Podman
┌────────────────────────▼────────────────────────────┐
│              AGY Worker                              │
│  Antigravity CLI in rootless container or host       │
│  (reasoning, tool execution, sandbox)                │
└─────────────────────────────────────────────────────┘
```

## Configuration

After `gravityclaw setup`, edit `~/.config/gravityclaw/gravityclaw.toml`:

```toml
[server]
host = "127.0.0.1"
port = 8787

[execution]
mode = "agy"
target = "host"            # or "container" for Podman isolation
worker_image = "localhost/gravityclaw-agy:1.1.13"

[control]
token_file = "~/.config/gravityclaw/secrets/control-token"

[telegram]
enabled = true
token_file = "~/.config/gravityclaw/secrets/telegram-token"
allowed_user_id = "your-telegram-user-id"

[learning]
enabled = true
```

## Requirements

- Python ≥ 3.12
- Node.js ≥ 18 (for web console build)
- Podman (rootless, for container execution mode)
- AGY (Antigravity CLI) — authenticated on the host

## Security

- Never expose port 8787 directly to the internet — use the Caddy profile or your own reverse proxy
- All secrets should be file-mode `0600`
- AGY credentials are bind-mounted read-only in container mode
- The systemd unit runs with `NoNewPrivileges=true` and `PrivateTmp=true`
- Worker processes execute inside rootless Podman containers by default

## Project Status

**Version 0.10.0** — validated through Milestone 10. Active development on M11 (TaskFlow, curator, telemetry).

## License

MIT

