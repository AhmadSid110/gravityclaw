# Milestone 7 — Skills and MCP capability governance

Status: verified

M7 gives GravityClaw ownership of capability selection while leaving reasoning
and tool use to native AGY. A run receives an immutable capability snapshot;
registry changes never mutate a worker that has already started.

## Delivered

- Schema v7 registry for native AGY skills, MCP servers, workspace bindings,
  health state, and per-run capability manifests.
- Skill discovery under `.agents/skills/` and validation of UTF-8 `SKILL.md`
  files, hashes, versions, profiles, and workspace scope.
- MCP registration for stdio, SSE, and HTTP transports with normalized config
  hashes and health states: `UNKNOWN`, `HEALTHY`, `DEGRADED`, `UNAVAILABLE`,
  and `MISCONFIGURED`.
- Workspace/profile capability selection with enable/disable controls.
- Atomic per-run snapshots containing native `.agents/skills` layout and
  `mcp_config.json`.
- AGY `--add-dir` integration and read-only MCP config overlay in the worker.
- Secret references such as `secret:github-token`; values are resolved only
  while constructing the worker environment and never enter SQLite, manifests,
  events, or error command representations.
- Immutable capability manifests with stable hashes and inspectable run API.
- Adoption of a complete published snapshot after a crash between directory
  publication and SQLite persistence.
- Capability API endpoints for registration, listing, enable/disable, binding,
  health checks, and run inspection.

## Security boundary

The persistent AGY home volume remains the authentication boundary. Capability
material is mounted per worker. Workspace-scoped skills/MCP servers are not
selected for other workspaces, and shared registry/configuration changes are
serialized through SQLite plus atomic snapshot publication.

No marketplace, arbitrary download, OAuth management, or remote plugin
distribution is included.

## Verification

```text
Full regression suite                    66 tests PASS
50 simultaneous worker snapshots        PASS
Workspace capability isolation           PASS
Secret absent from SQLite/manifests      PASS
SIGKILL before snapshot rename           PASS
SIGKILL after rename before SQLite       PASS
Atomic re-publication/adoption           PASS
SQLite WAL and integrity                 PASS
```

The deterministic gate is [m7_capabilities.py](../acceptance/m7_capabilities.py).
