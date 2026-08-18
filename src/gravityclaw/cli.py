"""User-facing installation, diagnostics, service, and recovery CLI."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import (
    ConfigurationError,
    RELEASE_VERSION,
    RuntimeLayout,
    ensure_control_token,
    load_config,
    write_default_config,
)
from .identity import IdentityStore
from .ops import (
    backup_layout,
    database_health,
    restore_layout,
    verify_backup,
)
from .release import activate_candidate, release_manifest, rollback, write_release_manifest
from .server import run_gateway
from .store import Store


def _layout(args: argparse.Namespace) -> RuntimeLayout:
    return RuntimeLayout.for_user(Path(args.root).expanduser() if args.root else None)


def _config_path(args: argparse.Namespace, layout: RuntimeLayout) -> Path:
    return Path(args.config).expanduser().resolve() if args.config else layout.config_file


def setup(args: argparse.Namespace) -> int:
    layout = _layout(args)
    layout.create()
    config_path = _config_path(args, layout)
    if config_path != layout.config_file and not config_path.exists():
        config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        config_path.write_text(
            write_default_config(layout).read_text(encoding="utf-8"), encoding="utf-8"
        )
        config_path.chmod(0o600)
    else:
        write_default_config(layout)
    # Generate only the control credential. AGY and Telegram authentication are
    # deliberately left to their official/user-managed flows.
    ensure_control_token(layout.secret_dir / "gravityclaw-control-token")
    Store(layout.database).initialize()
    IdentityStore(layout.identity_dir, runtime_home=layout.data_dir).bootstrap()
    _write_service_unit(layout, config_path, load_config(config_path))
    _enable_service_unit()
    print(json.dumps({
        "version": RELEASE_VERSION,
        "config": str(config_path),
        "data": str(layout.data_dir),
        "identity": str(layout.identity_dir),
        "service_unit": str(_service_unit_path()),
        "agy_authentication": "user action required",
    }, indent=2, sort_keys=True))
    return 0


def doctor(args: argparse.Namespace) -> int:
    layout = _layout(args)
    config_path = _config_path(args, layout)
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    try:
        config = load_config(config_path)
        add("configuration", "ok", str(config_path))
    except (ConfigurationError, OSError) as exc:
        add("configuration", "error", str(exc))
        config = {}

    for name, path in (
        ("config directory", layout.config_dir),
        ("data directory", layout.data_dir),
        ("state directory", layout.state_dir),
        ("runtime directory", layout.runtime_dir),
        ("secret directory", layout.secret_dir),
    ):
        if path.is_dir() and not (path.stat().st_mode & 0o077):
            add(name, "ok", str(path))
        elif path.exists():
            add(name, "error", f"permissions too broad: {path}")
        else:
            add(name, "error", f"missing: {path}")

    if layout.database.is_file():
        try:
            health = database_health(layout.database)
            if health["integrity_check"] == "ok" and not health["foreign_key_errors"] and health["journal_mode"] == "wal":
                add("sqlite", "ok", f"schema {health['schema_version']}, WAL")
            else:
                add("sqlite", "error", json.dumps(health, sort_keys=True))
        except Exception as exc:  # doctor must report, not crash
            add("sqlite", "error", str(exc))
    else:
        add("sqlite", "error", f"missing: {layout.database}")

    podman = shutil.which("podman")
    if not podman:
        add("rootless podman", "error" if config.get("execution", {}).get("mode", "agy") == "agy" else "warn", "podman not found")
    else:
        try:
            result = subprocess.run([podman, "info", "--format", "{{.Host.Security.Rootless}}"], capture_output=True, text=True, timeout=15, check=False)
            rootless = result.stdout.strip().lower() == "true"
            add("rootless podman", "ok" if result.returncode == 0 and rootless else "error", result.stdout.strip() or result.stderr.strip())
        except (OSError, subprocess.TimeoutExpired) as exc:
            add("rootless podman", "error", str(exc))

    execution = config.get("execution", {}) if isinstance(config, dict) else {}
    agy_binary = str(execution.get("agy_binary", "agy"))
    agy_path = shutil.which(agy_binary) or (agy_binary if Path(agy_binary).is_file() else None)
    mode = execution.get("mode", "agy")
    add("agy installed", "ok" if agy_path else ("error" if mode == "agy" else "warn"), agy_path or "not found; authenticate/install official AGY")
    if agy_path and args.probe_agy:
        try:
            probe = subprocess.run(
                [agy_path, "-p", "Reply exactly AGY_DOCTOR_OK. Do not use tools.",
                 "--output-format", "stream-json", "--print-timeout", "30s"],
                capture_output=True, text=True, timeout=45, check=False,
            )
            authenticated = probe.returncode == 0 and "AGY_DOCTOR_OK" in probe.stdout
            add("agy authentication", "ok" if authenticated else "error", "official headless probe passed" if authenticated else "official headless probe failed")
        except (OSError, subprocess.TimeoutExpired) as exc:
            add("agy authentication", "error", str(exc))
    elif agy_path:
        add("agy authentication", "warn", "not probed; run doctor --probe-agy after the official AGY login")
    image = execution.get("worker_image")
    if podman and image:
        result = subprocess.run([podman, "image", "inspect", str(image)], capture_output=True, text=True, timeout=15, check=False)
        add("worker image", "ok" if result.returncode == 0 else "error", str(image) if result.returncode == 0 else result.stderr.strip())
    elif image:
        add("worker image", "warn", "not checked because podman is unavailable")

    for label, section_name, required in (("control token", "control", True), ("telegram token", "telegram", bool(config.get("telegram", {}).get("enabled")))):
        token_file = config.get(section_name, {}).get("token_file")
        if not token_file:
            add(label, "error" if required else "ok", "disabled" if not required else "missing token_file")
        elif Path(token_file).is_file() and not (Path(token_file).stat().st_mode & 0o077):
            add(label, "ok", "present; value withheld")
        else:
            add(label, "error" if required else "warn", "missing or permissions too broad")

    errors = sum(item["status"] == "error" for item in checks)
    result = {"version": RELEASE_VERSION, "healthy": errors == 0 and not any(item["status"] == "warn" for item in checks), "checks": checks}
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"GravityClaw {RELEASE_VERSION}")
        for item in checks:
            print(f"{'✓' if item['status'] == 'ok' else '⚠' if item['status'] == 'warn' else '✕'} {item['name']}: {item['detail']}")
    return 0 if errors == 0 else 1


def config_validate(args: argparse.Namespace) -> int:
    path = _config_path(args, _layout(args))
    value = load_config(path)
    print(json.dumps({"valid": True, "path": str(path), "sections": sorted(value)}, indent=2))
    return 0


def _service_action(action: str) -> int:
    if action in {"start", "stop", "restart"}:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    command = ["systemctl", "--user", action, "gravityclaw.service"]
    if action == "logs":
        command = ["journalctl", "--user", "-u", "gravityclaw.service", "-n", "100", "--no-pager"]
    result = subprocess.run(command, check=False)
    return result.returncode


def service(args: argparse.Namespace) -> int:
    return _service_action(args.service_action)


def gateway(args: argparse.Namespace) -> int:
    if args.config:
        os.environ["GRAVITYCLAW_CONFIG"] = str(Path(args.config).expanduser().resolve())
    run_gateway(host=args.host, port=args.port, log_level=args.log_level)
    return 0


def backup(args: argparse.Namespace) -> int:
    layout = _layout(args)
    if args.backup_action == "create":
        output = Path(args.output).expanduser().resolve() if args.output else layout.backup_dir / f"gravityclaw-{RELEASE_VERSION}.tar.gz"
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = backup_layout(layout, output)
        print(json.dumps({"backup": str(path)}, indent=2))
    elif args.backup_action == "verify":
        print(json.dumps(verify_backup(Path(args.archive)), indent=2, sort_keys=True))
    elif args.backup_action == "restore":
        target = Path(args.target).expanduser().resolve()
        print(json.dumps({"restored": str(restore_layout(Path(args.archive), target))}, indent=2))
    else:
        for path in sorted(layout.backup_dir.glob("*.tar.gz")):
            print(path)
    return 0


def release(args: argparse.Namespace) -> int:
    layout = _layout(args)
    config = load_config(_config_path(args, layout))
    if args.release_action == "manifest":
        path = write_release_manifest(layout, release_manifest(layout, source=Path(args.source).resolve() if args.source else None, config=config))
        print(json.dumps({"manifest": str(path), "release": json.loads(path.read_text(encoding="utf-8"))}, indent=2, sort_keys=True))
        return 0
    if args.release_action == "upgrade":
        candidate = Path(args.candidate).resolve()
        backup_path = None
        if layout.database.is_file():
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            backup_path = backup_layout(layout, layout.backup_dir / f"pre-upgrade-{stamp}.tar.gz")
        destination = activate_candidate(layout, candidate, args.version or RELEASE_VERSION)
        print(json.dumps({"activated": str(destination), "backup": str(backup_path) if backup_path else None, "restart_required": True}, indent=2))
        return 0
    target = rollback(layout)
    print(json.dumps({"rolled_back_to": str(target), "restart_required": True}, indent=2))
    return 0


def worker(args: argparse.Namespace) -> int:
    if shutil.which("podman") is None:
        print("podman is required to build the worker image", file=sys.stderr)
        return 1
    source = Path(args.source).expanduser().resolve()
    containerfile = source / "worker" / "Containerfile.agy"
    if not containerfile.is_file():
        print(f"worker Containerfile not found: {containerfile}", file=sys.stderr)
        return 1
    result = subprocess.run(["podman", "build", "--file", str(containerfile), "--tag", args.image, str(source)], check=False)
    return result.returncode


def _service_unit_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user" / "gravityclaw.service"


def _enable_service_unit() -> None:
    """Make the generated user unit survive the next user-manager reload."""
    if shutil.which("systemctl") is None:
        return
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "gravityclaw.service"], check=False)


def _write_service_unit(layout: RuntimeLayout, config_path: Path, config: dict[str, Any]) -> None:
    target = _service_unit_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    python = sys.executable
    server = config.get("server", {})
    host = server.get("host", "127.0.0.1")
    port = int(server.get("port", 8787))
    content = f"""[Unit]
Description=GravityClaw persistent personal agent
After=default.target

[Service]
Type=simple
ExecStart={python} -m gravityclaw.server --host {host} --port {port} --log-level info
Environment=GRAVITYCLAW_CONFIG={config_path}
Environment=GRAVITYCLAW_COOKIE_SECURE=1
WorkingDirectory={layout.data_dir}
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
UMask=0077

[Install]
WantedBy=default.target
"""
    target.write_text(content, encoding="utf-8")
    target.chmod(0o600)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gravityclaw", description="Install and operate GravityClaw")
    parser.add_argument("--root", help="isolated layout root (testing and migrations)")
    parser.add_argument("--config", help="TOML configuration path")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("setup", "install"):
        setup_parser = sub.add_parser(name, help="create the canonical layout and service unit")
        setup_parser.add_argument("--non-interactive", action="store_true", help="never prompt; use safe defaults")
    doc = sub.add_parser("doctor", help="validate installation health")
    doc.add_argument("--json", action="store_true")
    doc.add_argument("--probe-agy", action="store_true", help="run a small official headless AGY authentication probe")
    conf = sub.add_parser("config", help="configuration operations")
    conf_sub = conf.add_subparsers(dest="config_action", required=True)
    conf_sub.add_parser("validate")
    svc = sub.add_parser("service", help="manage the user service")
    svc.add_argument("service_action", choices=("start", "stop", "restart", "status", "logs"))
    for action in ("start", "stop", "restart", "status", "logs"):
        sub.add_parser(action, help=f"{action} the user service")
    gateway_parser = sub.add_parser("gateway", help="run the combined API and production Web gateway")
    gateway_parser.add_argument("--host", default="127.0.0.1")
    gateway_parser.add_argument("--port", type=int, default=8787)
    gateway_parser.add_argument("--log-level", default="info")
    gateway_parser.add_argument("--dev", action="store_true", help="run the backend for a separate Vite development server")
    bkp = sub.add_parser("backup", help="backup and restore state")
    bkp_sub = bkp.add_subparsers(dest="backup_action", required=True)
    create = bkp_sub.add_parser("create")
    create.add_argument("--output")
    bkp_sub.add_parser("list")
    verify = bkp_sub.add_parser("verify")
    verify.add_argument("archive")
    restore = bkp_sub.add_parser("restore")
    restore.add_argument("archive")
    restore.add_argument("target")
    rel = sub.add_parser("release", help="release provenance and switching")
    rel_sub = rel.add_subparsers(dest="release_action", required=True)
    manifest = rel_sub.add_parser("manifest")
    manifest.add_argument("--source")
    upgrade = rel_sub.add_parser("upgrade")
    upgrade.add_argument("--candidate", required=True)
    upgrade.add_argument("--version")
    rel_sub.add_parser("rollback")
    wrk = sub.add_parser("worker", help="build the pinned local worker image")
    wrk_sub = wrk.add_subparsers(dest="worker_action", required=True)
    build = wrk_sub.add_parser("build")
    build.add_argument("--source", default=".")
    build.add_argument("--image", default="localhost/gravityclaw-agy:1.1.13")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"setup", "install"}:
        return setup(args)
    if args.command == "doctor":
        return doctor(args)
    if args.command == "config":
        return config_validate(args)
    if args.command == "service":
        return service(args)
    if args.command in {"start", "stop", "restart", "status", "logs"}:
        return _service_action(args.command)
    if args.command == "gateway":
        return gateway(args)
    if args.command == "backup":
        return backup(args)
    if args.command == "release":
        return release(args)
    if args.command == "worker" and args.worker_action == "build":
        return worker(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
