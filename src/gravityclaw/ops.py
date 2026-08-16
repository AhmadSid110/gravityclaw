"""Safe operational helpers for backup, restore, and SQLite health checks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


class OperationsError(ValueError):
    """Raised when an operational action would be unsafe or invalid."""


def database_health(database: Path) -> dict[str, Any]:
    """Return integrity and WAL facts without mutating application state."""
    database = database.resolve()
    if not database.is_file():
        raise OperationsError(f"database does not exist: {database}")
    connection = sqlite3.connect(database)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = [row[0] for row in connection.execute("PRAGMA foreign_key_check").fetchall()]
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        schema = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        return {
            "path": str(database),
            "integrity_check": integrity,
            "foreign_key_errors": len(foreign_keys),
            "journal_mode": journal_mode,
            "schema_version": schema[0] if schema else None,
            "size_bytes": database.stat().st_size,
        }
    finally:
        connection.close()


def checkpoint_database(database: Path) -> dict[str, Any]:
    """Checkpoint WAL pages without forcing a vacuum or blocking writers."""
    database = database.resolve()
    if not database.is_file():
        raise OperationsError(f"database does not exist: {database}")
    connection = sqlite3.connect(database, timeout=10)
    try:
        result = connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        connection.commit()
        health = database_health(database)
        health["wal_checkpoint"] = list(result or ())
        return health
    finally:
        connection.close()


def backup_home(home: Path, destination: Path) -> Path:
    """Create a consistent compressed backup outside the live home directory."""
    home = home.resolve()
    destination = destination.resolve()
    if not home.is_dir():
        raise OperationsError(f"GravityClaw home does not exist: {home}")
    if destination == home or destination.is_relative_to(home):
        raise OperationsError("backup destination must be outside GravityClaw home")
    if destination.exists():
        raise OperationsError(f"backup already exists: {destination}")
    database = home / "gravityclaw.db"
    if not database.is_file():
        raise OperationsError("GravityClaw home has no database")
    _reject_links_and_special_files(home)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gravityclaw-backup-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "home"
        staging.mkdir(mode=0o700)
        _sqlite_backup(database, staging / "gravityclaw.db")
        for child in home.iterdir():
            if child.name in {"gravityclaw.db", "gravityclaw.db-wal", "gravityclaw.db-shm"}:
                continue
            target = staging / child.name
            if child.is_dir():
                shutil.copytree(child, target, symlinks=False)
            else:
                shutil.copy2(child, target)
        (staging / "backup.json").write_text(
            json.dumps({
                "format": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "database": database.name,
            }, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with tarfile.open(destination, "x:gz") as archive:
            archive.add(staging, arcname=".", recursive=True)
    verify_backup(destination)
    return destination


def verify_backup(archive_path: Path) -> dict[str, Any]:
    """Validate archive paths and the SQLite snapshot without extracting live data."""
    archive_path = archive_path.resolve()
    if not archive_path.is_file():
        raise OperationsError(f"backup does not exist: {archive_path}")
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            _validate_member(member)
        database_member = next(
            (member for member in members if _member_relative_name(member.name) == "gravityclaw.db"),
            None,
        )
        if database_member is None:
            raise OperationsError("backup has no gravityclaw.db")
        with tempfile.TemporaryDirectory(prefix="gravityclaw-verify-") as temporary:
            target = Path(temporary) / "gravityclaw.db"
            source = archive.extractfile(database_member)
            if source is None:
                raise OperationsError("backup database cannot be read")
            with target.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            health = database_health(target)
    return {"archive": str(archive_path), "members": len(members), "database": health}


def restore_backup(archive_path: Path, target_home: Path) -> Path:
    """Restore into a new home directory; never overwrite an existing home."""
    archive_path = archive_path.resolve()
    target_home = target_home.resolve()
    if target_home.exists():
        raise OperationsError("restore target already exists; choose a new directory")
    verify_backup(archive_path)
    target_home.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="gravityclaw-restore-", dir=target_home.parent))
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                _validate_member(member)
            archive.extractall(temporary)
        restored = temporary / "gravityclaw.db"
        if not restored.is_file():
            raise OperationsError("restored backup has no database")
        health = database_health(restored)
        if health["integrity_check"] != "ok" or health["foreign_key_errors"]:
            raise OperationsError("restored database failed integrity checks")
        os.replace(temporary, target_home)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target_home


def _sqlite_backup(source_path: Path, destination: Path) -> None:
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()
    os.chmod(destination, 0o600)


def _reject_links_and_special_files(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        mode = path.lstat().st_mode
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise OperationsError(f"backup refuses link or special file: {path}")


def _member_relative_name(name: str) -> str:
    value = PurePosixPath(name)
    while value.parts and value.parts[0] == ".":
        value = PurePosixPath(*value.parts[1:])
    return str(value)


def _validate_member(member: tarfile.TarInfo) -> None:
    value = PurePosixPath(member.name)
    if value.is_absolute() or ".." in value.parts:
        raise OperationsError(f"backup contains unsafe path: {member.name}")
    if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
        raise OperationsError(f"backup contains unsupported member: {member.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="GravityClaw operational maintenance")
    subparsers = parser.add_subparsers(dest="action", required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("--home", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--archive", type=Path, required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--target", type=Path, required=True)
    health = subparsers.add_parser("health")
    health.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "backup":
        result = {"backup": str(backup_home(args.home, args.output))}
    elif args.action == "verify":
        result = verify_backup(args.archive)
    elif args.action == "restore":
        result = {"restored": str(restore_backup(args.archive, args.target))}
    else:
        result = database_health(args.database)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
