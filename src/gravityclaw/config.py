"""Canonical filesystem layout and TOML configuration for GravityClaw."""

from __future__ import annotations

import os
import secrets
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIG_VERSION = 1
RELEASE_VERSION = "0.10.0"


class ConfigurationError(ValueError):
    """Raised when a GravityClaw configuration is unsafe or invalid."""


@dataclass(frozen=True, slots=True)
class RuntimeLayout:
    """The user-owned filesystem contract for a GravityClaw installation."""

    config_dir: Path
    data_dir: Path
    state_dir: Path
    runtime_dir: Path
    install_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "gravityclaw.toml"

    @property
    def identity_dir(self) -> Path:
        return self.config_dir / "identity"

    @property
    def capability_dir(self) -> Path:
        return self.config_dir / "capabilities"

    @property
    def secret_dir(self) -> Path:
        return self.runtime_dir / "secrets"

    @property
    def database(self) -> Path:
        return self.data_dir / "gravityclaw.db"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def release_dir(self) -> Path:
        return self.install_dir / "releases"

    @property
    def current_link(self) -> Path:
        return self.install_dir / "current"

    @classmethod
    def for_user(cls, home: Path | None = None) -> "RuntimeLayout":
        """Resolve XDG locations, with ``home`` useful for isolated tests."""
        if home is not None:
            root = home.expanduser().resolve()
            return cls(root / "config", root / "data", root / "state", root / "runtime", root / "install")
        config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "gravityclaw"
        data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "gravityclaw"
        state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "gravityclaw"
        runtime_root = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        install = Path(os.environ.get("GRAVITYCLAW_INSTALL_DIR", Path.home() / ".local" / "lib" / "gravityclaw"))
        return cls(config.resolve(), data.resolve(), state.resolve(), (runtime_root / "gravityclaw").resolve(), install.resolve())

    def create(self) -> None:
        for path in (
            self.config_dir, self.data_dir, self.state_dir, self.runtime_dir,
            self.identity_dir, self.capability_dir, self.secret_dir,
            self.data_dir / "memory", self.data_dir / "workspaces",
            self.data_dir / "artifacts", self.backup_dir, self.log_dir,
            self.release_dir,
        ):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                path.chmod(0o700)
            except OSError:
                pass


DEFAULT_CONFIG = """# GravityClaw production configuration. Secret values belong in token_file paths.
config_version = 1

[server]
host = "127.0.0.1"
port = 8787

[execution]
target = "host"
sandbox = false
mode = "agy"
worker_image = "localhost/gravityclaw-agy:1.1.13"
worker_image_digest = ""
agy_binary = "agy"

[execution.policy]
mode = "balanced"
allow_normal_commands = true
require_approval_for_elevated = true

[database]
path = "{database}"

[control]
token_file = "{control_token}"
cookie_secure = true

[telegram]
enabled = false
token_file = "{telegram_token}"
allowed_user_id = ""
default_workspace = ""

[scheduler]
enabled = true
poll_interval = 1.0

[backup]
directory = "{backup_dir}"
"""


def default_config_text(layout: RuntimeLayout) -> str:
    return DEFAULT_CONFIG.format(
        database=layout.database,
        control_token=layout.secret_dir / "gravityclaw-control-token",
        telegram_token=layout.secret_dir / "telegram-bot-token",
        backup_dir=layout.backup_dir,
    )


def write_default_config(layout: RuntimeLayout, *, overwrite: bool = False) -> Path:
    layout.create()
    if layout.config_file.exists() and not overwrite:
        return layout.config_file
    _atomic_write(layout.config_file, default_config_text(layout), mode=0o600)
    return layout.config_file


def load_config(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ConfigurationError(f"configuration does not exist: {path}")
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"invalid TOML in {path}: {exc}") from exc
    validate_config(value, path=path)
    return value


def validate_config(value: dict[str, Any], *, path: Path | None = None) -> None:
    if not isinstance(value, dict):
        raise ConfigurationError("configuration must be a TOML table")
    version = value.get("config_version", CONFIG_VERSION)
    if version != CONFIG_VERSION:
        raise ConfigurationError(f"unsupported config_version {version!r}")
    server = value.get("server", {})
    if not isinstance(server, dict) or not isinstance(server.get("port", 8787), int):
        raise ConfigurationError("server.port must be an integer")
    host = server.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host or any(char in host for char in "\r\n"):
        raise ConfigurationError("server.host must be a non-empty single-line string")
    port = int(server.get("port", 8787))
    if not 1 <= port <= 65535:
        raise ConfigurationError("server.port must be between 1 and 65535")
    execution = value.get("execution", {})
    if not isinstance(execution, dict):
        raise ConfigurationError("[execution] must be a table")
    mode = execution.get("mode", "agy")
    if mode not in {"agy", "fake"}:
        raise ConfigurationError("execution.mode must be 'agy' or 'fake'")
    target = execution.get("target", "host")
    if target not in {"host", "container", "podman"}:
        raise ConfigurationError("execution.target must be 'host' or 'container'")
    policy = execution.get("policy", {})
    if policy and not isinstance(policy, dict):
        raise ConfigurationError("[execution.policy] must be a table")
    policy_mode = policy.get("mode", "balanced") if isinstance(policy, dict) else "balanced"
    if policy_mode not in {"balanced", "full", "restricted"}:
        raise ConfigurationError("execution.policy.mode must be 'balanced', 'full', or 'restricted'")
    for section_name in ("control", "telegram"):
        section = value.get(section_name, {})
        if section and not isinstance(section, dict):
            raise ConfigurationError(f"[{section_name}] must be a table")
        token_file = section.get("token_file") if isinstance(section, dict) else None
        if token_file is not None and not isinstance(token_file, str):
            raise ConfigurationError(f"{section_name}.token_file must be a path")
    if path is not None and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigurationError(f"configuration must not be group/world accessible: {path}")


def read_secret_file(path: Path) -> str:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ConfigurationError(f"secret file does not exist: {path}")
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigurationError(f"secret file is too permissive: {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ConfigurationError(f"secret file is empty: {path}")
    return value


def ensure_control_token(path: Path) -> str:
    """Create a random control credential once; never prints its value."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        return read_secret_file(path)
    _atomic_write(path, secrets.token_urlsafe(48) + "\n", mode=0o600)
    return read_secret_file(path)


def _atomic_write(path: Path, content: str, *, mode: int) -> None:
    import os as _os
    import tempfile
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        _os.fchmod(descriptor, mode)
        with _os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            _os.fsync(handle.fileno())
        _os.replace(temporary, path)
        path.chmod(mode)
    finally:
        if temporary.exists():
            temporary.unlink()
