"""Release provenance and atomic local release switching."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .config import RELEASE_VERSION, RuntimeLayout
from .store import SCHEMA_VERSION


def release_manifest(layout: RuntimeLayout, *, source: Path | None = None, config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    execution = config.get("execution", {})
    image = execution.get("worker_image")
    digest = execution.get("worker_image_digest") or _podman_image_digest(image)
    agy_binary = str(execution.get("agy_binary", "agy"))
    return {
        "format": 1,
        "gravityclaw_version": RELEASE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "worker_image": image,
        "worker_image_digest": digest,
        "agy_binary": agy_binary,
        "agy_version": _command_version(agy_binary),
        "frontend_sha256": _file_digest((source or Path.cwd()) / "web" / "dist" / "index.html"),
    }


def write_release_manifest(layout: RuntimeLayout, manifest: dict[str, Any]) -> Path:
    target = layout.config_dir / "release-manifest.json"
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(target)
    return target


def activate_candidate(layout: RuntimeLayout, candidate: Path, version: str = RELEASE_VERSION) -> Path:
    candidate = candidate.expanduser().resolve()
    if not candidate.is_dir():
        raise ValueError(f"candidate release is not a directory: {candidate}")
    destination = layout.release_dir / version
    if destination.exists():
        raise ValueError(f"release already exists: {destination}")
    temporary = Path(tempfile.mkdtemp(prefix=f".{version}-", dir=layout.release_dir))
    try:
        shutil.copytree(candidate, temporary / "payload", symlinks=False, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git", ".venv", "node_modules", "__pycache__", "*.pyc"))
        (temporary / "manifest.json").write_text(json.dumps({"version": version}, sort_keys=True) + "\n", encoding="utf-8")
        temporary.rename(destination)
        _switch_link(layout, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def rollback(layout: RuntimeLayout) -> Path:
    previous = layout.install_dir / "previous"
    if not previous.is_symlink():
        raise ValueError("no previous release is available")
    target = previous.resolve()
    _switch_link(layout, target)
    return target


def _switch_link(layout: RuntimeLayout, target: Path) -> None:
    layout.install_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = layout.current_link
    if current.is_symlink():
        old = current.resolve()
        temporary_previous = layout.install_dir / ".previous.tmp"
        temporary_previous.unlink(missing_ok=True)
        temporary_previous.symlink_to(old)
        temporary_previous.replace(layout.install_dir / "previous")
    temporary = layout.install_dir / ".current.tmp"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    temporary.replace(current)


def _podman_image_digest(image: Any) -> str | None:
    if not image or not shutil.which("podman"):
        return None
    try:
        result = subprocess.run(["podman", "image", "inspect", str(image), "--format", "{{.Id}}"], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _command_version(command: str) -> str | None:
    try:
        result = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout.strip() or result.stderr.strip())[:200] or None


def _file_digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
