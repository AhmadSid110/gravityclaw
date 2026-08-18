# Milestone 10 — Packaging and deployment

M10 establishes the canonical single-user deployment path:

`Ubuntu/Debian → user-owned Python install → systemd user service → rootless Podman → official agy → private Web UI/Telegram`

## Canonical layout

```text
~/.config/gravityclaw/
├── gravityclaw.toml
├── release-manifest.json
├── identity/{SOUL,USER,AGENTS,TOOLS,HEARTBEAT,MEMORY}.md
└── capabilities/

~/.local/share/gravityclaw/
├── gravityclaw.db
├── memory/
├── workspaces/
├── artifacts/
└── backups/

~/.local/state/gravityclaw/logs/
~/.local/lib/gravityclaw/{releases,current,previous,venv}/
${XDG_RUNTIME_DIR}/gravityclaw/secrets/
```

The application keeps a compatibility path through `GRAVITYCLAW_HOME`; legacy
development installations do not need an immediate migration.

## Install and first run

From a reviewed release artifact that includes the built `web/dist` bundle:

```bash
npm --prefix web run build     # release preparation, not a production service
packaging/install.sh
gravityclaw doctor --json
```

The wheel packages that bundle as `gravityclaw/web_dist`; the installer refuses
an artifact without `web/dist/index.html`. `setup` creates the database, identity
templates, secure directories, control credential, and a user service unit. The
unit starts one `gravityclaw.server` gateway process. That process serves the
React UI, `/api/*`, and `/ws/*` from `127.0.0.1:8787`; no Node or Vite process is
needed in production. It never reads, copies, or automates AGY authentication.
Run the official `agy` login flow separately, then run doctor again. Telegram
remains disabled until its token file and allowed user ID are configured.

For development, run the same backend directly and use Vite only for HMR:

```bash
gravityclaw gateway --dev
cd web && npm run dev
```

Vite proxies `/api`, `/auth`, `/health`, and `/ws` to the backend. In production,
use only the combined gateway:

```bash
gravityclaw start
gravityclaw status
gravityclaw logs
gravityclaw restart
gravityclaw stop
```

The older `gravityclaw service <action>` spelling remains supported.

If the worker image is not supplied as part of the reviewed artifact, build it
from that artifact before starting the service:

```bash
gravityclaw worker build --source /path/to/release
```

Useful commands:

```bash
gravityclaw config validate
gravityclaw doctor
gravityclaw service start
gravityclaw service status
gravityclaw service logs
gravityclaw backup create --output ~/.local/share/gravityclaw/backups/pre-change.tar.gz
gravityclaw backup verify ~/.local/share/gravityclaw/backups/pre-change.tar.gz
```

The service binds to `127.0.0.1` by default. Use a private VPN or authenticated
reverse proxy if remote Web access is required; do not publish the control port
directly to the internet.

## Release provenance and rollback

```bash
gravityclaw release manifest --source /path/to/release
gravityclaw release upgrade --candidate /path/to/release --version 0.10.0
gravityclaw release rollback
```

Release switching uses a temporary directory and atomic symlink replacement.
The command reports that a service restart is required; it does not restart a
running gateway implicitly. A candidate must be reviewed and doctor-tested
before activation. The manifest records GravityClaw version, schema version,
AGY version, worker image reference/ID, and frontend digest without secrets.

## Clean-machine gate

The final M10 gate must run on a fresh Debian/Ubuntu user account with no source
checkout or existing GravityClaw data:

1. Install only the reviewed artifact, including its packaged `web/dist` bundle.
2. Run setup and doctor.
3. Authenticate official AGY and verify a tool/subagent run.
4. Run `gravityclaw start`; verify exactly one GravityClaw user service/process,
   no Node/Vite development server, and `GET /` returns the React shell.
5. Verify a history route such as `/conversations/123` returns the same shell,
   while an unknown `/api/*` path remains an API 404.
6. Verify the same `:8787` gateway serves API and WebSocket traffic, starts
   Telegram polling and the scheduler, and records startup reconciliation.
7. Verify `gravityclaw stop` shuts the gateway down cleanly and a user-service
   restart/reboot brings the single service back.
8. Verify memory, context, schedules, backup, and restore.
9. Install a newer candidate and verify migration plus rollback.
10. Restore onto a second clean account; re-provision secrets and AGY auth.
11. Re-run the critical M2–M9 gates and inspect for secret leakage.

The automated repository test suite validates the filesystem/config/backup/release
parts. Real AGY authentication and Telegram delivery remain explicit operator
steps because credentials must stay inside their official boundaries.
