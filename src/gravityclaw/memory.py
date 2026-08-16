"""Episodic Markdown memory plus searchable SQLite indexing."""

from __future__ import annotations

import fcntl
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

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
