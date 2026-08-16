"""Episodic Markdown memory plus searchable SQLite indexing."""

from __future__ import annotations

import fcntl
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from hashlib import sha256

from .store import Store


class MemoryService:
    def __init__(self, home: Path, store: Store) -> None:
        self.home = home
        self.store = store

    def record_episode(
        self,
        content: str,
        *,
        source: str,
        conversation_id: str | None = None,
        confidence: float = 1.0,
        now: datetime | None = None,
    ) -> str:
        text = content.strip()
        if not text:
            raise ValueError("memory content must not be empty")
        if conversation_id is not None:
            self.store.get_conversation(conversation_id)
        timestamp = now or datetime.now(UTC)
        memory_directory = self.home / "memory"
        memory_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        daily_path = memory_directory / f"{timestamp.date().isoformat()}.md"
        memory_id = str(uuid.uuid4())
        # SQLite is canonical.  The Markdown log is a human-readable journal
        # carrying the same stable id so a repair tool can detect omissions
        # without guessing from prose.
        self.store.add_memory(
            text,
            memory_id=memory_id,
            kind="episodic",
            source=source,
            source_conversation_id=conversation_id,
            confidence=confidence,
            created_at=timestamp.isoformat(),
        )
        safe_source = re.sub(r"[\r\n\]]+", " ", source).strip() or "unknown"
        safe_text = text.replace("\r", " ").replace("\n", " ")
        entry = (
            f"\n- {timestamp.isoformat()} [{safe_source}] "
            f"<!-- memory:{memory_id} --> {safe_text}\n"
        )
        descriptor = os.open(daily_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            payload = memoryview(entry.encode("utf-8"))
            while payload:
                written = os.write(descriptor, payload)
                if written <= 0:
                    raise OSError("episodic memory journal write made no progress")
                payload = payload[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return memory_id

    def retrieve(self, query: str, limit: int = 8) -> list[dict[str, object]]:
        return self.store.search_memories(query, limit=limit)

    def list_journals(self) -> list[dict[str, object]]:
        directory = self.home / "memory"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        journals: list[dict[str, object]] = []
        for path in sorted(directory.glob("*.md"), reverse=True):
            data = path.read_bytes()
            journals.append({
                "date": path.stem,
                "name": path.name,
                "characters": len(data.decode("utf-8")),
                "sha256": sha256(data).hexdigest(),
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
            })
        return journals

    def read_journal(self, date: str) -> dict[str, object]:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError("journal date must be YYYY-MM-DD")
        path = self.home / "memory" / f"{date}.md"
        if not path.exists():
            raise KeyError(f"journal not found: {date}")
        data = path.read_bytes()
        return {"date": date, "content": data.decode("utf-8"), "sha256": sha256(data).hexdigest()}

    def update_journal(self, date: str, content: str, expected_sha256: str | None = None) -> dict[str, object]:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError("journal date must be YYYY-MM-DD")
        path = self.home / "memory" / f"{date}.md"
        current = path.read_bytes() if path.exists() else b""
        current_hash = sha256(current).hexdigest()
        if expected_sha256 is not None and expected_sha256 != current_hash:
            raise ValueError(f"journal {date} changed; reload before saving")
        _atomic_write(path, content)
        return self.read_journal(date)


def _atomic_write(path: Path, content: str) -> None:
    import tempfile
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
