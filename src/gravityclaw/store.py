"""Transactional SQLite state for the reliable GravityClaw core.

GravityClaw stores backend conversation identifiers, never backend credentials.
Every run event has a monotonically increasing per-run sequence. Lifecycle
transitions and their corresponding events commit in the same transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .events import AgentEvent


SCHEMA_VERSION = 6
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
SCHEDULE_TYPES = ("one_shot", "interval", "cron", "heartbeat")
SCHEDULE_CONCURRENCY = ("SKIP", "QUEUE", "REPLACE")
MISFIRE_POLICIES = ("MISFIRE_SKIP", "MISFIRE_RUN_ONCE", "MISFIRE_CATCH_UP")
TRIGGER_STATES = (
    "PENDING", "CLAIMED", "DISPATCHED", "RUNNING", "COMPLETED", "SKIPPED",
    "MISSED", "FAILED", "CANCELLED",
)


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
class Artifact:
    id: str
    run_id: str
    conversation_id: str
    kind: str
    content: str
    excerpt: str
    summary: str
    sha256: str
    characters: int
    relevance: int
    created_at: str


@dataclass(frozen=True, slots=True)
class ContextWatermark:
    conversation_id: str
    backend_conversation_id: str | None
    last_run_id: str
    last_message_id: str | None
    identity_hashes: dict[str, str]
    context_fingerprint: str
    updated_at: str


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


@dataclass(frozen=True, slots=True)
class ScheduleRecord:
    id: str
    name: str
    enabled: bool
    trigger_type: str
    expression: str
    timezone: str
    prompt: str
    context_profile: str
    workspace_id: str
    conversation_policy: str
    concurrency_policy: str
    misfire_policy: str
    misfire_grace_seconds: int
    notification_policy: str
    notification_channel: str | None
    notification_chat_id: str | None
    generation: int
    next_run_at: str | None
    last_run_at: str | None
    created_at: str
    updated_at: str
    deleted_at: str | None


@dataclass(frozen=True, slots=True)
class TriggerRecord:
    id: str
    execution_key: str
    schedule_id: str
    generation: int
    scheduled_for: str
    state: str
    claim_owner: str | None
    lease_until: str | None
    run_id: str | None
    decision_reason: str | None
    attempt_count: int
    created_at: str
    claimed_at: str | None
    dispatched_at: str | None
    finished_at: str | None
    updated_at: str


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
                connection.executescript(_CONTEXT_SCHEMA)
                connection.executescript(_SCHEDULER_SCHEMA)
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
                version = 4
            if version == 4:
                connection.executescript(_CONTEXT_SCHEMA)
                self._set_schema_version(connection, 5)
                version = 5
            if version == 5:
                connection.executescript(_SCHEDULER_SCHEMA)
                self._set_schema_version(connection, 6)
            connection.executescript(_SCHEMA)
            connection.executescript(_CHANNEL_SCHEMA)
            connection.executescript(_CONTEXT_SCHEMA)
            connection.executescript(_SCHEDULER_SCHEMA)
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

    def get_conversation_by_channel_key(
        self, channel: str, channel_key: str
    ) -> Conversation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE channel=? AND channel_key=?",
                (channel, channel_key),
            ).fetchone()
        return _conversation(row) if row is not None else None

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
            connection.execute(
                "UPDATE context_watermarks SET backend_conversation_id=?, updated_at=? "
                "WHERE conversation_id=?",
                (backend_id, now, conversation_id),
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

    def get_run_message_id(self, run_id: str, role: str = "user") -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM messages WHERE source_run_id=? AND role=?",
                (run_id, role),
            ).fetchone()
        return str(row["id"]) if row is not None else None

    def messages_after(
        self,
        conversation_id: str,
        *,
        after_message_id: str | None = None,
        exclude_run_id: str | None = None,
        limit: int = 10_000,
    ) -> list[Message]:
        parameters: list[Any] = [conversation_id]
        boundary = ""
        if after_message_id is not None:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT rowid FROM messages WHERE id=? AND conversation_id=?",
                    (after_message_id, conversation_id),
                ).fetchone()
            if row is None:
                raise ValueError("summary boundary message no longer exists")
            boundary = "AND rowid>?"
            parameters.append(int(row["rowid"]))
        exclusion = ""
        if exclude_run_id is not None:
            exclusion = "AND (source_run_id IS NULL OR source_run_id<>?)"
            parameters.append(exclude_run_id)
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM messages WHERE conversation_id=? {boundary} {exclusion}
                    ORDER BY rowid LIMIT ?""",
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
            prompt_sha256 = hashlib.sha256(execution_prompt.encode("utf-8")).hexdigest()
            declared_sha256 = manifest.get("prompt_sha256")
            if declared_sha256 is not None and declared_sha256 != prompt_sha256:
                raise ValueError("context manifest prompt hash does not match snapshot")
            now = utc_now()
            connection.execute(
                "UPDATE runs SET request_json=?, version=version+1 WHERE id=?",
                (json.dumps(request, ensure_ascii=False, separators=(",", ":")), run_id),
            )
            connection.execute(
                """INSERT INTO context_manifests(
                    run_id, profile, lifecycle, budget_tokens, used_tokens,
                    prompt_sha256, identity_fingerprint, context_fingerprint,
                    manifest_json, created_at, updated_at
                ) VALUES(?, ?, 'COMPILED', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    str(manifest.get("profile", "chat")),
                    int(manifest.get("budget_tokens", 0)),
                    int(manifest.get("estimated_tokens", 0)),
                    prompt_sha256,
                    str(manifest.get("identity_fingerprint", "")),
                    str(manifest.get("context_fingerprint", "")),
                    json.dumps(dict(manifest), ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                ),
            )
            summary = manifest.get("summary_proposal")
            if isinstance(summary, Mapping):
                connection.execute(
                    """INSERT OR IGNORE INTO conversation_summaries(
                        id, conversation_id, version, first_message_id,
                        last_message_id, message_count, content, sha256, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()),
                        str(summary["conversation_id"]),
                        int(summary["version"]),
                        str(summary["first_message_id"]),
                        str(summary["last_message_id"]),
                        int(summary["message_count"]),
                        str(summary["content"]),
                        str(summary["sha256"]),
                        now,
                    ),
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
            run = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
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
            manifest_row = connection.execute(
                "SELECT manifest_json FROM context_manifests WHERE run_id=?", (run_id,)
            ).fetchone()
            if manifest_row is not None:
                manifest = json.loads(manifest_row["manifest_json"])
                identity_hashes = {
                    str(item["label"]): str(item["sha256"])
                    for item in manifest.get("sources", [])
                    if isinstance(item, dict)
                    and item.get("category") == "identity"
                    and item.get("included")
                    and item.get("sha256")
                }
                backend_row = connection.execute(
                    "SELECT agy_conversation_id FROM conversations WHERE id=?",
                    (run["conversation_id"],),
                ).fetchone()
                connection.execute(
                    "UPDATE context_manifests SET lifecycle='DISPATCHED', "
                    "dispatched_at=?, updated_at=? WHERE run_id=? AND lifecycle='COMPILED'",
                    (now, now, run_id),
                )
                connection.execute(
                    """INSERT INTO context_watermarks(
                        conversation_id, backend_conversation_id, last_run_id,
                        last_message_id, identity_hashes_json,
                        context_fingerprint, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(conversation_id) DO UPDATE SET
                        backend_conversation_id=excluded.backend_conversation_id,
                        last_run_id=excluded.last_run_id,
                        last_message_id=excluded.last_message_id,
                        identity_hashes_json=excluded.identity_hashes_json,
                        context_fingerprint=excluded.context_fingerprint,
                        updated_at=excluded.updated_at""",
                    (
                        run["conversation_id"],
                        backend_row["agy_conversation_id"] if backend_row else None,
                        run_id,
                        manifest.get("last_message_id"),
                        json.dumps(identity_hashes, separators=(",", ":"), sort_keys=True),
                        str(manifest.get("context_fingerprint", "")),
                        now,
                    ),
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
            if status in TERMINAL_RUN_STATUSES:
                connection.execute(
                    "UPDATE context_manifests SET lifecycle='ARCHIVED', archived_at=?, "
                    "updated_at=? WHERE run_id=? AND lifecycle IN ('COMPILED','DISPATCHED')",
                    (now, now, run_id),
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

    # ------------------------------------------------------------------
    # Durable scheduling state. The scheduler never edits runs directly
    # except through submit_scheduled_run(), which atomically links a claimed
    # occurrence to the ordinary queued-run lifecycle.

    def create_schedule(
        self,
        *,
        name: str,
        trigger_type: str,
        expression: str,
        timezone: str,
        prompt: str,
        context_profile: str,
        workspace_id: str,
        next_run_at: str | None,
        conversation_policy: str = "new",
        concurrency_policy: str = "QUEUE",
        misfire_policy: str = "MISFIRE_RUN_ONCE",
        misfire_grace_seconds: int = 3600,
        notification_policy: str = "silent",
        notification_channel: str | None = None,
        notification_chat_id: str | None = None,
        schedule_id: str | None = None,
    ) -> ScheduleRecord:
        if trigger_type not in SCHEDULE_TYPES:
            raise ValueError(f"invalid schedule type: {trigger_type}")
        if concurrency_policy not in SCHEDULE_CONCURRENCY:
            raise ValueError(f"invalid concurrency policy: {concurrency_policy}")
        if misfire_policy not in MISFIRE_POLICIES:
            raise ValueError(f"invalid misfire policy: {misfire_policy}")
        if conversation_policy not in {"new", "resume"}:
            raise ValueError(f"invalid conversation policy: {conversation_policy}")
        if notification_policy not in {"silent", "actionable"}:
            raise ValueError(f"invalid notification policy: {notification_policy}")
        if not name.strip() or not prompt.strip() or not expression.strip():
            raise ValueError("schedule name, expression, and prompt are required")
        if misfire_grace_seconds < 0:
            raise ValueError("misfire grace must not be negative")
        now = utc_now()
        record = ScheduleRecord(
            schedule_id or str(uuid.uuid4()), name.strip(), True, trigger_type,
            expression.strip(), timezone, prompt.strip(), context_profile,
            workspace_id, conversation_policy, concurrency_policy, misfire_policy,
            int(misfire_grace_seconds), notification_policy, notification_channel,
            notification_chat_id, 1, next_run_at, None, now, now, None,
        )
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)
            ).fetchone() is None:
                raise KeyError(f"workspace not found: {workspace_id}")
            connection.execute(
                """INSERT INTO schedules(
                    id, name, enabled, trigger_type, expression, timezone, prompt,
                    context_profile, workspace_id, conversation_policy,
                    concurrency_policy, misfire_policy, misfire_grace_seconds,
                    notification_policy, notification_channel, notification_chat_id,
                    generation, next_run_at, created_at, updated_at
                ) VALUES(?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record.id, record.name, record.trigger_type, record.expression,
                 record.timezone, record.prompt, record.context_profile,
                 record.workspace_id, record.conversation_policy,
                 record.concurrency_policy, record.misfire_policy,
                 record.misfire_grace_seconds, record.notification_policy,
                 record.notification_channel, record.notification_chat_id,
                 record.generation,
                 record.next_run_at, record.created_at, record.updated_at),
            )
        return record

    def get_schedule(self, schedule_id: str, *, include_deleted: bool = False) -> ScheduleRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM schedules WHERE id=?" +
                ("" if include_deleted else " AND deleted_at IS NULL"),
                (schedule_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"schedule not found: {schedule_id}")
        return _schedule(row)

    def list_schedules(self, *, include_deleted: bool = False) -> list[ScheduleRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM schedules" +
                ("" if include_deleted else " WHERE deleted_at IS NULL") +
                " ORDER BY created_at"
            ).fetchall()
        return [_schedule(row) for row in rows]

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> ScheduleRecord:
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE schedules SET enabled=?, updated_at=? "
                "WHERE id=? AND deleted_at IS NULL",
                (1 if enabled else 0, now, schedule_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"schedule not found: {schedule_id}")
        return self.get_schedule(schedule_id)

    def delete_schedule(self, schedule_id: str) -> ScheduleRecord:
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE schedules SET enabled=0, deleted_at=?, updated_at=? "
                "WHERE id=? AND deleted_at IS NULL",
                (now, now, schedule_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"schedule not found: {schedule_id}")
            connection.execute(
                "UPDATE trigger_occurrences SET state='SKIPPED', decision_reason=? "
                "WHERE schedule_id=? AND state IN ('PENDING','CLAIMED')",
                ("schedule deleted", schedule_id),
            )
        return self.get_schedule(schedule_id, include_deleted=True)

    def update_schedule_generation(
        self, schedule_id: str, *, next_run_at: str | None = None,
        enabled: bool | None = None,
    ) -> ScheduleRecord:
        """Create a new recurrence generation; old trigger identities stay auditable."""
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM schedules WHERE id=? AND deleted_at IS NULL",
                (schedule_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"schedule not found: {schedule_id}")
            connection.execute(
                "UPDATE trigger_occurrences SET state='SKIPPED', decision_reason=?, "
                "finished_at=?, updated_at=? WHERE schedule_id=? "
                "AND state IN ('PENDING','CLAIMED')",
                ("superseded by schedule generation", now, now, schedule_id),
            )
            connection.execute(
                """UPDATE schedules SET generation=generation+1,
                   next_run_at=COALESCE(?, next_run_at),
                   enabled=COALESCE(?, enabled), updated_at=? WHERE id=?""",
                (next_run_at, None if enabled is None else int(enabled), now, schedule_id),
            )
        return self.get_schedule(schedule_id)

    def materialize_triggers(
        self,
        schedule_id: str,
        generation: int,
        occurrences: Sequence[tuple[str, str, str | None]],
        *,
        next_run_at: str | None,
        last_run_at: str | None,
    ) -> list[TriggerRecord]:
        """Insert occurrence decisions and advance the schedule in one transaction."""
        now = utc_now()
        inserted: list[str] = []
        with self._connect() as connection:
            row = connection.execute(
                "SELECT generation FROM schedules WHERE id=? AND deleted_at IS NULL",
                (schedule_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"schedule not found: {schedule_id}")
            if int(row["generation"]) != generation:
                return []
            for scheduled_for, state, reason in occurrences:
                if state not in TRIGGER_STATES:
                    raise ValueError(f"invalid trigger state: {state}")
                execution_key = f"{schedule_id}:{generation}:{scheduled_for}"
                trigger_id = str(uuid.uuid5(uuid.NAMESPACE_URL, execution_key))
                connection.execute(
                    """INSERT OR IGNORE INTO trigger_occurrences(
                       id, execution_key, schedule_id, generation, scheduled_for,
                       state, decision_reason, attempt_count, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                    (trigger_id, execution_key, schedule_id, generation,
                     scheduled_for, state, reason, now, now),
                )
                if connection.execute(
                    "SELECT changes()"
                ).fetchone()[0]:
                    inserted.append(trigger_id)
            connection.execute(
                """UPDATE schedules SET next_run_at=?, last_run_at=?, updated_at=?
                   WHERE id=? AND generation=?""",
                (next_run_at, last_run_at, now, schedule_id, generation),
            )
        return [self.get_trigger(item) for item in inserted]

    def get_trigger(self, trigger_id: str) -> TriggerRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM trigger_occurrences WHERE id=?", (trigger_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"trigger not found: {trigger_id}")
        return _trigger(row)

    def list_triggers(
        self, *, schedule_id: str | None = None,
        states: Sequence[str] | None = None, limit: int = 1000,
    ) -> list[TriggerRecord]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if schedule_id is not None:
            clauses.append("schedule_id=?")
            parameters.append(schedule_id)
        if states:
            clauses.append("state IN (" + ",".join("?" for _ in states) + ")")
            parameters.extend(states)
        parameters.append(limit)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM trigger_occurrences" + where +
                " ORDER BY scheduled_for, id LIMIT ?", parameters
            ).fetchall()
        return [_trigger(row) for row in rows]

    def claim_trigger(
        self, trigger_id: str, owner: str, *, lease_seconds: int = 60,
        now: str | None = None,
    ) -> TriggerRecord | None:
        claimed_at = now or utc_now()
        expiry = _add_seconds(claimed_at, lease_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM trigger_occurrences WHERE id=?", (trigger_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"trigger not found: {trigger_id}")
            available = row["state"] == "PENDING" or (
                row["state"] == "CLAIMED" and row["lease_until"] is not None
                and row["lease_until"] <= claimed_at
            )
            if not available:
                return None
            connection.execute(
                """UPDATE trigger_occurrences SET state='CLAIMED', claim_owner=?,
                   lease_until=?, claimed_at=COALESCE(claimed_at, ?),
                   attempt_count=attempt_count+1, updated_at=? WHERE id=?""",
                (owner, expiry, claimed_at, claimed_at, trigger_id),
            )
        return self.get_trigger(trigger_id)

    def recover_trigger_leases(self) -> int:
        """Return pre-dispatch claims to PENDING after a gateway crash."""
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE trigger_occurrences SET state='PENDING', claim_owner=NULL,
                   lease_until=NULL, updated_at=?
                   WHERE state='CLAIMED' AND run_id IS NULL
                     AND lease_until IS NOT NULL AND lease_until<=?""",
                (now, now),
            )
            connection.execute(
                "UPDATE trigger_occurrences SET notification_state='PENDING', updated_at=? "
                "WHERE notification_state='PROCESSING' AND notification_decided_at IS NULL",
                (now,),
            )
        return cursor.rowcount

    def decide_trigger(
        self, trigger_id: str, state: str, reason: str, *,
        expected: Sequence[str] = ("PENDING",),
    ) -> bool:
        if state not in {"SKIPPED", "MISSED", "CANCELLED"}:
            raise ValueError("decision state must be SKIPPED, MISSED, or CANCELLED")
        placeholders = ",".join("?" for _ in expected)
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                f"""UPDATE trigger_occurrences SET state=?, decision_reason=?,
                    finished_at=?, updated_at=? WHERE id=? AND state IN ({placeholders})""",
                (state, reason, now, now, trigger_id, *expected),
            )
        return cursor.rowcount == 1

    def submit_scheduled_run(
        self, trigger_id: str, owner: str, conversation_id: str,
        request: Mapping[str, Any], *, backend: str = "agy-container",
    ) -> RunRecord:
        """Atomically turn a claimed occurrence into an ordinary queued run."""
        prompt = str(request.get("prompt", "")).strip()
        if not prompt:
            raise ValueError("scheduled prompt must not be empty")
        now = utc_now()
        run_id = str(uuid.uuid4())
        request_data = dict(request)
        trigger = self.get_trigger(trigger_id)
        request_data.update({
            "scheduled_trigger_id": trigger.id,
            "execution_key": trigger.execution_key,
        })
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM trigger_occurrences WHERE id=?", (trigger_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"trigger not found: {trigger_id}")
            if row["state"] != "CLAIMED" or row["claim_owner"] != owner:
                raise ValueError("trigger is not claimed by this scheduler")
            existing = connection.execute(
                "SELECT * FROM runs WHERE id IN (SELECT run_id FROM trigger_occurrences WHERE id=?)",
                (trigger_id,),
            ).fetchone()
            if existing is not None:
                return _run(existing)
            message_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO runs(
                    id, conversation_id, status, backend, request_json, created_at, version
                ) VALUES(?, ?, 'queued', ?, ?, ?, 1)""",
                (run_id, conversation_id, backend,
                 json.dumps(request_data, ensure_ascii=False, separators=(",", ":")), now),
            )
            connection.execute(
                """INSERT INTO messages(
                    id, conversation_id, role, content, created_at, source_run_id
                ) VALUES(?, ?, 'user', ?, ?, ?)""",
                (message_id, conversation_id, prompt, now, run_id),
            )
            connection.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?",
                (now, conversation_id),
            )
            self._append_event_tx(
                connection, run_id, "run.queued", None,
                {"status": "queued", "scheduled_trigger_id": trigger_id}, None, None,
            )
            connection.execute(
                """UPDATE trigger_occurrences SET state='DISPATCHED', run_id=?,
                   dispatched_at=?, lease_until=NULL, updated_at=? WHERE id=?""",
                (run_id, now, now, trigger_id),
            )
        return self.get_run(run_id)

    def sync_trigger_states(self) -> int:
        changed = 0
        now = utc_now()
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT t.id, t.state, r.status FROM trigger_occurrences t
                   JOIN runs r ON r.id=t.run_id
                   WHERE t.state IN ('DISPATCHED','RUNNING')"""
            ).fetchall()
            for row in rows:
                mapping = {
                    "running": "RUNNING", "completed": "COMPLETED",
                    "failed": "FAILED", "cancelled": "CANCELLED",
                    "interrupted": "FAILED", "orphaned": "FAILED",
                }
                state = mapping.get(str(row["status"]))
                if state and state != row["state"]:
                    connection.execute(
                        "UPDATE trigger_occurrences SET state=?, finished_at=?, updated_at=? "
                        "WHERE id=? AND state IN ('DISPATCHED','RUNNING')",
                        (state, now if state in {"COMPLETED", "FAILED", "CANCELLED"} else None,
                         now, row["id"]),
                    )
                    changed += 1
        return changed

    def count_active_triggers(self, schedule_id: str) -> int:
        with self._connect() as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM trigger_occurrences WHERE schedule_id=? "
                "AND state IN ('CLAIMED','DISPATCHED','RUNNING')", (schedule_id,)
            ).fetchone()[0])

    def claim_trigger_notification(self, trigger_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE trigger_occurrences SET notification_state='PROCESSING', "
                "updated_at=? WHERE id=? AND notification_state='PENDING'",
                (utc_now(), trigger_id),
            )
        return cursor.rowcount == 1

    def finish_trigger_notification(self, trigger_id: str, outbox_id: str | None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE trigger_occurrences SET notification_state='DONE', "
                "notification_outbox_id=?, notification_decided_at=?, updated_at=? WHERE id=?",
                (outbox_id, utc_now(), utc_now(), trigger_id),
            )

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

    def get_context_manifest(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM context_manifests WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            run = self.get_run(run_id)
            historical = run.request.get("context_manifest")
            if not isinstance(historical, dict):
                raise KeyError(f"context manifest not found: {run_id}")
            return {**historical, "lifecycle": "HISTORICAL"}
        manifest = json.loads(row["manifest_json"])
        manifest.update({
            "lifecycle": row["lifecycle"],
            "compiled_at": row["created_at"],
            "dispatched_at": row["dispatched_at"],
            "archived_at": row["archived_at"],
        })
        return manifest

    def get_context_watermark(self, conversation_id: str) -> ContextWatermark | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM context_watermarks WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        return ContextWatermark(
            row["conversation_id"], row["backend_conversation_id"],
            row["last_run_id"], row["last_message_id"],
            json.loads(row["identity_hashes_json"]), row["context_fingerprint"],
            row["updated_at"],
        )

    def latest_summary_version(self, conversation_id: str) -> int:
        with self._connect() as connection:
            return int(connection.execute(
                "SELECT COALESCE(MAX(version),0) FROM conversation_summaries "
                "WHERE conversation_id=?", (conversation_id,),
            ).fetchone()[0])

    def latest_context_summary(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_summaries WHERE conversation_id=? "
                "ORDER BY version DESC LIMIT 1", (conversation_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_conversation_summaries(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM conversation_summaries WHERE conversation_id=? "
                "ORDER BY version", (conversation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_artifact(
        self,
        run_id: str,
        *,
        kind: str,
        content: str,
        summary: str = "",
        excerpt_characters: int = 1200,
    ) -> str:
        if not kind.strip() or not content:
            raise ValueError("artifact kind and content are required")
        run = self.get_run(run_id)
        artifact_id = str(uuid.uuid4())
        excerpt = content[:excerpt_characters]
        now = utc_now()
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO artifacts(
                    id, run_id, conversation_id, kind, content, excerpt, summary,
                    sha256, characters, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (artifact_id, run_id, run.conversation_id, kind.strip(), content,
                 excerpt, summary.strip(), digest, len(content), now),
            )
        return artifact_id

    def relevant_artifacts(
        self, conversation_id: str, query: str, *, limit: int = 8
    ) -> list[Artifact]:
        terms = sorted(set(re.findall(r"[^\W_]+", query.casefold(), flags=re.UNICODE)))
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, run_id, conversation_id, kind, excerpt, summary,
                          sha256, characters, created_at
                   FROM artifacts WHERE conversation_id=?
                   ORDER BY created_at DESC, id""",
                (conversation_id,),
            ).fetchall()
        ranked: list[Artifact] = []
        for row in rows:
            searchable = f"{row['kind']} {row['summary']} {row['excerpt']}".casefold()
            relevance = sum(1 for term in terms if term in searchable)
            if terms and relevance == 0:
                continue
            ranked.append(Artifact(
                row["id"], row["run_id"], row["conversation_id"], row["kind"],
                "", row["excerpt"], row["summary"], row["sha256"],
                int(row["characters"]), relevance, row["created_at"],
            ))
        ranked.sort(key=lambda item: (-item.relevance, item.created_at, item.id))
        return ranked[:limit]


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


def _schedule(row: sqlite3.Row) -> ScheduleRecord:
    return ScheduleRecord(
        id=row["id"], name=row["name"], enabled=bool(row["enabled"]),
        trigger_type=row["trigger_type"], expression=row["expression"],
        timezone=row["timezone"], prompt=row["prompt"],
        context_profile=row["context_profile"], workspace_id=row["workspace_id"],
        conversation_policy=row["conversation_policy"],
        concurrency_policy=row["concurrency_policy"],
        misfire_policy=row["misfire_policy"],
        misfire_grace_seconds=int(row["misfire_grace_seconds"]),
        notification_policy=row["notification_policy"],
        notification_channel=row["notification_channel"],
        notification_chat_id=row["notification_chat_id"],
        generation=int(row["generation"]), next_run_at=row["next_run_at"],
        last_run_at=row["last_run_at"], created_at=row["created_at"],
        updated_at=row["updated_at"], deleted_at=row["deleted_at"],
    )


def _trigger(row: sqlite3.Row) -> TriggerRecord:
    return TriggerRecord(
        id=row["id"], execution_key=row["execution_key"],
        schedule_id=row["schedule_id"], generation=int(row["generation"]),
        scheduled_for=row["scheduled_for"], state=row["state"],
        claim_owner=row["claim_owner"], lease_until=row["lease_until"],
        run_id=row["run_id"], decision_reason=row["decision_reason"],
        attempt_count=int(row["attempt_count"]), created_at=row["created_at"],
        claimed_at=row["claimed_at"], dispatched_at=row["dispatched_at"],
        finished_at=row["finished_at"], updated_at=row["updated_at"],
    )


def _add_seconds(value: str, seconds: int) -> str:
    return (datetime.fromisoformat(value).astimezone(UTC) +
            timedelta(seconds=seconds)).isoformat()


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

_CONTEXT_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_manifests (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    profile TEXT NOT NULL,
    lifecycle TEXT NOT NULL CHECK(lifecycle IN ('COMPILED','DISPATCHED','ARCHIVED')),
    budget_tokens INTEGER NOT NULL,
    used_tokens INTEGER NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    identity_fingerprint TEXT NOT NULL,
    context_fingerprint TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    dispatched_at TEXT,
    archived_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS context_watermarks (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    backend_conversation_id TEXT,
    last_run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    last_message_id TEXT,
    identity_hashes_json TEXT NOT NULL,
    context_fingerprint TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    first_message_id TEXT NOT NULL,
    last_message_id TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    content TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(conversation_id, version),
    UNIQUE(conversation_id, first_message_id, last_message_id)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    summary TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    characters INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS artifacts_conversation_time
    ON artifacts(conversation_id, created_at);
"""

_SCHEDULER_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    trigger_type TEXT NOT NULL CHECK(trigger_type IN ('one_shot','interval','cron','heartbeat')),
    expression TEXT NOT NULL,
    timezone TEXT NOT NULL,
    prompt TEXT NOT NULL,
    context_profile TEXT NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    conversation_policy TEXT NOT NULL CHECK(conversation_policy IN ('new','resume')),
    concurrency_policy TEXT NOT NULL CHECK(concurrency_policy IN ('SKIP','QUEUE','REPLACE')),
    misfire_policy TEXT NOT NULL CHECK(misfire_policy IN ('MISFIRE_SKIP','MISFIRE_RUN_ONCE','MISFIRE_CATCH_UP')),
    misfire_grace_seconds INTEGER NOT NULL CHECK(misfire_grace_seconds >= 0),
    notification_policy TEXT NOT NULL DEFAULT 'silent' CHECK(notification_policy IN ('silent','actionable')),
    notification_channel TEXT,
    notification_chat_id TEXT,
    generation INTEGER NOT NULL DEFAULT 1,
    next_run_at TEXT,
    last_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS schedules_due ON schedules(enabled, next_run_at);

CREATE TABLE IF NOT EXISTS trigger_occurrences (
    id TEXT PRIMARY KEY,
    execution_key TEXT NOT NULL UNIQUE,
    schedule_id TEXT NOT NULL REFERENCES schedules(id),
    generation INTEGER NOT NULL,
    scheduled_for TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'PENDING','CLAIMED','DISPATCHED','RUNNING','COMPLETED','SKIPPED',
        'MISSED','FAILED','CANCELLED'
    )),
    claim_owner TEXT,
    lease_until TEXT,
    run_id TEXT UNIQUE REFERENCES runs(id) ON DELETE SET NULL,
    decision_reason TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    dispatched_at TEXT,
    finished_at TEXT,
    notification_state TEXT NOT NULL DEFAULT 'PENDING' CHECK(notification_state IN ('PENDING','PROCESSING','DONE')),
    notification_decided_at TEXT,
    notification_outbox_id TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(schedule_id, generation, scheduled_for)
);
CREATE INDEX IF NOT EXISTS trigger_due ON trigger_occurrences(state, scheduled_for);
CREATE INDEX IF NOT EXISTS trigger_schedule ON trigger_occurrences(schedule_id, scheduled_for);
"""
