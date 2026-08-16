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

From a reviewed release artifact:

```bash
packaging/install.sh
gravityclaw doctor --json
```

`setup` creates the database, identity templates, secure directories, control
credential, and a user service unit. It never reads, copies, or automates AGY
authentication. Run the official `agy` login flow separately, then run doctor
again. Telegram remains disabled until its token file and allowed user ID are
configured.

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

1. Install only the documented artifact.
2. Run setup and doctor.
3. Authenticate official AGY and verify a tool/subagent run.
4. Start the user service and verify Web plus Telegram.
5. Verify memory, context, schedules, backup, and restore.
6. Install a newer candidate and verify migration plus rollback.
7. Restore onto a second clean account; re-provision secrets and AGY auth.
8. Re-run the critical M2–M9 gates and inspect for secret leakage.

The automated repository test suite validates the filesystem/config/backup/release
parts. Real AGY authentication and Telegram delivery remain explicit operator
steps because credentials must stay inside their official boundaries.
