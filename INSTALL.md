# Installing GravityClaw

Three deployment methods from simplest to most customizable.

## Method 1: One-Liner (Bare Metal)

Works on Ubuntu 22+, Debian 12+, Fedora 39+:

```bash
curl -sSL https://raw.githubusercontent.com/AhmadSid110/gravityclaw/main/deploy/install.sh | bash
```

This installs Python, Podman, Node.js, builds everything, creates XDG directories,
and sets up a systemd user service. After install:

```bash
gravityclaw doctor          # verify everything
gravityclaw start           # start the service
# → http://localhost:8787
```

## Method 2: Docker Compose

For isolated container deployment:

```bash
git clone https://github.com/AhmadSid110/gravityclaw.git
cd gravityclaw
cp .env.example .env
# Edit .env with your configuration

# Create secrets
mkdir -p secrets
openssl rand -base64 36 > secrets/control-token
# Add telegram token if needed:
# echo "your-bot-token" > secrets/telegram-token

docker compose up -d
# → http://localhost:8787
```

With automatic HTTPS (Caddy reverse proxy):

```bash
# Set your domain in .env
echo "GRAVITYCLAW_DOMAIN=gc.yourdomain.com" >> .env
docker compose --profile with-proxy up -d
# → https://gc.yourdomain.com (auto-TLS)
```

## Method 3: Manual Install

Full control over each step:

```bash
# 1. Clone
git clone https://github.com/AhmadSid110/gravityclaw.git
cd gravityclaw

# 2. Python environment
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'

# 3. Build web console
cd web && npm install && npm run build && cd ..

# 4. Build worker image (needs Podman)
podman build -f worker/Containerfile.agy -t localhost/gravityclaw-agy:1.1.13 .

# 5. Setup (creates config, DB, identity, systemd unit)
.venv/bin/gravityclaw setup

# 6. Verify
.venv/bin/gravityclaw doctor

# 7. Start
.venv/bin/gravityclaw start
# or directly:
GRAVITYCLAW_HOME="$PWD/.runtime" .venv/bin/python -m gravityclaw.server
```

## AGY Authentication

GravityClaw does not manage AGY credentials — you authenticate through the
official Antigravity CLI flow on the host. The credentials live in `~/.config/agy`
and are bind-mounted (read-only) in container mode.

## Configuration

After `gravityclaw setup`, edit `~/.config/gravityclaw/gravityclaw.toml`:

```toml
[server]
host = "127.0.0.1"
port = 8787

[execution]
target = "host"       # or "container" for Podman isolation
mode = "agy"
worker_image = "localhost/gravityclaw-agy:1.1.13"

[telegram]
enabled = true
allowed_user_id = "your-telegram-user-id"
```

## Updating

```bash
# Bare metal
cd ~/.local/lib/gravityclaw/source
git pull
cd ../../
./deploy/install.sh

# Docker
docker compose pull
docker compose up -d --build
```

## Security Notes

- Never expose port 8787 directly to the internet
- Use `GRAVITYCLAW_CONTROL_TOKEN_FILE` for any remote access
- The Caddy profile provides automatic HTTPS
- All secret files should be mode 0600
- The systemd unit runs with `NoNewPrivileges=true` and `PrivateTmp=true`
