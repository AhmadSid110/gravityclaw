"""Transactional SQLite state for the reliable GravityClaw core.

GravityClaw stores backend conversation identifiers, never backend credentials.
Every run event has a monotonically increasing per-run sequence. Lifecycle
transitions and their corresponding events commit in the same transaction.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .events import AgentEvent


SCHEMA_VERSION = 4
RUN_STATUSES = (
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    "orphaned",
)
TERMINAL_RUN_STATUSES = (
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    "orphaned",
)
WORKER_STATES = ("running", "exited", "missing", "orphaned", "terminated")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class Workspace:
    id: str
    name: str
    path: Path
    created_at: str


@dataclass(frozen=True, slots=True)
class Conversation:
    id: str
    workspace_id: str
    channel: str
    channel_key: str | None
    title: str | None
    agy_conversation_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    conversation_id: str
    role: str
    content: str
    created_at: str
    source_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: str
    conversation_id: str
    status: str
    backend: str
    backend_conversation_id: str | None
    worker_id: str | None
    request: dict[str, Any]
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    version: int


@dataclass(frozen=True, slots=True)
class PersistedEvent:
    id: int
    run_id: str
    sequence: int
    source_sequence: int | None
    event_type: str
    conversation_id: str | None
    payload: dict[str, Any]
    raw: dict[str, Any] | None
    created_at: str


@dataclass(frozen=True, slots=True)
class WorkerRecord:
    external_id: str
    run_id: str | None
    workspace_id: str | None
    backend: str
    state: str
    metadata: dict[str, Any]
    created_at: str
    updated_at: str
    last_seen_at: str


class Store:
    """Small synchronous repository with one connection per transaction."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                connection.executescript(_SCHEMA)
                connection.executescript(_CHANNEL_SCHEMA)
                self._ensure_message_index(connection)
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                return
            version = int(row["value"])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported GravityClaw schema {version}; expected at most {SCHEMA_VERSION}"
                )
            if version < 1:
                raise RuntimeError(f"unsupported GravityClaw schema {version}")
            if version == 1:
                connection.executescript(_MEMORY_SCHEMA)
                self._set_schema_version(connection, 2)
                version = 2
            if version == 2:
                columns = {
                    str(item["name"])
                    for item in connection.execute("PRAGMA table_info(messages)").fetchall()
                }
                if "source_run_id" not in columns:
                    connection.execute(
                        "ALTER TABLE messages ADD COLUMN source_run_id TEXT "
                        "REFERENCES runs(id) ON DELETE SET NULL"
                    )
                self._set_schema_version(connection, 3)
                version = 3
            if version == 3:
                connection.executescript(_CHANNEL_SCHEMA)
                self._set_schema_version(connection, 4)
            connection.executescript(_SCHEMA)
            connection.executescript(_CHANNEL_SCHEMA)
            self._ensure_message_index(connection)

    @staticmethod
    def _set_schema_version(connection: sqlite3.Connection, version: int) -> None:
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(version),),
        )

    @staticmethod
    def _ensure_message_index(connection: sqlite3.Connection) -> None:
        connection.execute("DROP INDEX IF EXISTS messages_run_role")
        connection.execute(
            "CREATE UNIQUE INDEX messages_run_role ON messages(source_run_id, role) "
            "WHERE source_run_id IS NOT NULL AND role IN ('user', 'assistant')"
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create_workspace(self, name: str, path: Path) -> Workspace:
        resolved = path.resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        now = utc_now()
        record = Workspace(str(uuid.uuid4()), name, resolved, now)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO workspaces(id, name, path, created_at) VALUES(?, ?, ?, ?)",
                (record.id, record.name, str(record.path), record.created_at),
            )
        return record

    def get_workspace(self, workspace_id: str) -> Workspace:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workspaces WHERE id=?", (workspace_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"workspace not found: {workspace_id}")
        return Workspace(row["id"], row["name"], Path(row["path"]), row["created_at"])

    def list_workspaces(self) -> list[Workspace]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM workspaces ORDER BY created_at").fetchall()
        return [
            Workspace(row["id"], row["name"], Path(row["path"]), row["created_at"])
            for row in rows
        ]

    def create_conversation(
        self,
        workspace_id: str,
        *,
        channel: str = "web",
        channel_key: str | None = None,
        title: str | None = None,
    ) -> Conversation:
        now = utc_now()
        record = Conversation(
            str(uuid.uuid4()), workspace_id, channel, channel_key, title, None, now, now
        )
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO conversations(
                    id, workspace_id, channel, channel_key, title, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.id,
                    workspace_id,
                    channel,
                    channel_key,
                    title,
                    now,
                    now,
                ),
            )
        return record

    def get_conversation(self, conversation_id: str) -> Conversation:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"conversation not found: {conversation_id}")
        return _conversation(row)

    def bind_backend_conversation(self, conversation_id: str, backend_id: str) -> None:
        if not backend_id:
            return
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE conversations
                   SET agy_conversation_id=?, updated_at=?
                   WHERE id=? AND (agy_conversation_id IS NULL OR agy_conversation_id=?)""",
                (backend_id, now, conversation_id, backend_id),
            )
            if cursor.rowcount != 1:
                current = connection.execute(
                    "SELECT agy_conversation_id FROM conversations WHERE id=?",
                    (conversation_id,),
                ).fetchone()
                if current is None:
                    raise KeyError(f"conversation not found: {conversation_id}")
                raise ValueError(
                    "refusing to replace an existing AGY conversation binding "
                    f"({current['agy_conversation_id']} != {backend_id})"
                )

    def append_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        *,
        source_run_id: str | None = None,
    ) -> Message:
        if role not in {"user", "assistant", "system", "tool"}:
            raise ValueError(f"invalid message role: {role}")
        if not content.strip():
            raise ValueError("message content must not be empty")
        record = Message(
            str(uuid.uuid4()), conversation_id, role, content, utc_now(), source_run_id
        )
        with self._connect() as connection:
            if source_run_id is not None and role in {"user", "assistant"}:
                existing = connection.execute(
                    "SELECT * FROM messages WHERE source_run_id=? AND role=?",
                    (source_run_id, role),
                ).fetchone()
                if existing is not None:
                    return _message(existing)
            connection.execute(
                "INSERT INTO messages("
                "id, conversation_id, role, content, created_at, source_run_id"
                ") VALUES(?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    record.conversation_id,
                    record.role,
                    record.content,
                    record.created_at,
                    record.source_run_id,
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?",
                (record.created_at, conversation_id),
            )
        return record

    def recent_messages(
        self,
        conversation_id: str,
        limit: int = 20,
        *,
        exclude_run_id: str | None = None,
    ) -> list[Message]:
        exclusion = "AND (source_run_id IS NULL OR source_run_id<>?)" if exclude_run_id else ""
        parameters: list[Any] = [conversation_id]
        if exclude_run_id:
            parameters.append(exclude_run_id)
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM (
                       SELECT * FROM messages WHERE conversation_id=? {exclusion}
                       ORDER BY created_at DESC, rowid DESC LIMIT ?
                   ) ORDER BY created_at ASC, rowid ASC""",
                parameters,
            ).fetchall()
        return [_message(row) for row in rows]

    def submit_run(
        self,
        conversation_id: str,
        request: Mapping[str, Any],
        *,
        backend: str = "agy-container",
    ) -> RunRecord:
        """Persist the user turn, queued run, and lifecycle event atomically."""
        prompt = str(request.get("prompt", "")).strip()
        if not prompt:
            raise ValueError("prompt must not be empty")
        now = utc_now()
        run_id = str(uuid.uuid4())
        message_id = str(uuid.uuid4())
        request_json = json.dumps(dict(request), ensure_ascii=False, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO runs(
                    id, conversation_id, status, backend, request_json, created_at, version
                ) VALUES(?, ?, 'queued', ?, ?, ?, 1)""",
                (run_id, conversation_id, backend, request_json, now),
            )
            connection.execute(
                """INSERT INTO messages(
                    id, conversation_id, role, content, created_at, source_run_id
                ) VALUES(?, ?, 'user', ?, ?, ?)""",
                (message_id, conversation_id, prompt, now, run_id),
            )
            connection.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id)
            )
            self._append_event_tx(
                connection, run_id, "run.queued", None, {"status": "queued"}, None, None
            )
        return self.get_run(run_id)

    def enqueue_run(
        self,
        conversation_id: str,
        request: Mapping[str, Any],
        *,
        backend: str = "agy-container",
    ) -> RunRecord:
        now = utc_now()
        run_id = str(uuid.uuid4())
        request_json = json.dumps(dict(request), ensure_ascii=False, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO runs(
                    id, conversation_id, status, backend, request_json, created_at, version
                ) VALUES(?, ?, 'queued', ?, ?, ?, 1)""",
                (run_id, conversation_id, backend, request_json, now),
            )
            self._append_event_tx(
                connection,
                run_id,
                "run.queued",
                None,
                {"status": "queued"},
                None,
                None,
            )
        return self.get_run(run_id)

    def start_run(self, conversation_id: str, backend: str = "agy") -> RunRecord:
        """Compatibility helper used by unit tests and early core callers."""
        queued = self.enqueue_run(conversation_id, {}, backend=backend)
        claimed = self.claim_run(queued.id)
        assert claimed is not None
        return claimed

    def claim_run(self, run_id: str) -> RunRecord | None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"run not found: {run_id}")
            if row["status"] != "queued":
                return None
            occupied = connection.execute(
                "SELECT 1 FROM runs WHERE conversation_id=? AND status='running' LIMIT 1",
                (row["conversation_id"],),
            ).fetchone()
            if occupied is not None:
                return None
            cursor = connection.execute(
                """UPDATE runs SET status='running', started_at=?, version=version+1
                   WHERE id=? AND status='queued'""",
                (now, run_id),
            )
            if cursor.rowcount != 1:
                return None
            self._append_event_tx(
                connection,
                run_id,
                "run.running",
                None,
                {"status": "running"},
                None,
                None,
            )
        return self.get_run(run_id)

    def prepare_run_context(
        self,
        run_id: str,
        execution_prompt: str,
        manifest: Mapping[str, Any],
    ) -> RunRecord:
        """Persist the immutable dispatch-time context snapshot for a claimed run."""
        if not execution_prompt:
            raise ValueError("execution prompt must not be empty")
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"run not found: {run_id}")
            if row["status"] != "running" or row["worker_id"] is not None:
                raise ValueError("context can only be prepared before a running worker starts")
            request = json.loads(row["request_json"])
            existing = request.get("execution_prompt")
            if existing is not None:
                if existing != execution_prompt or request.get(
                    "context_manifest"
                ) != dict(manifest):
                    raise ValueError("refusing to replace an existing context snapshot")
                return _run(row)
            request["execution_prompt"] = execution_prompt
            request["context_manifest"] = dict(manifest)
            connection.execute(
                "UPDATE runs SET request_json=?, version=version+1 WHERE id=?",
                (json.dumps(request, ensure_ascii=False, separators=(",", ":")), run_id),
            )
            self._append_event_tx(
                connection,
                run_id,
                "run.context_compiled",
                None,
                {"manifest": dict(manifest)},
                None,
                None,
            )
        return self.get_run(run_id)

    def next_queued_runs(self) -> list[RunRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT q.* FROM runs q
                   WHERE q.status='queued'
                     AND NOT EXISTS (
                       SELECT 1 FROM runs r
                       WHERE r.conversation_id=q.conversation_id AND r.status='running'
                     )
                     AND q.created_at=(
                       SELECT MIN(q2.created_at) FROM runs q2
                       WHERE q2.conversation_id=q.conversation_id AND q2.status='queued'
                     )
                   ORDER BY q.created_at"""
            ).fetchall()
        return [_run(row) for row in rows]

    def attach_worker(
        self,
        run_id: str,
        external_id: str,
        *,
        workspace_id: str,
        backend: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        metadata_json = json.dumps(dict(metadata or {}), separators=(",", ":"))
        with self._connect() as connection:
            run = connection.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(f"run not found: {run_id}")
            if run["status"] != "running":
                raise ValueError(f"cannot attach worker to {run['status']} run")
            connection.execute(
                """INSERT INTO workers(
                    external_id, run_id, workspace_id, backend, state, metadata_json,
                    created_at, updated_at, last_seen_at
                ) VALUES(?, ?, ?, ?, 'running', ?, ?, ?, ?)
                ON CONFLICT(external_id) DO UPDATE SET
                    run_id=excluded.run_id,
                    workspace_id=excluded.workspace_id,
                    state='running',
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at,
                    last_seen_at=excluded.last_seen_at""",
                (
                    external_id,
                    run_id,
                    workspace_id,
                    backend,
                    metadata_json,
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE runs SET worker_id=?, version=version+1 WHERE id=?",
                (external_id, run_id),
            )
            self._append_event_tx(
                connection,
                run_id,
                "worker.attached",
                None,
                {"external_id": external_id, "workspace_id": workspace_id},
                None,
                None,
            )

    def record_worker(
        self,
        external_id: str,
        *,
        run_id: str | None,
        workspace_id: str | None,
        backend: str,
        state: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if state not in WORKER_STATES:
            raise ValueError(f"invalid worker state: {state}")
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO workers(
                    external_id, run_id, workspace_id, backend, state, metadata_json,
                    created_at, updated_at, last_seen_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(external_id) DO UPDATE SET
                    state=excluded.state,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at,
                    last_seen_at=excluded.last_seen_at""",
                (
                    external_id,
                    run_id,
                    workspace_id,
                    backend,
                    state,
                    json.dumps(dict(metadata or {}), separators=(",", ":")),
                    now,
                    now,
                    now,
                ),
            )

    def get_worker(self, external_id: str) -> WorkerRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workers WHERE external_id=?", (external_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"worker not found: {external_id}")
        return _worker(row)

    def list_workers(self) -> list[WorkerRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM workers ORDER BY created_at").fetchall()
        return [_worker(row) for row in rows]

    def update_worker_state(self, external_id: str, state: str) -> None:
        if state not in WORKER_STATES:
            raise ValueError(f"invalid worker state: {state}")
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE workers SET state=?, updated_at=?, last_seen_at=? WHERE external_id=?",
                (state, now, now, external_id),
            )

    def transition_run(
        self,
        run_id: str,
        status: str,
        *,
        expected: Sequence[str],
        backend_conversation_id: str | None = None,
        error: str | None = None,
        assistant_response: str | None = None,
    ) -> bool:
        if status not in RUN_STATUSES:
            raise ValueError(f"invalid run status: {status}")
        if not expected:
            raise ValueError("expected statuses must not be empty")
        now = utc_now()
        placeholders = ",".join("?" for _ in expected)
        finished_at = now if status in TERMINAL_RUN_STATUSES else None
        with self._connect() as connection:
            cursor = connection.execute(
                f"""UPDATE runs SET status=?,
                    backend_conversation_id=COALESCE(?, backend_conversation_id),
                    error=?, finished_at=?, version=version+1
                    WHERE id=? AND status IN ({placeholders})""",
                (
                    status,
                    backend_conversation_id,
                    error,
                    finished_at,
                    run_id,
                    *expected,
                ),
            )
            if cursor.rowcount != 1:
                return False
            self._append_event_tx(
                connection,
                run_id,
                f"run.{status}",
                backend_conversation_id,
                {"status": status, "error": error},
                None,
                None,
            )
            if status == "completed" and assistant_response and assistant_response.strip():
                conversation_id = connection.execute(
                    "SELECT conversation_id FROM runs WHERE id=?", (run_id,)
                ).fetchone()["conversation_id"]
                connection.execute(
                    """INSERT OR IGNORE INTO messages(
                        id, conversation_id, role, content, created_at, source_run_id
                    ) VALUES(?, ?, 'assistant', ?, ?, ?)""",
                    (
                        str(uuid.uuid4()),
                        conversation_id,
                        assistant_response.strip(),
                        now,
                        run_id,
                    ),
                )
        return True

    def finish_run(
        self,
        run_id: str,
        status: str,
        *,
        backend_conversation_id: str | None = None,
        error: str | None = None,
    ) -> None:
        normalized = "cancelled" if status == "canceled" else status
        if normalized not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"invalid terminal run status: {status}")
        if not self.transition_run(
            run_id,
            normalized,
            expected=("queued", "running"),
            backend_conversation_id=backend_conversation_id,
            error=error,
        ):
            raise ValueError(f"run is missing or already terminal: {run_id}")

    def get_run(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"run not found: {run_id}")
        return _run(row)

    def list_runs(
        self,
        *,
        conversation_id: str | None = None,
        statuses: Sequence[str] | None = None,
    ) -> list[RunRecord]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if conversation_id is not None:
            clauses.append("conversation_id=?")
            parameters.append(conversation_id)
        if statuses:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            parameters.extend(statuses)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs" + where + " ORDER BY created_at", parameters
            ).fetchall()
        return [_run(row) for row in rows]

    def recover_interrupted_runs(self) -> int:
        """Legacy safe recovery when no execution backend can reconcile workers."""
        recovered = 0
        for run in self.list_runs(statuses=("running",)):
            if self.transition_run(
                run.id,
                "interrupted",
                expected=("running",),
                error="gateway restarted while run was active",
            ):
                recovered += 1
        return recovered

    def append_event(
        self,
        run_id: str,
        event: AgentEvent,
        *,
        source_sequence: int | None = None,
    ) -> PersistedEvent:
        with self._connect() as connection:
            event_id, sequence = self._append_event_tx(
                connection,
                run_id,
                event.type,
                event.conversation_id,
                event.data,
                event.raw,
                source_sequence,
            )
            row = connection.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        assert row is not None
        return _event(row)

    def _append_event_tx(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        conversation_id: str | None,
        payload: Mapping[str, Any],
        raw: Mapping[str, Any] | None,
        source_sequence: int | None,
    ) -> tuple[int, int]:
        if source_sequence is not None:
            existing = connection.execute(
                "SELECT id, sequence FROM events WHERE run_id=? AND source_sequence=?",
                (run_id, source_sequence),
            ).fetchone()
            if existing is not None:
                return int(existing["id"]), int(existing["sequence"])
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
        cursor = connection.execute(
            """INSERT INTO events(
                run_id, sequence, source_sequence, event_type, conversation_id,
                payload_json, raw_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                sequence,
                source_sequence,
                event_type,
                conversation_id,
                json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")),
                json.dumps(dict(raw), ensure_ascii=False, separators=(",", ":"))
                if raw is not None
                else None,
                utc_now(),
            ),
        )
        return int(cursor.lastrowid), sequence

    def list_events(
        self, run_id: str, *, after_sequence: int = 0, limit: int = 1000
    ) -> list[PersistedEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM events WHERE run_id=? AND sequence>?
                   ORDER BY sequence LIMIT ?""",
                (run_id, after_sequence, limit),
            ).fetchall()
        return [_event(row) for row in rows]

    def add_memory(
        self,
        content: str,
        *,
        memory_id: str | None = None,
        kind: str = "episodic",
        source: str,
        source_conversation_id: str | None = None,
        confidence: float = 1.0,
        created_at: str | None = None,
    ) -> str:
        if kind not in {"episodic", "curated", "fact"}:
            raise ValueError(f"invalid memory kind: {kind}")
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        text = content.strip()
        if not text:
            raise ValueError("memory content must not be empty")
        if not source.strip():
            raise ValueError("memory source must not be empty")
        resolved_id = memory_id or str(uuid.uuid4())
        now = created_at or utc_now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO memories(
                    id, kind, content, source, source_conversation_id,
                    confidence, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    resolved_id,
                    kind,
                    text,
                    source,
                    source_conversation_id,
                    confidence,
                    now,
                    now,
                ),
            )
        return resolved_id

    def search_memories(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        # Compile user text into literal Unicode word tokens.  Passing arbitrary
        # input straight to FTS MATCH would expose its query language and make
        # punctuation such as ':' or '-' produce syntax errors.
        terms = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
        if not terms:
            return []
        fts_query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms[:20])
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT m.*, bm25(memories_fts) AS rank
                   FROM memories_fts
                   JOIN memories m ON m.rowid=memories_fts.rowid
                   WHERE memories_fts MATCH ?
                   ORDER BY rank, m.confidence DESC, m.updated_at DESC
                   LIMIT ?""",
                (fts_query, limit),
            ).fetchall()
        return [dict(row) for row in rows]


def _conversation(row: sqlite3.Row) -> Conversation:
    return Conversation(
        row["id"],
        row["workspace_id"],
        row["channel"],
        row["channel_key"],
        row["title"],
        row["agy_conversation_id"],
        row["created_at"],
        row["updated_at"],
    )


def _message(row: sqlite3.Row) -> Message:
    keys = set(row.keys())
    return Message(
        id=row["id"],
        conversation_id=row["conversation_id"],
        role=row["role"],
        content=row["content"],
        created_at=row["created_at"],
        source_run_id=row["source_run_id"] if "source_run_id" in keys else None,
    )


def _run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        id=row["id"],
        conversation_id=row["conversation_id"],
        status=row["status"],
        backend=row["backend"],
        backend_conversation_id=row["backend_conversation_id"],
        worker_id=row["worker_id"],
        request=json.loads(row["request_json"]),
        error=row["error"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        version=int(row["version"]),
    )


def _event(row: sqlite3.Row) -> PersistedEvent:
    return PersistedEvent(
        id=int(row["id"]),
        run_id=row["run_id"],
        sequence=int(row["sequence"]),
        source_sequence=row["source_sequence"],
        event_type=row["event_type"],
        conversation_id=row["conversation_id"],
        payload=json.loads(row["payload_json"]),
        raw=json.loads(row["raw_json"]) if row["raw_json"] else None,
        created_at=row["created_at"],
    )


def _worker(row: sqlite3.Row) -> WorkerRecord:
    return WorkerRecord(
        external_id=row["external_id"],
        run_id=row["run_id"],
        workspace_id=row["workspace_id"],
        backend=row["backend"],
        state=row["state"],
        metadata=json.loads(row["metadata_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_seen_at=row["last_seen_at"],
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    channel TEXT NOT NULL,
    channel_key TEXT,
    title TEXT,
    agy_conversation_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(channel, channel_key)
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system', 'tool')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS messages_conversation_time
    ON messages(conversation_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS messages_run_role
    ON messages(source_run_id, role)
    WHERE source_run_id IS NOT NULL AND role IN ('user', 'assistant');

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN (
        'queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted', 'orphaned'
    )),
    backend TEXT NOT NULL,
    backend_conversation_id TEXT,
    worker_id TEXT,
    request_json TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS one_running_run_per_conversation
    ON runs(conversation_id) WHERE status='running';
CREATE INDEX IF NOT EXISTS runs_status_created ON runs(status, created_at);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    source_sequence INTEGER,
    event_type TEXT NOT NULL,
    conversation_id TEXT,
    payload_json TEXT NOT NULL,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, sequence),
    UNIQUE(run_id, source_sequence)
);
CREATE INDEX IF NOT EXISTS events_run_sequence ON events(run_id, sequence);

CREATE TABLE IF NOT EXISTS workers (
    external_id TEXT PRIMARY KEY,
    run_id TEXT UNIQUE REFERENCES runs(id) ON DELETE SET NULL,
    workspace_id TEXT REFERENCES workspaces(id),
    backend TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'running', 'exited', 'missing', 'orphaned', 'terminated'
    )),
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('episodic', 'curated', 'fact')),
    content TEXT NOT NULL,
    source TEXT NOT NULL,
    source_conversation_id TEXT,
    confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence >= 0 AND confidence <= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    source,
    content='memories',
    content_rowid='rowid',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS memories_after_insert AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, source)
    VALUES (new.rowid, new.content, new.source);
END;
CREATE TRIGGER IF NOT EXISTS memories_after_delete AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, source)
    VALUES ('delete', old.rowid, old.content, old.source);
END;
CREATE TRIGGER IF NOT EXISTS memories_after_update AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, source)
    VALUES ('delete', old.rowid, old.content, old.source);
    INSERT INTO memories_fts(rowid, content, source)
    VALUES (new.rowid, new.content, new.source);
END;
"""

_MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('episodic', 'curated', 'fact')),
    content TEXT NOT NULL,
    source TEXT NOT NULL,
    source_conversation_id TEXT,
    confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence >= 0 AND confidence <= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    source,
    content='memories',
    content_rowid='rowid',
    tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS memories_after_insert AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, source)
    VALUES (new.rowid, new.content, new.source);
END;
CREATE TRIGGER IF NOT EXISTS memories_after_delete AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, source)
    VALUES ('delete', old.rowid, old.content, old.source);
END;
CREATE TRIGGER IF NOT EXISTS memories_after_update AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, source)
    VALUES ('delete', old.rowid, old.content, old.source);
    INSERT INTO memories_fts(rowid, content, source)
    VALUES (new.rowid, new.content, new.source);
END;
"""

_CHANNEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_cursors (
    channel TEXT PRIMARY KEY,
    last_update_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_aliases (
    alias TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
DROP INDEX IF EXISTS workspace_alias_workspace;

CREATE TABLE IF NOT EXISTS channel_bindings (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    thread_key TEXT NOT NULL DEFAULT '',
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(channel, sender_id, chat_id, thread_key)
);

CREATE TABLE IF NOT EXISTS channel_inbox (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    provider_update_id INTEGER NOT NULL,
    sender_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    thread_key TEXT NOT NULL DEFAULT '',
    provider_message_id TEXT,
    text TEXT NOT NULL,
    command TEXT,
    conversation_id TEXT REFERENCES conversations(id),
    run_id TEXT REFERENCES runs(id),
    outbox_id TEXT,
    result_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(channel, provider_update_id)
);

CREATE TABLE IF NOT EXISTS cancellation_requests (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'PENDING', 'PROCESSING', 'COMPLETED', 'FAILED'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cancellation_status
    ON cancellation_requests(status, created_at);

CREATE TABLE IF NOT EXISTS channel_outbox (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    logical_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('MESSAGE', 'PRESENTATION')),
    run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL,
    thread_key TEXT NOT NULL DEFAULT '',
    provider_message_id TEXT,
    desired_text TEXT NOT NULL,
    delivered_text TEXT,
    status TEXT NOT NULL CHECK(status IN (
        'PENDING', 'SENDING', 'RETRY_WAIT', 'DELIVERED', 'UNCERTAIN', 'FAILED'
    )),
    event_sequence INTEGER NOT NULL DEFAULT 0,
    delivery_version INTEGER NOT NULL DEFAULT 1,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    lease_until TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(channel, logical_key)
);
CREATE INDEX IF NOT EXISTS channel_outbox_delivery
    ON channel_outbox(status, available_at);
CREATE UNIQUE INDEX IF NOT EXISTS channel_outbox_run
    ON channel_outbox(channel, run_id) WHERE run_id IS NOT NULL;
"""
