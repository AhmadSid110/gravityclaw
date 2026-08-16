"""Governed native AGY skills/MCP capabilities and per-run snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from .execution import ContainerSpec
from .store import Conversation, RunRecord, Store, Workspace, utc_now


HEALTH_STATES = ("UNKNOWN", "HEALTHY", "DEGRADED", "UNAVAILABLE", "MISCONFIGURED")
PROFILES = {"chat", "coding", "heartbeat", "scheduled"}
SECRET_REF_PREFIX = "secret:"


class CapabilityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SkillRecord:
    id: str
    name: str
    path: Path
    scope: str
    workspace_id: str | None
    enabled: bool
    profiles: tuple[str, ...]
    sha256: str
    version: str
    validation_state: str
    validation_error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class MCPRecord:
    id: str
    name: str
    transport: str
    command: str | None
    url: str | None
    args: tuple[str, ...]
    env_refs: dict[str, str]
    enabled: bool
    scope: str
    workspace_id: str | None
    config_hash: str
    health_state: str
    health_error: str | None
    last_checked_at: str | None
    created_at: str
    updated_at: str


class CapabilityManager:
    """Registry plus immutable runtime snapshot publisher.

    Registry rows contain only configuration and secret references. Secret
    values are resolved at worker construction time and never enter a run
    request, manifest, event, or SQLite row.
    """

    def __init__(self, home: Path, store: Store, *, secret_dir: Path | None = None) -> None:
        self.home = home
        self.store = store
        self.secret_dir = secret_dir
        self.snapshot_root = home / "capability-snapshots"
        self.snapshot_root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def register_skill(
        self, *, skill_id: str, name: str, path: Path,
        workspace_id: str | None = None, profiles: Sequence[str] = (),
        version: str = "unversioned",
    ) -> SkillRecord:
        _validate_capability_id(skill_id)
        if not name.strip():
            raise CapabilityError("skill id and name are required")
        profile_values = tuple(sorted(set(profiles)))
        if any(item not in PROFILES for item in profile_values):
            raise CapabilityError("skill contains an unknown context profile")
        scope = "workspace" if workspace_id else "global"
        root = self.store.get_workspace(workspace_id).path if workspace_id else self.home / "skills"
        resolved = self._safe_path(path, root)
        state, error = _validate_skill(resolved)
        digest = _tree_hash(resolved) if resolved.exists() else "missing"
        now = utc_now()
        with self.store._connect() as connection:
            connection.execute(
                """INSERT INTO skills(
                   id, name, path, scope, workspace_id, enabled, profiles_json,
                   sha256, version, validation_state, validation_error, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name, path=excluded.path, scope=excluded.scope,
                   workspace_id=excluded.workspace_id, profiles_json=excluded.profiles_json,
                   sha256=excluded.sha256, version=excluded.version,
                   validation_state=excluded.validation_state,
                   validation_error=excluded.validation_error, updated_at=excluded.updated_at""",
                (skill_id, name.strip(), str(resolved), scope, workspace_id,
                 json.dumps(profile_values, separators=(",", ":")), digest, version,
                 state, error, now, now),
            )
        return self.get_skill(skill_id)

    def discover_skills(self, workspace_id: str) -> list[SkillRecord]:
        workspace = self.store.get_workspace(workspace_id)
        candidates: list[tuple[Path, str | None]] = []
        local = workspace.path / ".agents" / "skills"
        if local.is_dir():
            candidates.extend((item, workspace_id) for item in sorted(local.iterdir()) if item.is_dir())
        global_root = self.home / "skills"
        if global_root.is_dir():
            candidates.extend((item, None) for item in sorted(global_root.iterdir()) if item.is_dir())
        records: list[SkillRecord] = []
        for path, scope_workspace in candidates:
            skill_id = f"workspace:{workspace_id}:{path.name}" if scope_workspace else f"global:{path.name}"
            records.append(self.register_skill(
                skill_id=skill_id, name=path.name, path=path,
                workspace_id=scope_workspace,
            ))
        return records

    def get_skill(self, skill_id: str) -> SkillRecord:
        with self.store._connect() as connection:
            row = connection.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
        if row is None:
            raise KeyError(f"skill not found: {skill_id}")
        return _skill(row)

    def list_skills(self, workspace_id: str | None = None) -> list[SkillRecord]:
        with self.store._connect() as connection:
            rows = connection.execute("SELECT * FROM skills ORDER BY id").fetchall()
        records = [_skill(row) for row in rows]
        return [item for item in records if workspace_id is None or item.workspace_id in {None, workspace_id}]

    def set_skill_enabled(self, skill_id: str, enabled: bool) -> SkillRecord:
        with self.store._connect() as connection:
            cursor = connection.execute(
                "UPDATE skills SET enabled=?, updated_at=? WHERE id=?",
                (int(enabled), utc_now(), skill_id),
            )
        if cursor.rowcount != 1:
            raise KeyError(f"skill not found: {skill_id}")
        return self.get_skill(skill_id)

    def register_mcp(
        self, *, server_id: str, name: str, transport: str,
        command: str | None = None, url: str | None = None,
        args: Sequence[str] = (), env_refs: Mapping[str, str] | None = None,
        workspace_id: str | None = None,
    ) -> MCPRecord:
        _validate_capability_id(server_id)
        if transport not in {"stdio", "sse", "http"}:
            raise CapabilityError("MCP transport must be stdio, sse, or http")
        if transport == "stdio" and (not command or url):
            raise CapabilityError("stdio MCP requires command and forbids url")
        if transport in {"sse", "http"} and (not url or command):
            raise CapabilityError("remote MCP requires url and forbids command")
        if url and urlparse(url).scheme not in {"http", "https"}:
            raise CapabilityError("MCP url must use http or https")
        refs = {str(key): str(value) for key, value in (env_refs or {}).items()}
        for key, value in refs.items():
            if not key or not value.startswith(SECRET_REF_PREFIX) or len(value) <= len(SECRET_REF_PREFIX):
                raise CapabilityError(f"invalid secret reference for MCP environment: {key}")
            _validate_secret_name(value[len(SECRET_REF_PREFIX):])
        config = {"transport": transport, "command": command, "url": url,
                  "args": list(args), "env_refs": refs}
        digest = _hash_json(config)
        scope = "workspace" if workspace_id else "global"
        if workspace_id:
            self.store.get_workspace(workspace_id)
        now = utc_now()
        with self.store._connect() as connection:
            connection.execute(
                """INSERT INTO mcp_servers(
                   id, name, transport, command, url, args_json, env_refs_json,
                   enabled, scope, workspace_id, config_hash, health_state,
                   created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 'UNKNOWN', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name, transport=excluded.transport,
                   command=excluded.command, url=excluded.url, args_json=excluded.args_json,
                   env_refs_json=excluded.env_refs_json, scope=excluded.scope,
                   workspace_id=excluded.workspace_id, config_hash=excluded.config_hash,
                   health_state='UNKNOWN', health_error=NULL, last_checked_at=NULL,
                   updated_at=excluded.updated_at""",
                (server_id, name.strip(), transport, command, url,
                 json.dumps(list(args), separators=(",", ":")),
                 json.dumps(refs, separators=(",", ":")), scope, workspace_id,
                 digest, now, now),
            )
        return self.get_mcp(server_id)

    def get_mcp(self, server_id: str) -> MCPRecord:
        with self.store._connect() as connection:
            row = connection.execute("SELECT * FROM mcp_servers WHERE id=?", (server_id,)).fetchone()
        if row is None:
            raise KeyError(f"MCP server not found: {server_id}")
        return _mcp(row)

    def list_mcp(self, workspace_id: str | None = None) -> list[MCPRecord]:
        with self.store._connect() as connection:
            rows = connection.execute("SELECT * FROM mcp_servers ORDER BY id").fetchall()
        records = [_mcp(row) for row in rows]
        return [item for item in records if workspace_id is None or item.workspace_id in {None, workspace_id}]

    def set_mcp_enabled(self, server_id: str, enabled: bool) -> MCPRecord:
        with self.store._connect() as connection:
            cursor = connection.execute(
                "UPDATE mcp_servers SET enabled=?, updated_at=? WHERE id=?",
                (int(enabled), utc_now(), server_id),
            )
        if cursor.rowcount != 1:
            raise KeyError(f"MCP server not found: {server_id}")
        return self.get_mcp(server_id)

    def bind(self, *, workspace_id: str, capability_type: str,
             capability_id: str, profile: str = "*") -> None:
        if capability_type not in {"skill", "mcp"}:
            raise CapabilityError("capability type must be skill or mcp")
        if profile != "*" and profile not in PROFILES:
            raise CapabilityError("unknown capability profile")
        self.store.get_workspace(workspace_id)
        table = self.get_skill if capability_type == "skill" else self.get_mcp
        table(capability_id)
        now = utc_now()
        with self.store._connect() as connection:
            connection.execute(
                """INSERT INTO capability_bindings(
                   id, workspace_id, capability_type, capability_id, profile,
                   enabled, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(workspace_id, capability_type, capability_id, profile)
                DO UPDATE SET enabled=1, updated_at=excluded.updated_at""",
                (f"{workspace_id}:{capability_type}:{capability_id}:{profile}",
                 workspace_id, capability_type, capability_id, profile, now, now),
            )

    def set_binding_enabled(self, *, workspace_id: str, capability_type: str,
                            capability_id: str, profile: str, enabled: bool) -> None:
        with self.store._connect() as connection:
            cursor = connection.execute(
                "UPDATE capability_bindings SET enabled=?, updated_at=? WHERE "
                "workspace_id=? AND capability_type=? AND capability_id=? AND profile=?",
                (int(enabled), utc_now(), workspace_id, capability_type, capability_id, profile),
            )
        if cursor.rowcount != 1:
            raise KeyError("capability binding not found")

    def prepare_run(self, run: RunRecord, conversation: Conversation,
                    workspace: Workspace) -> RunRecord:
        profile = str(run.request.get("context_profile", "chat"))
        skills = self._selected_skills(workspace.id, profile)
        mcps = self._selected_mcp(workspace.id, profile)
        final = self.snapshot_root / run.id
        manifest = self._manifest(run, workspace, profile, skills, mcps)
        if final.exists():
            published_file = final / "manifest.json"
            if not published_file.is_file():
                raise CapabilityError("capability snapshot is incomplete")
            published = json.loads(published_file.read_text(encoding="utf-8"))
            if published.get("manifest_hash") != manifest.get("manifest_hash"):
                raise CapabilityError("existing capability snapshot does not match registry")
            try:
                existing = self.store.get_capability_manifest(run.id)
            except KeyError:
                # SIGKILL can land after directory publication but before the
                # SQLite manifest transaction. The complete published snapshot
                # is authoritative and can be adopted on restart.
                existing = published
            if existing.get("manifest_hash") != manifest["manifest_hash"]:
                raise CapabilityError("existing capability snapshot does not match registry")
            return self.store.prepare_run_capabilities(run.id, existing, str(final))
        temporary = Path(tempfile.mkdtemp(prefix=f".{run.id}.", dir=self.snapshot_root))
        try:
            skill_root = temporary / "skills"
            skill_root.mkdir(mode=0o700)
            native_skill_root = skill_root / ".agents" / "skills"
            native_skill_root.mkdir(mode=0o700, parents=True)
            for skill in skills:
                shutil.copytree(
                    skill.path, native_skill_root / skill.id.replace(":", "_"),
                    symlinks=False,
                )
            mcp_config = {"mcpServers": {}}
            for server in mcps:
                entry: dict[str, Any] = {"args": list(server.args)}
                if server.transport == "stdio":
                    entry["command"] = server.command
                else:
                    entry["url"] = server.url
                entry["env"] = {
                    key: "${" + _secret_env_name(ref) + "}"
                    for key, ref in sorted(server.env_refs.items())
                }
                mcp_config["mcpServers"][server.id] = entry
            _atomic_json(temporary / "mcp_config.json", mcp_config, mode=0o600)
            _atomic_json(temporary / "manifest.json", manifest, mode=0o600)
            os.replace(temporary, final)
        except FileExistsError:
            shutil.rmtree(temporary, ignore_errors=True)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return self.store.prepare_run_capabilities(run.id, manifest, str(final))

    def apply_to_spec(self, spec: ContainerSpec, run: RunRecord) -> ContainerSpec:
        manifest = run.request.get("capability_manifest")
        snapshot = run.request.get("capability_snapshot")
        if not isinstance(manifest, dict) or not snapshot:
            return spec
        root = Path(str(snapshot)).resolve()
        if not root.is_dir() or not (root / "manifest.json").is_file():
            raise CapabilityError("capability snapshot is missing")
        environment = dict(spec.environment)
        for server in manifest.get("mcp", []):
            for ref in server.get("secret_refs", []):
                name = str(ref).split(":", 1)[1]
                environment[_secret_env_name(str(ref))] = self.resolve_secret(str(ref))
        mounts = list(spec.mounts)
        skills = root / "skills"
        if skills.is_dir() and manifest.get("skills"):
            mounts.append((skills, "/gravityclaw/capabilities/skills", "ro"))
        if manifest.get("mcp"):
            mounts.append((root / "mcp_config.json", "/home/worker/.gemini/config/mcp_config.json", "ro"))
        command = list(spec.command)
        if manifest.get("skills") and command and Path(command[0]).name in {"agy", "antigravity"}:
            command.extend(["--add-dir", "/gravityclaw/capabilities/skills"])
        return replace(spec, command=tuple(command), mounts=tuple(mounts), environment=environment)

    def resolve_secret(self, reference: str) -> str:
        if not reference.startswith(SECRET_REF_PREFIX):
            raise CapabilityError("only secret:name references are accepted")
        name = reference[len(SECRET_REF_PREFIX):]
        _validate_secret_name(name)
        env_name = _secret_env_name(reference)
        value = os.environ.get(env_name)
        if value:
            return value
        if self.secret_dir:
            path = (self.secret_dir / name).resolve()
            if path.parent != self.secret_dir.resolve():
                raise CapabilityError("secret path escaped secret directory")
            if path.is_file():
                return path.read_text(encoding="utf-8").strip()
        raise CapabilityError(f"secret is not available: {reference}")

    def health_check(self, server_id: str) -> MCPRecord:
        server = self.get_mcp(server_id)
        state = "UNKNOWN"
        error: str | None = None
        if server.transport == "stdio":
            if server.command and shutil.which(server.command):
                state = "HEALTHY"
            else:
                state, error = "UNAVAILABLE", "stdio command is not installed"
        elif server.url and urlparse(server.url).scheme in {"http", "https"}:
            # Remote probing is deliberately not implicit; reachability can
            # change independently of registry validity.
            state = "UNKNOWN"
        else:
            state, error = "MISCONFIGURED", "invalid MCP URL"
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE mcp_servers SET health_state=?, health_error=?, last_checked_at=?, updated_at=? WHERE id=?",
                (state, error, utc_now(), utc_now(), server_id),
            )
        return self.get_mcp(server_id)

    def _selected_skills(self, workspace_id: str, profile: str) -> list[SkillRecord]:
        selected: list[SkillRecord] = []
        for item in self.list_skills(workspace_id):
            if not item.enabled or item.validation_state != "VALID":
                continue
            if item.profiles and profile not in item.profiles:
                continue
            if item.workspace_id not in {None, workspace_id}:
                continue
            if not self._binding_allows(workspace_id, "skill", item.id, profile):
                continue
            selected.append(item)
        return sorted(selected, key=lambda item: item.id)

    def _selected_mcp(self, workspace_id: str, profile: str) -> list[MCPRecord]:
        selected: list[MCPRecord] = []
        for item in self.list_mcp(workspace_id):
            if not item.enabled or item.health_state in {"UNAVAILABLE", "MISCONFIGURED"}:
                continue
            if item.workspace_id not in {None, workspace_id}:
                continue
            if not self._binding_allows(workspace_id, "mcp", item.id, profile):
                continue
            selected.append(item)
        return sorted(selected, key=lambda item: item.id)

    def _binding_allows(self, workspace_id: str, kind: str, capability_id: str, profile: str) -> bool:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT enabled FROM capability_bindings WHERE workspace_id=? "
                "AND capability_type=? AND capability_id=? AND profile IN ('*',?)",
                (workspace_id, kind, capability_id, profile),
            ).fetchall()
        return not rows or any(bool(row["enabled"]) for row in rows)

    @staticmethod
    def _safe_path(path: Path, root: Path) -> Path:
        resolved = path.resolve()
        root_resolved = root.resolve()
        if not resolved.is_relative_to(root_resolved):
            raise CapabilityError("capability path is outside its approved root")
        return resolved

    def _manifest(self, run: RunRecord, workspace: Workspace, profile: str,
                  skills: Sequence[SkillRecord], mcps: Sequence[MCPRecord]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": 1, "run_id": run.id, "workspace_id": workspace.id,
            "workspace": workspace.name, "profile": profile,
            "skills": [{"id": item.id, "name": item.name, "sha256": item.sha256,
                         "version": item.version} for item in skills],
            "mcp": [{"id": item.id, "name": item.name, "transport": item.transport,
                     "config_hash": item.config_hash, "health_state": item.health_state,
                     "secret_refs": [item.env_refs[key] for key in sorted(item.env_refs)]}
                    for item in mcps],
            "network_profile": "mcp" if any(item.transport != "stdio" for item in mcps) else "none",
            "permission_profile": "autonomous" if run.request.get("allow_all") else "native",
            "secret_refs": sorted({ref for item in mcps for ref in item.env_refs.values()}),
        }
        hash_payload = dict(payload)
        hash_payload.pop("run_id", None)
        payload["manifest_hash"] = _hash_json(hash_payload)
        return payload


def _validate_secret_name(name: str) -> None:
    if not name or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for char in name):
        raise CapabilityError("invalid secret name")


def _validate_capability_id(value: str) -> None:
    if not value or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-" for char in value):
        raise CapabilityError("invalid capability id")


def _secret_env_name(reference: str) -> str:
    name = reference.split(":", 1)[1]
    return "GRAVITYCLAW_SECRET_" + name.upper().replace("-", "_").replace(".", "_")


def _validate_skill(path: Path) -> tuple[str, str | None]:
    if not path.is_dir():
        return "MISSING", "skill directory does not exist"
    skill_file = path / "SKILL.md"
    if not skill_file.is_file():
        return "INVALID", "SKILL.md is missing"
    try:
        if not skill_file.read_text(encoding="utf-8").strip():
            return "INVALID", "SKILL.md is empty"
    except UnicodeDecodeError:
        return "INVALID", "SKILL.md is not UTF-8"
    return "VALID", None


def _tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise CapabilityError(f"skill symlinks are not allowed: {item}")
        if item.is_file():
            digest.update(str(item.relative_to(path)).encode())
            digest.update(item.read_bytes())
    return digest.hexdigest()


def _hash_json(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _atomic_json(path: Path, value: object, *, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _skill(row: Any) -> SkillRecord:
    return SkillRecord(
        row["id"], row["name"], Path(row["path"]), row["scope"], row["workspace_id"],
        bool(row["enabled"]), tuple(json.loads(row["profiles_json"])), row["sha256"],
        row["version"], row["validation_state"], row["validation_error"],
        row["created_at"], row["updated_at"],
    )


def _mcp(row: Any) -> MCPRecord:
    return MCPRecord(
        row["id"], row["name"], row["transport"], row["command"], row["url"],
        tuple(json.loads(row["args_json"])), json.loads(row["env_refs_json"]),
        bool(row["enabled"]), row["scope"], row["workspace_id"], row["config_hash"],
        row["health_state"], row["health_error"], row["last_checked_at"],
        row["created_at"], row["updated_at"],
    )
