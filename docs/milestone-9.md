# Milestone 9 — Security and Operational Hardening

M9 hardens the validated GravityClaw runtime without changing its agent or
channel semantics. The control plane remains the source of truth; workers are
still disposable, rootless, externally isolated execution environments.

## Security hardening

- Podman workers retain a read-only root filesystem, dropped capabilities,
  `no-new-privileges`, private IPC/UTS namespaces, memory/CPU/PID limits, and
  bounded container logs.
- Networked AGY workers use rootless slirp networking with host loopback access
  disabled. Networkless workers use `--network none`.
- Secret values are passed through a short-lived mode-0600 env file instead of
  appearing in the Podman command arguments. The file is removed immediately
  after worker creation.
- Telegram and control-token files, as well as capability secret files, must not
  be group/world accessible.
- OpenAPI/Swagger/ReDoc routes require the control-plane credential whenever
  authentication is enabled. Health and session-login remain public by design.
- Capability snapshots continue to contain secret references only; worker
  snapshots remain immutable after launch.

## Operations

`gravityclaw-ops` provides safe maintenance actions:

```bash
gravityclaw-ops health --database /path/to/gravityclaw.db
gravityclaw-ops backup --home /path/to/.gravityclaw --output /safe/backup.tar.gz
gravityclaw-ops verify --archive /safe/backup.tar.gz
gravityclaw-ops restore --archive /safe/backup.tar.gz --target /new/.gravityclaw
```

Backups use SQLite's consistent backup API, omit live WAL/SHM sidecars, reject
symlinks and special files, validate archive paths against traversal, verify
SQLite integrity before completion, and never overwrite an existing restore
target.

## Verification

- 83 unit/integration tests pass.
- Rootless Podman M2 crash, reconciliation, cancellation, and workspace
  isolation gate passes.
- M7 capability publication, secret non-persistence, and workspace-scope gate
  passes.
- Backup → verify → restore round-trip passes with WAL and integrity checks.
- `pip check` reports no broken requirements.
- Frontend production build passes and `npm audit --omit=dev --audit-level=high`
  reports zero vulnerabilities.
- Prior M1–M8B regression tests remain green.

## Deliberate boundaries

The worker network profile permits required external AGY/MCP access but blocks
host loopback; domain-level egress allowlisting remains deployment-specific.
AGY's private authentication volume is not included in GravityClaw backups.
Production deployments should use a mode-0600 control-token file and set
`GRAVITYCLAW_COOKIE_SECURE=1` behind HTTPS.
