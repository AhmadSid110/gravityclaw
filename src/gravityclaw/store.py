"""Transactional SQLite state for the reliable GravityClaw core.

GravityClaw stores backend conversation identifiers, never backend credentials.
Every run event has a monotonically increasing per-run sequence. Lifecycle
transitions and their corresponding events commit in the same transaction.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .events import AgentEvent
from .models import get_model_context_limit


SCHEMA_VERSION = 18
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

FLOW_STATUSES = (
    "QUEUED",
    "RUNNING",
    "WAITING",
    "BLOCKED",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
)
TERMINAL_FLOW_STATUSES = ("SUCCEEDED", "FAILED", "CANCELLED")

TASK_STATUSES = (
    "TRIAGE",
    "TODO",
    "READY",
    "RUNNING",
    "BLOCKED",
    "DONE",
    "FAILED",
    "CANCELLED",
    "ARCHIVED",
)
TERMINAL_TASK_STATUSES = ("DONE", "FAILED", "CANCELLED", "ARCHIVED")

TASK_PRIORITIES = ("LOW", "MEDIUM", "HIGH", "URGENT")

BLOCK_REASONS = (
    "dependency",
    "needs_user_input",
    "missing_capability",
    "transient_failure",
    "external_service",
    "review_required",
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
    model_override: str | None
    effort_override: str | None
    created_at: str
    updated_at: str
    kind: str = "normal"
    archived_at: str | None = None


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
    version: int
    next_run_at: str | None
    last_run_at: str | None
    created_at: str
    updated_at: str
    deleted_at: str | None


@dataclass(frozen=True, slots=True)
class AuditRecord:
    id: int
    actor: str
    action: str
    resource_type: str
    resource_id: str | None
    expected_version: int | None
    resulting_version: int | None
    payload: dict[str, Any]
    created_at: str


class VersionConflict(ValueError):
    """A control-plane mutation was based on stale resource state."""


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


GOAL_STATUSES = ("active", "paused", "completed", "cancelled", "failed")
GOAL_VERDICTS = ("continue", "done", "failed", "paused")


@dataclass(frozen=True, slots=True)
class GoalRecord:
    id: str
    conversation_id: str
    objective: str
    acceptance: list[dict[str, Any]]
    status: str
    max_turns: int
    turns_used: int
    current_step: str | None
    last_run_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class GoalEvaluationRecord:
    id: str
    goal_id: str
    run_id: str | None
    turn_number: int
    verdict: str
    reason: str | None
    acceptance_state: list[dict[str, Any]]
    created_at: str


@dataclass(frozen=True, slots=True)
class TaskFlowRecord:
    id: str
    title: str
    objective: str
    status: str
    workspace_id: str
    context_profile: str
    state_json: dict[str, Any]
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class FlowTaskRecord:
    id: str
    flow_id: str
    title: str
    body: str
    acceptance_json: list[dict[str, Any]] | list[str]
    status: str
    assignee_profile: str
    priority: str
    workspace_id: str
    idempotency_key: str | None
    max_attempts: int
    block_reason: str | None
    block_detail: str | None
    block_recurrence_count: int
    version: int
    created_at: str
    updated_at: str
    parent_ids: list[str] = field(default_factory=list)
    child_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TaskAttemptRecord:
    id: str
    task_id: str
    run_id: str
    attempt_no: int
    started_at: str
    finished_at: str | None
    outcome: str | None
    summary: str | None


@dataclass(frozen=True, slots=True)
class TaskCommentRecord:
    id: str
    task_id: str
    author_type: str
    author_id: str
    body: str
    created_at: str


@dataclass(frozen=True, slots=True)
class TaskClaimRecord:
    task_id: str
    owner: str
    lease_until: str
    heartbeat_at: str
    heartbeat_message: str | None


class Store:
    """Small synchronous repository with one connection per transaction."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                connection.executescript(_SCHEMA)
                connection.executescript(_MEMORY_SCHEMA)
                connection.executescript(_CHANNEL_SCHEMA)
                connection.executescript(_CONTEXT_SCHEMA)
                connection.executescript(_SCHEDULER_SCHEMA)
                connection.executescript(_CAPABILITY_SCHEMA)
                connection.executescript(_CONTROL_SCHEMA)
                connection.executescript(_IDENTITY_SCHEMA)
                connection.executescript(_GOAL_SCHEMA)
                connection.executescript(_LEARNING_SCHEMA)
                connection.executescript(_SKILLS_SCHEMA)
                connection.executescript(_CONTEXT_SNAPSHOT_SCHEMA)
                connection.executescript(_ATTACHMENT_SCHEMA)
                connection.executescript(_TASKFLOW_SCHEMA)
                connection.executescript(_TELEMETRY_SCHEMA)
                self._ensure_message_index(connection)
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                try:
                    self.path.chmod(0o600)
                except OSError:
                    pass
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
                version = 6
            if version == 6:
                connection.executescript(_CAPABILITY_SCHEMA)
                self._set_schema_version(connection, 7)
                version = 7
            if version == 7:
                columns = {
                    str(item["name"])
                    for item in connection.execute("PRAGMA table_info(schedules)").fetchall()
                }
                if "version" not in columns:
                    connection.execute(
                        "ALTER TABLE schedules ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
                    )
                connection.executescript(_CONTROL_SCHEMA)
                self._set_schema_version(connection, 8)
                version = 8
            if version == 8:
                connection.executescript(_IDENTITY_SCHEMA)
                self._set_schema_version(connection, 9)
                version = 9
            if version == 9:
                columns = {
                    str(item["name"])
                    for item in connection.execute("PRAGMA table_info(conversations)").fetchall()
                }
                if "model_override" not in columns:
                    connection.execute(
                        "ALTER TABLE conversations ADD COLUMN model_override TEXT"
                    )
                self._set_schema_version(connection, 10)
                version = 10
            if version == 10:
                columns = {
                    str(item["name"])
                    for item in connection.execute("PRAGMA table_info(conversations)").fetchall()
                }
                if "effort_override" not in columns:
                    connection.execute(
                        "ALTER TABLE conversations ADD COLUMN effort_override TEXT"
                    )
                self._set_schema_version(connection, 11)
                version = 11
            if version == 11:
                connection.executescript(_GOAL_SCHEMA)
                self._set_schema_version(connection, 12)
                version = 12
            if version == 12:
                connection.executescript(_LEARNING_SCHEMA)
                self._set_schema_version(connection, 13)
                version = 13
            if version == 13:
                connection.executescript(_SKILLS_SCHEMA)
                self._set_schema_version(connection, 14)
                version = 14
            if version == 14:
                connection.executescript(_CONTEXT_SNAPSHOT_SCHEMA)
                self._set_schema_version(connection, 15)
                version = 15
            if version == 15:
                connection.executescript(_ATTACHMENT_SCHEMA)
                self._set_schema_version(connection, 16)
                version = 16
            if version == 16:
                columns = {
                    str(item["name"])
                    for item in connection.execute("PRAGMA table_info(conversations)").fetchall()
                }
                if "kind" not in columns:
                    connection.execute(
                        "ALTER TABLE conversations ADD COLUMN kind TEXT NOT NULL DEFAULT 'normal'"
                    )
                if "archived_at" not in columns:
                    connection.execute(
                        "ALTER TABLE conversations ADD COLUMN archived_at TEXT"
                    )
                self._set_schema_version(connection, 17)
                version = 17
            if version == 17:
                connection.executescript(_TASKFLOW_SCHEMA)
                self._set_schema_version(connection, 18)
                version = 18
            connection.executescript(_SCHEMA)
            connection.executescript(_MEMORY_SCHEMA)
            connection.executescript(_CHANNEL_SCHEMA)
            connection.executescript(_CONTEXT_SCHEMA)
            connection.executescript(_SCHEDULER_SCHEMA)
            connection.executescript(_CAPABILITY_SCHEMA)
            connection.executescript(_CONTROL_SCHEMA)
            connection.executescript(_IDENTITY_SCHEMA)
            connection.executescript(_GOAL_SCHEMA)
            connection.executescript(_LEARNING_SCHEMA)
            connection.executescript(_SKILLS_SCHEMA)
            connection.executescript(_CONTEXT_SNAPSHOT_SCHEMA)
            connection.executescript(_ATTACHMENT_SCHEMA)
            connection.executescript(_TASKFLOW_SCHEMA)
            connection.executescript(_TELEMETRY_SCHEMA)
            self._ensure_message_index(connection)
            for ws_row in connection.execute("SELECT id FROM workspaces").fetchall():
                self._ensure_main_conversation_tx(connection, str(ws_row["id"]))
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

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

    @staticmethod
    def _ensure_main_conversation_tx(connection: sqlite3.Connection, workspace_id: str) -> None:
        row = connection.execute(
            "SELECT id FROM conversations WHERE workspace_id=? AND kind='main' AND archived_at IS NULL",
            (workspace_id,),
        ).fetchone()
        if row is None:
            now = utc_now()
            main_id = f"conv_main_{workspace_id[:8]}" if workspace_id != "default" else "conv_main"
            existing = connection.execute("SELECT id FROM conversations WHERE id=?", (main_id,)).fetchone()
            if existing is not None:
                main_id = f"conv_main_{uuid.uuid4().hex[:8]}"
            connection.execute(
                """INSERT INTO conversations(
                    id, workspace_id, channel, channel_key, title, kind,
                    model_override, effort_override, created_at, updated_at
                ) VALUES(?, ?, 'system', ?, 'Main', 'main', NULL, NULL, ?, ?)""",
                (main_id, workspace_id, f"system:main:{workspace_id}", now, now),
            )

    def ensure_main_conversation(self, workspace_id: str) -> Conversation:
        with self._connect() as connection:
            self._ensure_main_conversation_tx(connection, workspace_id)
            row = connection.execute(
                "SELECT * FROM conversations WHERE workspace_id=? AND kind='main' AND archived_at IS NULL",
                (workspace_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"main conversation not found for workspace {workspace_id}")
        return _conversation(row)

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
            self._ensure_main_conversation_tx(connection, record.id)
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
        kind: str = "normal",
        model_override: str | None = None,
    ) -> Conversation:
        now = utc_now()
        record = Conversation(
            str(uuid.uuid4()),
            workspace_id,
            channel,
            channel_key,
            title or ("Main" if kind == "main" else "New chat"),
            None,
            model_override,
            None,
            now,
            now,
            kind,
            None,
        )
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO conversations(
                    id, workspace_id, channel, channel_key, title, kind, model_override,
                    effort_override, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.id,
                    workspace_id,
                    channel,
                    channel_key,
                    record.title,
                    kind,
                    model_override,
                    None,
                    now,
                    now,
                ),
            )
        return record

    def archive_conversation(self, conversation_id: str) -> Conversation:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"conversation not found: {conversation_id}")
            if row["kind"] == "main":
                raise ValueError("Main conversation cannot be archived")
            now = utc_now()
            connection.execute(
                "UPDATE conversations SET archived_at=?, updated_at=? WHERE id=?",
                (now, now, conversation_id),
            )
            updated = connection.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            return _conversation(updated)

    def update_conversation(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        model_override: str | None = None,
    ) -> Conversation:
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"conversation not found: {conversation_id}")
            new_title = title if title is not None else row["title"]
            new_model = model_override if model_override is not None else row["model_override"]
            connection.execute(
                "UPDATE conversations SET title=?, model_override=?, updated_at=? WHERE id=?",
                (new_title, new_model, now, conversation_id),
            )
            updated = connection.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            return _conversation(updated)

    def restore_conversation(self, conversation_id: str) -> Conversation:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"conversation not found: {conversation_id}")
            now = utc_now()
            connection.execute(
                "UPDATE conversations SET archived_at=NULL, updated_at=? WHERE id=?",
                (now, conversation_id),
            )
            updated = connection.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            return _conversation(updated)

    def delete_conversation(self, conversation_id: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"conversation not found: {conversation_id}")
            if row["kind"] == "main":
                raise ValueError("Main conversation cannot be deleted")
            connection.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
            connection.execute("DELETE FROM conversation_summaries WHERE conversation_id=?", (conversation_id,))
            connection.execute("DELETE FROM attachments WHERE conversation_id=?", (conversation_id,))
            connection.execute("DELETE FROM channel_bindings WHERE conversation_id=?", (conversation_id,))
            connection.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))

    def search_conversations(
        self,
        query: str,
        *,
        workspace_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        clean = query.strip()
        if not clean:
            return []
        like_pattern = f"%{clean}%"
        params: list[Any] = [like_pattern, like_pattern]
        ws_clause = ""
        if workspace_id:
            ws_clause = " AND c.workspace_id = ?"
            params.append(workspace_id)
        params.append(limit)
        
        sql = f"""
            SELECT c.id as conversation_id, c.title, c.kind, m.id as message_id, m.role, m.content, m.created_at
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE c.archived_at IS NULL AND (m.content LIKE ? OR c.title LIKE ?) {ws_clause}
            ORDER BY m.created_at DESC
            LIMIT ?
        """
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [
            {
                "conversation_id": row["conversation_id"],
                "title": row["title"],
                "kind": row["kind"],
                "message_id": row["message_id"],
                "role": row["role"],
                "content": row["content"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

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

    def get_model_default(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='model_default'"
            ).fetchone()
        return str(row["value"]) if row is not None and row["value"] else None

    def ensure_model_default(self, model: str | None) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES('model_default', ?) "
                "ON CONFLICT(key) DO NOTHING",
                (model or "",),
            )

    def set_model_default(self, model: str | None) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES('model_default', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (model or "",),
            )

    def set_model_catalog(self, catalog: Mapping[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES('model_catalog', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(dict(catalog), ensure_ascii=False, separators=(",", ":")),),
            )

    def get_model_catalog(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='model_catalog'"
            ).fetchone()
        if row is None:
            return {"models": [], "default_model": self.get_model_default(), "agy_version": "unknown", "source": "server"}
        return json.loads(row["value"])

    def _snapshot_model_request_tx(
        self, connection: sqlite3.Connection, conversation_id: str,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        conversation = connection.execute(
            "SELECT model_override, effort_override FROM conversations WHERE id=?", (conversation_id,)
        ).fetchone()
        if conversation is None:
            raise KeyError(f"conversation not found: {conversation_id}")
        default_row = connection.execute(
            "SELECT value FROM metadata WHERE key='model_default'"
        ).fetchone()
        version_row = connection.execute(
            "SELECT value FROM metadata WHERE key='agy_version'"
        ).fetchone()
        requested = str(conversation["model_override"]).strip() if conversation["model_override"] else None
        resolved = requested or (str(default_row["value"]).strip() if default_row and default_row["value"] else None)
        effort = str(conversation["effort_override"]).strip() if conversation["effort_override"] else None
        if not effort:
            effort = "medium"
        snapshot = dict(request)
        snapshot["requested_model"] = requested
        snapshot["resolved_model"] = resolved
        snapshot["model_policy"] = "explicit" if requested else "default"
        snapshot["effort"] = effort
        snapshot["agy_version"] = str(request.get("agy_version") or (version_row["value"] if version_row else "unknown"))
        return snapshot

    def set_conversation_model(self, conversation_id: str, model: str | None) -> Conversation:
        value = model.strip() if model else None
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE conversations SET model_override=?, updated_at=? WHERE id=?",
                (value, utc_now(), conversation_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"conversation not found: {conversation_id}")
        return self.get_conversation(conversation_id)

    def set_conversation_effort(self, conversation_id: str, effort: str | None) -> Conversation:
        if effort and effort not in ("low", "medium", "high"):
            raise ValueError("effort must be low, medium, or high")
        value = effort.strip() if effort else None
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE conversations SET effort_override=?, updated_at=? WHERE id=?",
                (value, utc_now(), conversation_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"conversation not found: {conversation_id}")
        return self.get_conversation(conversation_id)

    def bind_backend_conversation(self, conversation_id: str, backend_id: str) -> None:
        if not backend_id:
            return
        now = utc_now()
        with self._connect() as connection:
            current = connection.execute(
                "SELECT agy_conversation_id FROM conversations WHERE id=?",
                (conversation_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"conversation not found: {conversation_id}")
            existing = current[0]
            if existing and existing != backend_id:
                raise ValueError(
                    f"conversation {conversation_id} already bound to "
                    f"{existing!r}; cannot silently replace with {backend_id!r}"
                )
            connection.execute(
                """UPDATE conversations
                   SET agy_conversation_id=?, updated_at=?
                   WHERE id=?""",
                (backend_id, now, conversation_id),
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
        with self._connect() as connection:
            request_snapshot = self._snapshot_model_request_tx(connection, conversation_id, request)
            request_json = json.dumps(request_snapshot, ensure_ascii=False, separators=(",", ":"))
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
            attachment_ids = request.get("attachment_ids") or []
            if isinstance(attachment_ids, (list, tuple)):
                for att_id in attachment_ids:
                    if isinstance(att_id, str) and att_id.strip():
                        connection.execute(
                            "UPDATE attachments SET message_id=? WHERE id=?",
                            (message_id, att_id.strip()),
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
        with self._connect() as connection:
            request_snapshot = self._snapshot_model_request_tx(connection, conversation_id, request)
            request_json = json.dumps(request_snapshot, ensure_ascii=False, separators=(",", ":"))
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

    def prepare_run_capabilities(
        self, run_id: str, manifest: Mapping[str, Any], snapshot_path: str
    ) -> RunRecord:
        """Persist an immutable capability snapshot before a worker starts."""
        if not snapshot_path:
            raise ValueError("capability snapshot path must not be empty")
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"run not found: {run_id}")
            if row["status"] != "running":
                raise ValueError("capabilities can only be prepared for a running run")
            request = json.loads(row["request_json"])
            existing = request.get("capability_manifest")
            if existing is not None:
                if existing != dict(manifest) or request.get("capability_snapshot") != snapshot_path:
                    raise ValueError("refusing to replace an existing capability snapshot")
                return _run(row)
            request["capability_manifest"] = dict(manifest)
            request["capability_snapshot"] = snapshot_path
            now = utc_now()
            connection.execute(
                "UPDATE runs SET request_json=?, version=version+1 WHERE id=?",
                (json.dumps(request, ensure_ascii=False, separators=(",", ":")), run_id),
            )
            connection.execute(
                """INSERT INTO capability_manifests(
                   run_id, workspace_id, profile, manifest_json, manifest_hash,
                   snapshot_path, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, str(manifest["workspace_id"]), str(manifest["profile"]),
                    json.dumps(dict(manifest), ensure_ascii=False, separators=(",", ":")),
                    str(manifest["manifest_hash"]), snapshot_path, now,
                ),
            )
            self._append_event_tx(
                connection, run_id, "run.capabilities_compiled", None,
                {"manifest_hash": manifest["manifest_hash"],
                 "skill_count": len(manifest.get("skills", [])),
                 "mcp_count": len(manifest.get("mcp", []))}, None, None,
            )
        return self.get_run(run_id)

    def get_capability_manifest(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM capability_manifests WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            run = self.get_run(run_id)
            value = run.request.get("capability_manifest")
            if not isinstance(value, dict):
                raise KeyError(f"capability manifest not found: {run_id}")
            return value
        return json.loads(row["manifest_json"])

    def list_capability_manifests(
        self, workspace_id: str, *, limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id, workspace_id, profile, manifest_json, manifest_hash, "
                "snapshot_path, created_at FROM capability_manifests "
                "WHERE workspace_id=? ORDER BY created_at DESC LIMIT ?",
                (workspace_id, limit),
            ).fetchall()
        return [
            {"run_id": row["run_id"], "workspace_id": row["workspace_id"],
             "profile": row["profile"], "manifest": json.loads(row["manifest_json"]),
             "manifest_hash": row["manifest_hash"], "snapshot_path": row["snapshot_path"],
             "created_at": row["created_at"]}
            for row in rows
        ]

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
            if assistant_response and assistant_response.strip():
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

    def get_usage_summary(self, *, days: int = 30) -> dict[str, Any]:
        """Aggregate run counts and token usage over recent days."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._connect() as connection:
            total_runs = connection.execute(
                "SELECT COUNT(*) AS cnt FROM runs WHERE created_at >= ?", (cutoff,)
            ).fetchone()["cnt"]
            completed_runs = connection.execute(
                "SELECT COUNT(*) AS cnt FROM runs WHERE status='completed' AND created_at >= ?", (cutoff,)
            ).fetchone()["cnt"]
            token_row = connection.execute(
                """SELECT COALESCE(SUM(cm.used_tokens), 0) AS total_tokens,
                          COALESCE(SUM(cm.budget_tokens), 0) AS total_budget
                   FROM context_manifests cm
                   JOIN runs r ON r.id = cm.run_id
                   WHERE r.created_at >= ?""",
                (cutoff,),
            ).fetchone()
            model_breakdown = connection.execute(
                """SELECT json_extract(r.request_json, '$.resolved_model') AS model,
                          COUNT(*) AS runs,
                          COALESCE(SUM(cm.used_tokens), 0) AS tokens
                   FROM runs r
                   LEFT JOIN context_manifests cm ON cm.run_id = r.id
                   WHERE r.created_at >= ?
                   GROUP BY model
                   ORDER BY runs DESC""",
                (cutoff,),
            ).fetchall()
        return {
            "period_days": days,
            "total_runs": total_runs,
            "completed_runs": completed_runs,
            "total_tokens_used": token_row["total_tokens"],
            "total_budget_tokens": token_row["total_budget"],
            "models": [
                {"model": row["model"] or "unknown", "runs": row["runs"], "tokens": row["tokens"]}
                for row in model_breakdown
            ],
        }

    def list_conversations(
        self,
        *,
        workspace_id: str | None = None,
        channel: str | None = None,
        include_archived: bool = False,
        limit: int = 200,
    ) -> list[Conversation]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if workspace_id is not None:
            clauses.append("workspace_id=?")
            parameters.append(workspace_id)
        if channel is not None:
            clauses.append("channel=?")
            parameters.append(channel)
        if not include_archived:
            clauses.append("archived_at IS NULL")
        parameters.append(limit)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM conversations" + where +
                " ORDER BY CASE WHEN kind='main' THEN 0 ELSE 1 END, updated_at DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [_conversation(row) for row in rows]

    def list_messages(self, conversation_id: str, *, limit: int = 200) -> list[Message]:
        return self.recent_messages(conversation_id, limit=limit)

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
            notification_chat_id, 1, 1, next_run_at, None, now, now, None,
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
                    generation, version, next_run_at, created_at, updated_at
                ) VALUES(?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record.id, record.name, record.trigger_type, record.expression,
                 record.timezone, record.prompt, record.context_profile,
                 record.workspace_id, record.conversation_policy,
                 record.concurrency_policy, record.misfire_policy,
                 record.misfire_grace_seconds, record.notification_policy,
                 record.notification_channel, record.notification_chat_id,
                 record.generation, record.version,
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

    def set_schedule_enabled(
        self, schedule_id: str, enabled: bool, *, expected_version: int | None = None
    ) -> ScheduleRecord:
        now = utc_now()
        with self._connect() as connection:
            predicates = "id=? AND deleted_at IS NULL"
            parameters: list[Any] = [1 if enabled else 0, now, schedule_id]
            if expected_version is not None:
                predicates += " AND version=?"
                parameters.append(expected_version)
            cursor = connection.execute(
                "UPDATE schedules SET enabled=?, version=version+1, updated_at=? "
                f"WHERE {predicates}",
                parameters,
            )
            if cursor.rowcount != 1:
                if expected_version is not None:
                    try:
                        current = self.get_schedule(schedule_id)
                    except KeyError:
                        raise
                    raise VersionConflict(
                        f"schedule {schedule_id} version is {current.version}, expected {expected_version}"
                    )
                raise KeyError(f"schedule not found: {schedule_id}")
        return self.get_schedule(schedule_id)

    def update_schedule(
        self, schedule_id: str, *, name: str, trigger_type: str,
        expression: str, timezone: str, prompt: str, context_profile: str,
        workspace_id: str, conversation_policy: str, concurrency_policy: str,
        misfire_policy: str, misfire_grace_seconds: int,
        notification_policy: str, notification_channel: str | None,
        notification_chat_id: str | None, next_run_at: str | None,
        expected_version: int | None = None,
    ) -> ScheduleRecord:
        """Publish a complete schedule generation with optimistic concurrency."""
        now = utc_now()
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)).fetchone() is None:
                raise KeyError(f"workspace not found: {workspace_id}")
            current = connection.execute(
                "SELECT * FROM schedules WHERE id=? AND deleted_at IS NULL", (schedule_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"schedule not found: {schedule_id}")
            predicates = "id=? AND deleted_at IS NULL"
            parameters: list[Any] = [
                name.strip(), trigger_type, expression.strip(), timezone, prompt.strip(),
                context_profile, workspace_id, conversation_policy, concurrency_policy,
                misfire_policy, int(misfire_grace_seconds), notification_policy,
                notification_channel, notification_chat_id, next_run_at, now, schedule_id,
            ]
            if expected_version is not None:
                predicates += " AND version=?"
                parameters.append(expected_version)
            connection.execute(
                "UPDATE trigger_occurrences SET state='SKIPPED', decision_reason=?, finished_at=?, updated_at=? "
                "WHERE schedule_id=? AND state IN ('PENDING','CLAIMED')",
                ("superseded by schedule generation", now, now, schedule_id),
            )
            cursor = connection.execute(
                "UPDATE schedules SET name=?, trigger_type=?, expression=?, timezone=?, prompt=?, "
                "context_profile=?, workspace_id=?, conversation_policy=?, concurrency_policy=?, "
                "misfire_policy=?, misfire_grace_seconds=?, notification_policy=?, notification_channel=?, "
                "notification_chat_id=?, generation=generation+1, version=version+1, next_run_at=?, updated_at=? "
                f"WHERE {predicates}", parameters,
            )
            if cursor.rowcount != 1:
                current = self.get_schedule(schedule_id)
                raise VersionConflict(f"schedule {schedule_id} version is {current.version}, expected {expected_version}")
        return self.get_schedule(schedule_id)

    def delete_schedule(
        self, schedule_id: str, *, expected_version: int | None = None
    ) -> ScheduleRecord:
        now = utc_now()
        with self._connect() as connection:
            predicates = "id=? AND deleted_at IS NULL"
            parameters: list[Any] = [now, now, schedule_id]
            if expected_version is not None:
                predicates += " AND version=?"
                parameters.append(expected_version)
            cursor = connection.execute(
                "UPDATE schedules SET enabled=0, version=version+1, deleted_at=?, updated_at=? "
                f"WHERE {predicates}",
                parameters,
            )
            if cursor.rowcount != 1:
                if expected_version is not None:
                    try:
                        current = self.get_schedule(schedule_id, include_deleted=True)
                    except KeyError:
                        raise
                    raise VersionConflict(
                        f"schedule {schedule_id} version is {current.version}, expected {expected_version}"
                    )
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
                   enabled=COALESCE(?, enabled), version=version+1,
                   updated_at=? WHERE id=?""",
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

    def create_manual_trigger(
        self, schedule_id: str, request_id: str, *, scheduled_for: str | None = None,
    ) -> TriggerRecord:
        """Create an idempotent manual occurrence without advancing recurrence."""
        now = scheduled_for or utc_now()
        with self._connect() as connection:
            schedule = connection.execute(
                "SELECT id, generation FROM schedules WHERE id=? AND deleted_at IS NULL",
                (schedule_id,),
            ).fetchone()
            if schedule is None:
                raise KeyError(f"schedule not found: {schedule_id}")
            execution_key = f"{schedule_id}:{int(schedule['generation'])}:manual:{request_id}"
            trigger_id = str(uuid.uuid5(uuid.NAMESPACE_URL, execution_key))
            connection.execute(
                """INSERT OR IGNORE INTO trigger_occurrences(
                   id, execution_key, schedule_id, generation, scheduled_for,
                   state, decision_reason, attempt_count, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'PENDING', 'manual run', 0, ?, ?)""",
                (trigger_id, execution_key, schedule_id, int(schedule["generation"]), now, now, now),
            )
        return self.get_trigger(trigger_id)

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

    def list_events_global(self, *, after_id: int = 0, limit: int = 1000) -> list[PersistedEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?",
                (after_id, limit),
            ).fetchall()
        return [_event(row) for row in rows]

    def latest_event_id(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])

    def record_audit(
        self, *, actor: str, action: str, resource_type: str,
        resource_id: str | None = None, expected_version: int | None = None,
        resulting_version: int | None = None, payload: Mapping[str, Any] | None = None,
    ) -> AuditRecord:
        clean = _redact_audit(dict(payload or {}))
        created = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO control_audit(
                   actor, action, resource_type, resource_id, expected_version,
                   resulting_version, payload_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (actor, action, resource_type, resource_id, expected_version,
                 resulting_version, json.dumps(clean, ensure_ascii=False, separators=(",", ":")), created),
            )
            audit_id = int(cursor.lastrowid)
        return AuditRecord(audit_id, actor, action, resource_type, resource_id,
                           expected_version, resulting_version, clean, created)

    def list_audit(self, *, after_id: int = 0, limit: int = 200) -> list[AuditRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM control_audit WHERE id>? ORDER BY id LIMIT ?",
                (after_id, limit),
            ).fetchall()
        return [
            AuditRecord(int(row["id"]), row["actor"], row["action"],
                        row["resource_type"], row["resource_id"],
                        row["expected_version"], row["resulting_version"],
                        json.loads(row["payload_json"]), row["created_at"])
            for row in rows
        ]

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

    def list_memories(self, *, kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        query = "SELECT * FROM memories"
        parameters: list[Any] = []
        if kind:
            query += " WHERE kind=?"
            parameters.append(kind)
        query += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters).fetchall()]

    def get_memory(self, memory_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if row is None:
            raise KeyError(f"memory not found: {memory_id}")
        return dict(row)

    def update_memory(self, memory_id: str, *, content: str) -> None:
        """Update a memory record's content."""
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE memories SET content=?, updated_at=? WHERE id=?",
                (content, now, memory_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"memory not found: {memory_id}")

    def delete_memory(self, memory_id: str) -> None:
        """Delete a memory record."""
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            if cursor.rowcount == 0:
                raise KeyError(f"memory not found: {memory_id}")

    def memory_usage(self, memory_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        label = f"memory:{memory_id}"
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id, manifest_json FROM context_manifests "
                "WHERE manifest_json LIKE ? ORDER BY created_at DESC LIMIT ?",
                (f"%{label}%", limit),
            ).fetchall()
        usage: list[dict[str, Any]] = []
        for row in rows:
            try:
                manifest = json.loads(row["manifest_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            source = next((item for item in manifest.get("sources", [])
                           if item.get("label") == label), None)
            if source is not None:
                usage.append({"run_id": row["run_id"], **source})
        return usage

    def record_memory_revision(
        self,
        memory_id: str,
        *,
        previous_content: str,
        new_content: str,
        reason: str = "",
        superseded_at: str | None = None,
        source_run_id: str | None = None,
        source_conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Record supersession revision history for an updated memory."""
        now = superseded_at or utc_now()
        rev_id = str(uuid.uuid4())
        with self._connect() as connection:
            # Determine current revision count
            row = connection.execute(
                "SELECT COUNT(*) as count FROM memory_revisions WHERE memory_id=?",
                (memory_id,),
            ).fetchone()
            rev_num = (row["count"] + 1) if row else 1
            connection.execute(
                """INSERT INTO memory_revisions(
                    id, memory_id, revision, previous_content, new_content,
                    superseded_at, source_run_id, source_conversation_id, reason
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rev_id,
                    memory_id,
                    rev_num,
                    previous_content,
                    new_content,
                    now,
                    source_run_id,
                    source_conversation_id,
                    reason,
                ),
            )
        return {
            "id": rev_id,
            "memory_id": memory_id,
            "revision": rev_num,
            "previous_content": previous_content,
            "new_content": new_content,
            "superseded_at": now,
            "source_run_id": source_run_id,
            "source_conversation_id": source_conversation_id,
            "reason": reason,
        }

    def list_memory_revisions(
        self, memory_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List historical memory supersession revisions."""
        query = "SELECT * FROM memory_revisions"
        params: list[Any] = []
        if memory_id:
            query += " WHERE memory_id=?"
            params.append(memory_id)
        query += " ORDER BY superseded_at DESC, revision DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            return [dict(r) for r in connection.execute(query, params).fetchall()]

    def get_memory_curator_setting(self, key: str, default: str = "") -> str:
        """Get a memory curator setting by key."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM memory_curator_settings WHERE key=?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def set_memory_curator_setting(self, key: str, value: str) -> None:
        """Set a memory curator setting by key."""
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO memory_curator_settings(key, value, updated_at)
                   VALUES(?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, now),
            )

    def sync_identity_revision(self, name: str, sha256_value: str, content: str) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT version, sha256 FROM identity_revisions WHERE name=? "
                "ORDER BY version DESC LIMIT 1", (name,)
            ).fetchone()
            if row is None:
                version = 1
                connection.execute(
                    "INSERT INTO identity_revisions(name, version, sha256, content, created_at) "
                    "VALUES(?,?,?,?,?)", (name, version, sha256_value, content, now)
                )
            elif row["sha256"] != sha256_value:
                version = int(row["version"]) + 1
                connection.execute(
                    "INSERT INTO identity_revisions(name, version, sha256, content, created_at) "
                    "VALUES(?,?,?,?,?)", (name, version, sha256_value, content, now)
                )
            else:
                version = int(row["version"])
            return {"name": name, "version": version, "sha256": sha256_value, "updated_at": now}

    def list_identity_revisions(self, name: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT name, version, sha256, content, created_at FROM identity_revisions "
                "WHERE name=? ORDER BY version DESC LIMIT ?", (name, limit)
            ).fetchall()
        return [dict(row) for row in rows]

    def append_identity_revision(self, name: str, content: str, sha256_value: str,
                                 expected_version: int | None = None) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT version FROM identity_revisions WHERE name=? ORDER BY version DESC LIMIT 1",
                (name,),
            ).fetchone()
            current = int(row["version"]) if row else 0
            if expected_version is not None and current != expected_version:
                raise VersionConflict(f"identity {name} version is {current}, expected {expected_version}")
            version = current + 1
            connection.execute(
                "INSERT INTO identity_revisions(name, version, sha256, content, created_at) VALUES(?,?,?,?,?)",
                (name, version, sha256_value, content, now),
            )
        return {"name": name, "version": version, "sha256": sha256_value, "updated_at": now}

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

    def conversation_context_status(self, conversation_id: str, *, limit: int = 5, profile_budgets: dict[str, int] | None = None) -> dict[str, Any]:
        self.get_conversation(conversation_id)
        runs = self.list_runs(conversation_id=conversation_id)
        messages = self.list_messages(conversation_id, limit=10000)
        total_turns = len(messages)
        conversation_total_tokens = sum(math.ceil(len(m.content.encode("utf-8")) / 3) for m in messages)
        summaries = self.list_conversation_summaries(conversation_id)
        compactions_count = len(summaries)
        latest_summary = summaries[-1] if summaries else None
        compacted_count = int(latest_summary.get("message_count", 0)) if latest_summary else 0

        manifests: list[tuple[RunRecord, dict[str, Any]]] = []
        for run in runs:
            try:
                manifests.append((run, self.get_context_manifest(run.id)))
            except KeyError:
                continue
        active = next((item for item in reversed(manifests) if item[0].status in {"running", "queued"}), None)
        selected = active or (manifests[-1] if manifests else None)

        # Resolve active model for the conversation
        conv_model: str | None = None
        try:
            conv_model = self.get_conversation_model(conversation_id)
        except Exception:
            pass

        selected_model = (
            (selected[0].request.get("model") or selected[0].request.get("resolved_model"))
            if selected
            else conv_model
        ) or conv_model

        model_budget = get_model_context_limit(selected_model)
        default_budget = model_budget
        if profile_budgets:
            default_budget = profile_budgets.get("chat", model_budget)

        def _resolve_budget(manifest: dict[str, Any]) -> int:
            """Use the current profile budget if available, otherwise fall back to model limit."""
            if profile_budgets:
                profile_name = str(manifest.get("profile", "chat"))
                return profile_budgets.get(profile_name, model_budget)
            stored = int(manifest.get("budget_tokens", 0) or 0)
            if stored and stored > 128_000:
                return stored
            return model_budget

        recent: list[dict[str, Any]] = []
        for run, manifest in reversed(manifests[-limit:]):
            budget_for_run = _resolve_budget(manifest)
            used = int(manifest.get("estimated_tokens", manifest.get("used_tokens", 0)) or 0)
            recent.append({
                "run_id": run.id,
                "status": run.status,
                "state": "current" if active and run.id == active[0].id else "last",
                "used_tokens": used,
                "budget_tokens": budget_for_run,
                "percent": round((used / budget_for_run) * 100, 1) if budget_for_run else 0,
                "created_at": run.created_at,
            })
        if selected is None:
            identity_tokens = 1200
            memory_tokens = 50
            operational_tokens = 20
            conv_tokens = conversation_total_tokens
            estimated_used = identity_tokens + memory_tokens + operational_tokens + conv_tokens
            return {
                "run_id": None, "state": "none", "used_tokens": estimated_used, "budget_tokens": default_budget,
                "percent": round((estimated_used / default_budget) * 100, 1) if default_budget else 0,
                "generation_reserve": max(0, default_budget - estimated_used),
                "last_compaction": None,
                "messages_compacted": 0,
                "compactions_count": 0,
                "conversation_total_tokens": conversation_total_tokens,
                "breakdown": {
                    "identity": identity_tokens,
                    "conversation": conv_tokens,
                    "memory": memory_tokens,
                    "operational": operational_tokens,
                    "artifacts": 0,
                },
                "conversation": {
                    "total": conv_tokens,
                    "summary": 0,
                    "recent": conv_tokens,
                },
                "memory_items": [], "memory_excluded": 0, "recent": recent,
                "model": selected_model, "context_profile": "chat",
                "conversation_turns": {
                    "total": total_turns,
                    "summary_range": None,
                    "recent_range": [1, total_turns] if total_turns > 0 else None,
                    "watermark_turn": total_turns,
                },
                "watermark_turn": total_turns,
            }
        run, manifest = selected
        all_sources = [item for item in manifest.get("sources", []) if isinstance(item, dict)]
        sources = [item for item in all_sources if item.get("included")]
        breakdown = {"identity": 0, "conversation": 0, "memory": 0, "operational": 0, "artifacts": 0}
        conversation_breakdown = {"summary": 0, "recent": 0}
        memory_items: list[dict[str, Any]] = []
        memory_excluded = 0
        recent_turn_count = 0
        summary_turn_range: dict[str, int | None] = {"first": None, "last": None}
        for source in all_sources:
            tokens = int(source.get("estimated_tokens", 0) or 0)
            category = str(source.get("category", "operational"))
            included = source.get("included", False)
            if category in {"curated_memory", "retrieved_memory"}:
                if included:
                    memory_items.append({"label": source.get("label"), "tokens": tokens, "confidence": source.get("confidence"), "included": True})
                else:
                    memory_excluded += 1
            if not included:
                continue
            if category == "identity":
                bucket = "identity"
            elif category in {"history", "conversation_summary"}:
                bucket = "conversation"
                if category == "conversation_summary":
                    conversation_breakdown["summary"] += tokens
                else:
                    conversation_breakdown["recent"] += tokens
                    recent_turn_count += 1
            elif category in {"curated_memory", "retrieved_memory"}:
                bucket = "memory"
            elif category == "artifact":
                bucket = "artifacts"
            else:
                bucket = "operational"
            breakdown[bucket] += tokens

        summary_first = 1
        summary_last = compacted_count if compacted_count else None
        recent_first = (compacted_count + 1) if compacted_count else 1
        recent_last = total_turns

        # Model from the run request
        model = run.request.get("model") or run.request.get("resolved_model") or selected_model
        context_profile = str(manifest.get("profile", "chat"))

        budget = _resolve_budget(manifest)

        if breakdown["conversation"] == 0 and conversation_total_tokens > 0:
            breakdown["conversation"] = conversation_total_tokens
            conversation_breakdown["recent"] = conversation_total_tokens

        total_active_used = (
            breakdown["identity"]
            + breakdown["conversation"]
            + breakdown["memory"]
            + breakdown["operational"]
            + breakdown["artifacts"]
        )
        used = max(int(manifest.get("estimated_tokens", manifest.get("used_tokens", 0)) or 0), total_active_used)

        return {
            "run_id": run.id,
            "state": "current" if active and run.id == active[0].id else "last",
            "status": run.status,
            "model": model,
            "context_profile": context_profile,
            "used_tokens": used,
            "budget_tokens": budget,
            "percent": round((used / budget) * 100, 1) if budget else 0,
            "generation_reserve": max(0, budget - used),
            "last_compaction": latest_summary.get("created_at") if latest_summary else None,
            "messages_compacted": compacted_count,
            "compactions_count": compactions_count,
            "conversation_total_tokens": conversation_total_tokens,
            "breakdown": breakdown,
            "conversation": {
                "total": breakdown["conversation"],
                "summary": conversation_breakdown["summary"],
                "recent": conversation_breakdown["recent"],
            },
            "conversation_turns": {
                "total": total_turns,
                "summary_range": [summary_first, summary_last] if summary_last else None,
                "recent_range": [recent_first, recent_last] if recent_last >= recent_first else None,
                "watermark_turn": total_turns,
            },
            "watermark_turn": total_turns,
            "memory_items": memory_items,
            "memory_excluded": memory_excluded,
            "recent": recent,
            "manifest": manifest,
        }

    def compact_conversation(
        self,
        conversation_id: str,
        *,
        keep_recent_turns: int = 8,
    ) -> dict[str, Any]:
        """Manually trigger context compaction for a conversation."""
        self.get_conversation(conversation_id)
        messages = self.list_messages(conversation_id, limit=10000)
        if len(messages) < 2:
            raise ValueError("Conversation has too few messages to compact (minimum 2 messages required).")

        if len(messages) <= keep_recent_turns:
            older_messages = messages[:-1]
        else:
            older_messages = messages[:-keep_recent_turns]

        summaries = self.list_conversation_summaries(conversation_id)
        prior_summary = summaries[-1] if summaries else None
        next_version = (int(prior_summary["version"]) + 1) if prior_summary else 1

        from .context import _summarize_messages
        summary_proposal = _summarize_messages(older_messages, next_version, prior_summary)

        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            summary_id = str(uuid.uuid4())
            connection.execute(
                """INSERT OR REPLACE INTO conversation_summaries(
                    id, conversation_id, version, first_message_id,
                    last_message_id, message_count, content, sha256, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    summary_id,
                    conversation_id,
                    summary_proposal.version,
                    summary_proposal.first_message_id,
                    summary_proposal.last_message_id,
                    summary_proposal.message_count,
                    summary_proposal.content,
                    summary_proposal.sha256,
                    now,
                ),
            )

        status = self.conversation_context_status(conversation_id)
        return {
            "summary_id": summary_id,
            "version": summary_proposal.version,
            "message_count": summary_proposal.message_count,
            "messages_compacted_this_run": len(older_messages),
            "content": summary_proposal.content,
            "context_status": status,
        }

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

    def list_artifacts(self, run_id: str) -> list[Artifact]:
        """Return artifact metadata for a run without requiring content reads."""
        self.get_run(run_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE run_id=? ORDER BY created_at, id",
                (run_id,),
            ).fetchall()
        return [
            Artifact(
                row["id"], row["run_id"], row["conversation_id"], row["kind"],
                "", row["excerpt"], row["summary"], row["sha256"],
                int(row["characters"]), 0, row["created_at"],
            )
            for row in rows
        ]

    def get_artifact(self, artifact_id: str) -> Artifact:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"artifact not found: {artifact_id}")
        return Artifact(
            row["id"], row["run_id"], row["conversation_id"], row["kind"],
            row["content"], row["excerpt"], row["summary"], row["sha256"],
            int(row["characters"]), 0, row["created_at"],
        )

    # ──────────────────────────────────────────────────────────────────
    # Goals
    # ──────────────────────────────────────────────────────────────────

    def create_goal(
        self,
        conversation_id: str,
        objective: str,
        *,
        acceptance: list[dict[str, Any]] | None = None,
        max_turns: int = 12,
        goal_id: str | None = None,
    ) -> GoalRecord:
        if not objective.strip():
            raise ValueError("objective must not be empty")
        if max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        now = utc_now()
        record = GoalRecord(
            id=goal_id or str(uuid.uuid4()),
            conversation_id=conversation_id,
            objective=objective.strip(),
            acceptance=acceptance or [],
            status="active",
            max_turns=max_turns,
            turns_used=0,
            current_step=None,
            last_run_id=None,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone() is None:
                raise KeyError(f"conversation not found: {conversation_id}")
            # Only one active/paused goal per conversation (enforced by unique index too).
            existing = connection.execute(
                "SELECT id FROM goals WHERE conversation_id=? AND status IN ('active','paused')",
                (conversation_id,),
            ).fetchone()
            if existing is not None:
                raise ValueError(
                    f"conversation already has an active goal: {existing['id']}"
                )
            connection.execute(
                """INSERT INTO goals(
                    id, conversation_id, objective, acceptance_json, status,
                    max_turns, turns_used, current_step, last_run_id,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.id, record.conversation_id, record.objective,
                    json.dumps(record.acceptance), record.status, record.max_turns,
                    record.turns_used, record.current_step, record.last_run_id,
                    record.created_at, record.updated_at,
                ),
            )
        return record

    def get_goal(self, goal_id: str) -> GoalRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM goals WHERE id=?", (goal_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"goal not found: {goal_id}")
        return _goal(row)

    def get_active_goal(self, conversation_id: str) -> GoalRecord | None:
        """Return the active/paused goal for a conversation, or None."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM goals WHERE conversation_id=? AND status IN ('active','paused') "
                "ORDER BY created_at DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return _goal(row) if row else None

    def list_goals(
        self,
        conversation_id: str | None = None,
        *,
        statuses: tuple[str, ...] | None = None,
        limit: int = 50,
    ) -> list[GoalRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if conversation_id:
            clauses.append("conversation_id=?")
            params.append(conversation_id)
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM goals{where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [_goal(row) for row in rows]

    def update_goal(
        self,
        goal_id: str,
        *,
        status: str | None = None,
        current_step: str | None = ...,  # type: ignore[assignment]
        last_run_id: str | None = ...,  # type: ignore[assignment]
        turns_used: int | None = None,
        acceptance: list[dict[str, Any]] | None = None,
    ) -> GoalRecord:
        now = utc_now()
        sets: list[str] = ["updated_at=?"]
        params: list[Any] = [now]
        if status is not None:
            if status not in GOAL_STATUSES:
                raise ValueError(f"invalid goal status: {status}")
            sets.append("status=?")
            params.append(status)
        if current_step is not ...:
            sets.append("current_step=?")
            params.append(current_step)
        if last_run_id is not ...:
            sets.append("last_run_id=?")
            params.append(last_run_id)
        if turns_used is not None:
            sets.append("turns_used=?")
            params.append(turns_used)
        if acceptance is not None:
            sets.append("acceptance_json=?")
            params.append(json.dumps(acceptance))
        params.append(goal_id)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE goals SET {', '.join(sets)} WHERE id=?",
                params,
            )
            if cursor.rowcount == 0:
                raise KeyError(f"goal not found: {goal_id}")
            row = connection.execute(
                "SELECT * FROM goals WHERE id=?", (goal_id,)
            ).fetchone()
        return _goal(row)

    def increment_goal_turn(self, goal_id: str, run_id: str) -> GoalRecord:
        """Atomically increment turns_used and link the latest run."""
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE goals SET turns_used=turns_used+1, last_run_id=?, updated_at=? WHERE id=?",
                (run_id, now, goal_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"goal not found: {goal_id}")
            row = connection.execute(
                "SELECT * FROM goals WHERE id=?", (goal_id,)
            ).fetchone()
        return _goal(row)

    def record_goal_evaluation(
        self,
        goal_id: str,
        run_id: str | None,
        turn_number: int,
        verdict: str,
        reason: str | None = None,
        acceptance_state: list[dict[str, Any]] | None = None,
    ) -> GoalEvaluationRecord:
        if verdict not in GOAL_VERDICTS:
            raise ValueError(f"invalid verdict: {verdict}")
        now = utc_now()
        record = GoalEvaluationRecord(
            id=str(uuid.uuid4()),
            goal_id=goal_id,
            run_id=run_id,
            turn_number=turn_number,
            verdict=verdict,
            reason=reason,
            acceptance_state=acceptance_state or [],
            created_at=now,
        )
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO goal_evaluations(
                    id, goal_id, run_id, turn_number, verdict, reason,
                    acceptance_state_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.id, record.goal_id, record.run_id, record.turn_number,
                    record.verdict, record.reason, json.dumps(record.acceptance_state),
                    record.created_at,
                ),
            )
        return record

    def list_goal_evaluations(
        self, goal_id: str, *, limit: int = 50
    ) -> list[GoalEvaluationRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM goal_evaluations WHERE goal_id=? ORDER BY turn_number DESC LIMIT ?",
                (goal_id, limit),
            ).fetchall()
        return [_goal_evaluation(row) for row in rows]

    # ──────────────────────────────────────────────────────────────────
    # Learning
    # ──────────────────────────────────────────────────────────────────

    def create_learning_job(
        self,
        run_id: str,
        conversation_id: str,
        gate_score: float,
        gate_signals: list[str],
        context: dict[str, Any],
        *,
        job_id: str | None = None,
    ) -> "LearningJob":
        from .learning import LearningJob
        now = utc_now()
        record_id = job_id or str(uuid.uuid4())
        context_json = json.dumps(context)
        signals_json = json.dumps(gate_signals)
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO learning_jobs(
                    id, run_id, conversation_id, state, gate_score,
                    gate_signals_json, context_json, result_json,
                    attempts, created_at, started_at, completed_at
                ) VALUES(?, ?, ?, 'pending', ?, ?, ?, NULL, 0, ?, NULL, NULL)""",
                (record_id, run_id, conversation_id, gate_score,
                 signals_json, context_json, now),
            )
        return LearningJob(
            id=record_id,
            run_id=run_id,
            conversation_id=conversation_id,
            state="pending",
            gate_score=gate_score,
            gate_signals=tuple(gate_signals),
            context_json=context_json,
            result_json=None,
            attempts=0,
            created_at=now,
            started_at=None,
            completed_at=None,
        )

    def update_learning_job(
        self,
        job_id: str,
        *,
        state: str | None = None,
        result: dict[str, Any] | None = None,
        attempts: int | None = None,
    ) -> None:
        from .learning import LEARNING_JOB_STATES
        now = utc_now()
        sets: list[str] = []
        params: list[Any] = []
        if state is not None:
            if state not in LEARNING_JOB_STATES:
                raise ValueError(f"invalid learning job state: {state}")
            sets.append("state=?")
            params.append(state)
            if state == "running":
                sets.append("started_at=?")
                params.append(now)
            elif state in ("completed", "failed", "expired"):
                sets.append("completed_at=?")
                params.append(now)
        if result is not None:
            sets.append("result_json=?")
            params.append(json.dumps(result))
        if attempts is not None:
            sets.append("attempts=?")
            params.append(attempts)
        if not sets:
            return
        params.append(job_id)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE learning_jobs SET {', '.join(sets)} WHERE id=?",
                params,
            )
            if cursor.rowcount == 0:
                raise KeyError(f"learning job not found: {job_id}")

    def get_learning_job(self, job_id: str) -> "LearningJob":
        from .learning import LearningJob
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM learning_jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"learning job not found: {job_id}")
        return _learning_job(row)

    def list_learning_jobs(
        self,
        *,
        conversation_id: str | None = None,
        states: tuple[str, ...] | None = None,
        limit: int = 50,
    ) -> list["LearningJob"]:
        clauses: list[str] = []
        params: list[Any] = []
        if conversation_id:
            clauses.append("conversation_id=?")
            params.append(conversation_id)
        if states:
            placeholders = ",".join("?" * len(states))
            clauses.append(f"state IN ({placeholders})")
            params.extend(states)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM learning_jobs{where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [_learning_job(row) for row in rows]

    def record_learning_event(
        self,
        run_id: str,
        conversation_id: str,
        event_type: str,
        target: str,
        status: str,
        summary: str,
        reviewer_model: str,
        *,
        event_id: str | None = None,
    ) -> "LearningEvent":
        from .learning import LearningEvent
        now = utc_now()
        record_id = event_id or str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO learning_events(
                    id, run_id, conversation_id, event_type, target,
                    status, summary, reviewer_model, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record_id, run_id, conversation_id, event_type, target,
                 status, summary, reviewer_model, now),
            )
        return LearningEvent(
            id=record_id,
            run_id=run_id,
            conversation_id=conversation_id,
            event_type=event_type,
            target=target,
            status=status,
            summary=summary,
            reviewer_model=reviewer_model,
            created_at=now,
        )

    def list_learning_events(
        self,
        *,
        conversation_id: str | None = None,
        run_id: str | None = None,
        event_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list["LearningEvent"]:
        clauses: list[str] = []
        params: list[Any] = []
        if conversation_id:
            clauses.append("conversation_id=?")
            params.append(conversation_id)
        if run_id:
            clauses.append("run_id=?")
            params.append(run_id)
        if event_type:
            clauses.append("event_type=?")
            params.append(event_type)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM learning_events{where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [_learning_event(row) for row in rows]

    def get_learning_event(self, event_id: str) -> "LearningEvent":
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM learning_events WHERE id=?", (event_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"learning event not found: {event_id}")
        return _learning_event(row)

    def update_learning_event(self, event_id: str, *, status: str) -> None:
        """Update status of a learning event."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE learning_events SET status=? WHERE id=?",
                (status, event_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"learning event not found: {event_id}")

    # ──────────────────────────────────────────────────────────────────
    # Run Skill Context
    # ──────────────────────────────────────────────────────────────────

    def save_run_skill_context(self, run_id: str, context_json: str) -> None:
        """Persist the RunSkillContext for a run."""
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO run_skill_context(run_id, context_json, created_at)
                   VALUES(?, ?, ?)""",
                (run_id, context_json, now),
            )

    def get_run_skill_context(self, run_id: str) -> str | None:
        """Load the RunSkillContext JSON for a run."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT context_json FROM run_skill_context WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return row["context_json"] if row else None

    # ──────────────────────────────────────────────────────────────────
    # Context Snapshots (Phase 4C)
    # ──────────────────────────────────────────────────────────────────

    def save_context_snapshot(
        self,
        run_id: str,
        *,
        model: str,
        context_limit: int,
        input_tokens: int,
        output_tokens: int | None = None,
        token_source: str = "estimated",
        segments: Sequence[Mapping[str, Any]] | None = None,
        skills: Sequence[Mapping[str, Any]] | None = None,
        memories: Sequence[Mapping[str, Any]] | None = None,
        transformations: Sequence[Mapping[str, Any]] | None = None,
        conversation_tokens: int | None = None,
        last_invocation_tokens: int | None = None,
    ) -> None:
        """Persist a compact context snapshot for a run.

        Called once per run—either when the context is compiled (estimated)
        or when the provider returns authoritative token usage.
        """
        if token_source not in ("provider", "tokenizer", "estimated"):
            raise ValueError(f"invalid token_source: {token_source}")
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO run_context_snapshots(
                    run_id, model, context_limit, input_tokens, output_tokens,
                    token_source, segments_json, skills_json, memory_json,
                    transformations_json, conversation_tokens,
                    last_invocation_tokens, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    model,
                    context_limit,
                    input_tokens,
                    output_tokens,
                    token_source,
                    json.dumps(segments or [], ensure_ascii=False, separators=(",", ":")),
                    json.dumps(skills or [], ensure_ascii=False, separators=(",", ":")),
                    json.dumps(memories or [], ensure_ascii=False, separators=(",", ":")),
                    json.dumps(transformations, ensure_ascii=False, separators=(",", ":")) if transformations else None,
                    conversation_tokens,
                    last_invocation_tokens,
                    now,
                ),
            )

    def get_context_snapshot(self, run_id: str) -> dict[str, Any] | None:
        """Load the context snapshot for a run, returning a structured dict or None."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM run_context_snapshots WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "model": row["model"],
            "context_limit": row["context_limit"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "token_source": row["token_source"],
            "segments": json.loads(row["segments_json"]),
            "skills": json.loads(row["skills_json"]),
            "memories": json.loads(row["memory_json"]),
            "transformations": json.loads(row["transformations_json"]) if row["transformations_json"] else None,
            "conversation_tokens": row["conversation_tokens"],
            "last_invocation_tokens": row["last_invocation_tokens"],
            "created_at": row["created_at"],
        }

    def update_context_snapshot_tokens(
        self,
        run_id: str,
        *,
        input_tokens: int,
        output_tokens: int | None = None,
        token_source: str = "provider",
    ) -> bool:
        """Update token counts when provider returns authoritative usage.

        Returns True if the snapshot was updated, False if no snapshot exists.
        """
        if token_source not in ("provider", "tokenizer", "estimated"):
            raise ValueError(f"invalid token_source: {token_source}")
        with self._connect() as connection:
            result = connection.execute(
                """UPDATE run_context_snapshots
                   SET input_tokens=?, output_tokens=?, token_source=?
                   WHERE run_id=?""",
                (input_tokens, output_tokens, token_source, run_id),
            )
        return result.rowcount > 0

    # ──────────────────────────────────────────────────────────────────
    # Skills FTS Sync
    # ──────────────────────────────────────────────────────────────────

    def sync_skills_fts(self) -> None:
        """Rebuild the skills_fts index from learned_skills.

        Called after schema initialization and after bulk skill registration.
        Safe to call multiple times (delete + re-insert).
        """
        with self._connect() as connection:
            connection.execute("DELETE FROM skills_fts")
            connection.execute(
                """INSERT INTO skills_fts(name, description)
                   SELECT name, description FROM learned_skills
                   WHERE state = 'active'"""
            )

    def upsert_skill_fts(self, skill_name: str) -> None:
        """Update the FTS entry for a single skill after mutation."""
        with self._connect() as connection:
            # Remove old entry (if exists) — use name-based lookup
            connection.execute(
                "DELETE FROM skills_fts WHERE name=?", (skill_name,),
            )
            # Get the current skill state
            row = connection.execute(
                "SELECT name, description, state FROM learned_skills WHERE name=?",
                (skill_name,),
            ).fetchone()
            if row is None:
                return
            # Insert if active
            if row["state"] == "active":
                connection.execute(
                    "INSERT INTO skills_fts(name, description) VALUES(?, ?)",
                    (row["name"], row["description"]),
                )

    # ── TaskFlow & Kanban Store Methods ─────────────────────────────────

    def create_task_flow(
        self,
        title: str,
        objective: str,
        workspace_id: str,
        *,
        context_profile: str = "TASKFLOW_WORKER",
        state_json: dict[str, Any] | None = None,
        flow_id: str | None = None,
    ) -> TaskFlowRecord:
        now = utc_now()
        flow_id = flow_id or f"flow_{uuid.uuid4().hex[:12]}"
        state_str = json.dumps(state_json or {})
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO task_flows(id, title, objective, status, workspace_id, context_profile, state_json, version, created_at, updated_at) "
                "VALUES(?, ?, ?, 'QUEUED', ?, ?, ?, 1, ?, ?)",
                (flow_id, title, objective, workspace_id, context_profile, state_str, now, now),
            )
            row = connection.execute("SELECT * FROM task_flows WHERE id=?", (flow_id,)).fetchone()
            if row is None:
                raise RuntimeError(f"failed to create task flow {flow_id}")
            return _task_flow(row)

    def get_task_flow(self, flow_id: str) -> TaskFlowRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM task_flows WHERE id=?", (flow_id,)).fetchone()
            return _task_flow(row) if row is not None else None

    def list_task_flows(
        self, *, workspace_id: str | None = None, status: str | None = None
    ) -> list[TaskFlowRecord]:
        query = "SELECT * FROM task_flows WHERE 1=1"
        params: list[Any] = []
        if workspace_id:
            query += " AND workspace_id=?"
            params.append(workspace_id)
        if status:
            query += " AND status=?"
            params.append(status.upper())
        query += " ORDER BY updated_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
            return [_task_flow(r) for r in rows]

    def update_task_flow_status(
        self,
        flow_id: str,
        status: str,
        *,
        expected_version: int | None = None,
        state_json: dict[str, Any] | None = None,
    ) -> TaskFlowRecord:
        now = utc_now()
        status = status.upper().strip()
        with self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM task_flows WHERE id=?", (flow_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"task flow {flow_id} not found")
            curr_ver = int(current["version"])
            if expected_version is not None and curr_ver != expected_version:
                raise VersionConflict(f"expected version {expected_version}, got {curr_ver}")

            new_ver = curr_ver + 1
            if state_json is not None:
                connection.execute(
                    "UPDATE task_flows SET status=?, state_json=?, version=?, updated_at=? WHERE id=?",
                    (status, json.dumps(state_json), new_ver, now, flow_id),
                )
            else:
                connection.execute(
                    "UPDATE task_flows SET status=?, version=?, updated_at=? WHERE id=?",
                    (status, new_ver, now, flow_id),
                )
            row = connection.execute("SELECT * FROM task_flows WHERE id=?", (flow_id,)).fetchone()
            if row is None:
                raise RuntimeError(f"failed to update task flow {flow_id}")
            return _task_flow(row)

    def update_task_flow(
        self,
        flow_id: str,
        *,
        title: str | None = None,
        objective: str | None = None,
        context_profile: str | None = None,
        state_json: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> TaskFlowRecord:
        now = utc_now()
        with self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM task_flows WHERE id=?", (flow_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"task flow {flow_id} not found")
            curr_ver = int(current["version"])
            if expected_version is not None and curr_ver != expected_version:
                raise VersionConflict(f"expected version {expected_version}, got {curr_ver}")

            new_title = title if title is not None else current["title"]
            new_obj = objective if objective is not None else current["objective"]
            new_profile = context_profile if context_profile is not None else current["context_profile"]
            new_state = json.dumps(state_json) if state_json is not None else current["state_json"]
            new_ver = curr_ver + 1

            connection.execute(
                "UPDATE task_flows SET title=?, objective=?, context_profile=?, state_json=?, version=?, updated_at=? WHERE id=?",
                (new_title, new_obj, new_profile, new_state, new_ver, now, flow_id),
            )
            row = connection.execute("SELECT * FROM task_flows WHERE id=?", (flow_id,)).fetchone()
            if row is None:
                raise RuntimeError(f"failed to update task flow {flow_id}")
            return _task_flow(row)

    def delete_task_flow(self, flow_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM task_flows WHERE id=?", (flow_id,))
            return cursor.rowcount > 0

    # ── Flow Tasks CRUD ────────────────────────────────────────────────

    def create_flow_task(
        self,
        flow_id: str,
        title: str,
        body: str,
        workspace_id: str,
        *,
        acceptance_criteria: list[dict[str, Any]] | list[str] | None = None,
        priority: str = "MEDIUM",
        assignee_profile: str = "default",
        idempotency_key: str | None = None,
        max_attempts: int = 3,
        parent_ids: Sequence[str] | None = None,
        task_id: str | None = None,
    ) -> FlowTaskRecord:
        now = utc_now()
        task_id = task_id or f"task_{uuid.uuid4().hex[:12]}"
        acceptance_str = json.dumps(acceptance_criteria or [])
        priority = priority.upper().strip()
        status = "TODO"

        with self._connect() as connection:
            # Check idempotency within flow
            if idempotency_key:
                existing = connection.execute(
                    "SELECT * FROM flow_tasks WHERE flow_id=? AND idempotency_key=?",
                    (flow_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    parents = [
                        str(r["parent_task_id"])
                        for r in connection.execute(
                            "SELECT parent_task_id FROM task_dependencies WHERE child_task_id=?",
                            (str(existing["id"]),),
                        ).fetchall()
                    ]
                    children = [
                        str(r["child_task_id"])
                        for r in connection.execute(
                            "SELECT child_task_id FROM task_dependencies WHERE parent_task_id=?",
                            (str(existing["id"]),),
                        ).fetchall()
                    ]
                    return _flow_task(existing, parent_ids=parents, child_ids=children)

            connection.execute(
                "INSERT INTO flow_tasks(id, flow_id, title, body, acceptance_json, status, assignee_profile, priority, workspace_id, idempotency_key, max_attempts, version, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (task_id, flow_id, title, body, acceptance_str, status, assignee_profile, priority, workspace_id, idempotency_key, max_attempts, now, now),
            )

            # Insert parent dependencies
            if parent_ids:
                for p_id in parent_ids:
                    connection.execute(
                        "INSERT OR IGNORE INTO task_dependencies(parent_task_id, child_task_id) VALUES(?, ?)",
                        (p_id, task_id),
                    )

            row = connection.execute("SELECT * FROM flow_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise RuntimeError(f"failed to create flow task {task_id}")
            return _flow_task(row, parent_ids=list(parent_ids or []))

    def find_flow_task_by_idempotency(self, flow_id: str, idempotency_key: str) -> FlowTaskRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM flow_tasks WHERE flow_id=? AND idempotency_key=?",
                (flow_id, idempotency_key),
            ).fetchone()
            if row is None:
                return None
            parents = [
                str(r["parent_task_id"])
                for r in connection.execute(
                    "SELECT parent_task_id FROM task_dependencies WHERE child_task_id=?",
                    (str(row["id"]),),
                ).fetchall()
            ]
            children = [
                str(r["child_task_id"])
                for r in connection.execute(
                    "SELECT child_task_id FROM task_dependencies WHERE parent_task_id=?",
                    (str(row["id"]),),
                ).fetchall()
            ]
            return _flow_task(row, parent_ids=parents, child_ids=children)

    def get_flow_task(self, task_id: str) -> FlowTaskRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM flow_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return None
            parents = [
                str(r["parent_task_id"])
                for r in connection.execute(
                    "SELECT parent_task_id FROM task_dependencies WHERE child_task_id=?",
                    (task_id,),
                ).fetchall()
            ]
            children = [
                str(r["child_task_id"])
                for r in connection.execute(
                    "SELECT child_task_id FROM task_dependencies WHERE parent_task_id=?",
                    (task_id,),
                ).fetchall()
            ]
            return _flow_task(row, parent_ids=parents, child_ids=children)

    def list_flow_tasks(
        self, *, flow_id: str | None = None, status: str | None = None
    ) -> list[FlowTaskRecord]:
        query = "SELECT * FROM flow_tasks WHERE 1=1"
        params: list[Any] = []
        if flow_id:
            query += " AND flow_id=?"
            params.append(flow_id)
        if status:
            query += " AND status=?"
            params.append(status.upper())
        query += " ORDER BY created_at ASC"

        with self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
            tasks: list[FlowTaskRecord] = []
            for r in rows:
                t_id = str(r["id"])
                parents = [
                    str(p["parent_task_id"])
                    for p in connection.execute(
                        "SELECT parent_task_id FROM task_dependencies WHERE child_task_id=?",
                        (t_id,),
                    ).fetchall()
                ]
                children = [
                    str(c["child_task_id"])
                    for c in connection.execute(
                        "SELECT child_task_id FROM task_dependencies WHERE parent_task_id=?",
                        (t_id,),
                    ).fetchall()
                ]
                tasks.append(_flow_task(r, parent_ids=parents, child_ids=children))
            return tasks

    def update_flow_task_status(
        self,
        task_id: str,
        status: str,
        *,
        expected_version: int | None = None,
        block_reason: str | None = None,
        block_detail: str | None = None,
        increment_block_recurrence: bool = False,
    ) -> FlowTaskRecord:
        now = utc_now()
        status = status.upper().strip()
        with self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM flow_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"flow task {task_id} not found")
            curr_ver = int(current["version"])
            if expected_version is not None and curr_ver != expected_version:
                raise VersionConflict(f"expected version {expected_version}, got {curr_ver}")

            new_ver = curr_ver + 1
            curr_count = int(current["block_recurrence_count"] or 0)
            new_count = curr_count + 1 if increment_block_recurrence else (0 if status == "DONE" else curr_count)

            connection.execute(
                "UPDATE flow_tasks SET status=?, block_reason=?, block_detail=?, block_recurrence_count=?, version=?, updated_at=? WHERE id=?",
                (status, block_reason, block_detail, new_count, new_ver, now, task_id),
            )
            row = connection.execute("SELECT * FROM flow_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise RuntimeError(f"failed to update flow task {task_id}")
            parents = [
                str(r["parent_task_id"])
                for r in connection.execute(
                    "SELECT parent_task_id FROM task_dependencies WHERE child_task_id=?",
                    (task_id,),
                ).fetchall()
            ]
            children = [
                str(r["child_task_id"])
                for r in connection.execute(
                    "SELECT child_task_id FROM task_dependencies WHERE parent_task_id=?",
                    (task_id,),
                ).fetchall()
            ]
            return _flow_task(row, parent_ids=parents, child_ids=children)

    def update_flow_task(
        self,
        task_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        acceptance_criteria: list[dict[str, Any]] | list[str] | None = None,
        priority: str | None = None,
        assignee_profile: str | None = None,
        max_attempts: int | None = None,
        expected_version: int | None = None,
    ) -> FlowTaskRecord:
        now = utc_now()
        with self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM flow_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"flow task {task_id} not found")
            curr_ver = int(current["version"])
            if expected_version is not None and curr_ver != expected_version:
                raise VersionConflict(f"expected version {expected_version}, got {curr_ver}")

            new_title = title if title is not None else current["title"]
            new_body = body if body is not None else current["body"]
            new_acc = json.dumps(acceptance_criteria) if acceptance_criteria is not None else current["acceptance_json"]
            new_priority = priority.upper().strip() if priority is not None else current["priority"]
            new_assignee = assignee_profile if assignee_profile is not None else current["assignee_profile"]
            new_max = max_attempts if max_attempts is not None else current["max_attempts"]
            new_ver = curr_ver + 1

            connection.execute(
                "UPDATE flow_tasks SET title=?, body=?, acceptance_json=?, priority=?, assignee_profile=?, max_attempts=?, version=?, updated_at=? WHERE id=?",
                (new_title, new_body, new_acc, new_priority, new_assignee, new_max, new_ver, now, task_id),
            )
            row = connection.execute("SELECT * FROM flow_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise RuntimeError(f"failed to update flow task {task_id}")
            parents = [
                str(r["parent_task_id"])
                for r in connection.execute(
                    "SELECT parent_task_id FROM task_dependencies WHERE child_task_id=?",
                    (task_id,),
                ).fetchall()
            ]
            children = [
                str(r["child_task_id"])
                for r in connection.execute(
                    "SELECT child_task_id FROM task_dependencies WHERE parent_task_id=?",
                    (task_id,),
                ).fetchall()
            ]
            return _flow_task(row, parent_ids=parents, child_ids=children)

    def delete_flow_task(self, task_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM flow_tasks WHERE id=?", (task_id,))
            return cursor.rowcount > 0

    # ── Dependencies ──────────────────────────────────────────────────

    def add_task_dependency(self, parent_task_id: str, child_task_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO task_dependencies(parent_task_id, child_task_id) VALUES(?, ?)",
                (parent_task_id, child_task_id),
            )

    def remove_task_dependency(self, parent_task_id: str, child_task_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM task_dependencies WHERE parent_task_id=? AND child_task_id=?",
                (parent_task_id, child_task_id),
            )

    def get_task_dependencies(self, task_id: str) -> tuple[list[str], list[str]]:
        with self._connect() as connection:
            parents = [
                str(r["parent_task_id"])
                for r in connection.execute(
                    "SELECT parent_task_id FROM task_dependencies WHERE child_task_id=?",
                    (task_id,),
                ).fetchall()
            ]
            children = [
                str(r["child_task_id"])
                for r in connection.execute(
                    "SELECT child_task_id FROM task_dependencies WHERE parent_task_id=?",
                    (task_id,),
                ).fetchall()
            ]
            return parents, children

    def get_all_flow_dependencies(self, flow_id: str) -> list[tuple[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT d.parent_task_id, d.child_task_id FROM task_dependencies d "
                "JOIN flow_tasks t ON t.id=d.parent_task_id WHERE t.flow_id=?",
                (flow_id,),
            ).fetchall()
            return [(str(r["parent_task_id"]), str(r["child_task_id"])) for r in rows]

    # ── Comments ──────────────────────────────────────────────────────

    def add_task_comment(
        self,
        task_id: str,
        author_type: str,
        author_id: str,
        body: str,
        *,
        comment_id: str | None = None,
    ) -> TaskCommentRecord:
        now = utc_now()
        comment_id = comment_id or f"comment_{uuid.uuid4().hex[:12]}"
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO task_comments(id, task_id, author_type, author_id, body, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (comment_id, task_id, author_type, author_id, body, now),
            )
            row = connection.execute("SELECT * FROM task_comments WHERE id=?", (comment_id,)).fetchone()
            if row is None:
                raise RuntimeError(f"failed to add task comment {comment_id}")
            return _task_comment(row)

    def list_task_comments(self, task_id: str) -> list[TaskCommentRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_comments WHERE task_id=? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
            return [_task_comment(r) for r in rows]

    def delete_task_comment(self, comment_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM task_comments WHERE id=?", (comment_id,))
            return cursor.rowcount > 0

    # ── Attempts ──────────────────────────────────────────────────────

    def record_task_attempt(
        self,
        task_id: str,
        run_id: str,
        attempt_no: int,
        started_at: str,
        *,
        finished_at: str | None = None,
        outcome: str | None = None,
        summary: str | None = None,
        attempt_id: str | None = None,
    ) -> TaskAttemptRecord:
        attempt_id = attempt_id or f"attempt_{uuid.uuid4().hex[:12]}"
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO task_attempts(id, task_id, run_id, attempt_no, started_at, finished_at, outcome, summary) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, task_id, run_id, attempt_no, started_at, finished_at, outcome, summary),
            )
            row = connection.execute("SELECT * FROM task_attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None:
                raise RuntimeError(f"failed to record task attempt {attempt_id}")
            return _task_attempt(row)

    def update_task_attempt(
        self,
        attempt_id: str,
        *,
        finished_at: str | None = None,
        outcome: str | None = None,
        summary: str | None = None,
    ) -> TaskAttemptRecord | None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE task_attempts SET finished_at=?, outcome=?, summary=? WHERE id=?",
                (finished_at, outcome, summary, attempt_id),
            )
            row = connection.execute("SELECT * FROM task_attempts WHERE id=?", (attempt_id,)).fetchone()
            return _task_attempt(row) if row is not None else None

    def list_task_attempts(self, task_id: str) -> list[TaskAttemptRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_attempts WHERE task_id=? ORDER BY attempt_no ASC",
                (task_id,),
            ).fetchall()
            return [_task_attempt(r) for r in rows]

    # ── Claims & Leases ───────────────────────────────────────────────

    def claim_task(self, task_id: str, owner: str, lease_seconds: int = 300) -> bool:
        now = utc_now()
        lease_until = _add_seconds(now, lease_seconds)
        with self._connect() as connection:
            task = connection.execute("SELECT status FROM flow_tasks WHERE id=?", (task_id,)).fetchone()
            if not task or task["status"] != "READY":
                return False
            claim = connection.execute("SELECT lease_until FROM task_claims WHERE task_id=?", (task_id,)).fetchone()
            if claim and claim["lease_until"] >= now:
                return False

            connection.execute(
                "INSERT INTO task_claims(task_id, owner, lease_until, heartbeat_at) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET "
                "owner=excluded.owner, lease_until=excluded.lease_until, heartbeat_at=excluded.heartbeat_at",
                (task_id, owner, lease_until, now),
            )
            connection.execute(
                "UPDATE flow_tasks SET status='RUNNING', updated_at=? WHERE id=?",
                (now, task_id),
            )
            return True

    def claim_ready_tasks(
        self, owner: str, limit: int = 4, lease_seconds: int = 300
    ) -> list[FlowTaskRecord]:
        now = utc_now()
        lease_until = _add_seconds(now, lease_seconds)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT t.* FROM flow_tasks t "
                "JOIN task_flows f ON f.id=t.flow_id "
                "LEFT JOIN task_claims c ON c.task_id=t.id "
                "WHERE t.status='READY' AND f.status IN ('RUNNING', 'QUEUED') "
                "AND (c.task_id IS NULL OR c.lease_until < ?) "
                "ORDER BY "
                "CASE t.priority WHEN 'URGENT' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'MEDIUM' THEN 3 ELSE 4 END, "
                "t.created_at ASC LIMIT ?",
                (now, limit),
            ).fetchall()

            claimed: list[FlowTaskRecord] = []
            for r in rows:
                t_id = str(r["id"])
                connection.execute(
                    "INSERT INTO task_claims(task_id, owner, lease_until, heartbeat_at) "
                    "VALUES(?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET "
                    "owner=excluded.owner, lease_until=excluded.lease_until, heartbeat_at=excluded.heartbeat_at",
                    (t_id, owner, lease_until, now),
                )
                connection.execute(
                    "UPDATE flow_tasks SET status='RUNNING', updated_at=? WHERE id=?",
                    (now, t_id),
                )
                parents = [
                    str(p["parent_task_id"])
                    for p in connection.execute(
                        "SELECT parent_task_id FROM task_dependencies WHERE child_task_id=?",
                        (t_id,),
                    ).fetchall()
                ]
                children = [
                    str(c["child_task_id"])
                    for c in connection.execute(
                        "SELECT child_task_id FROM task_dependencies WHERE parent_task_id=?",
                        (t_id,),
                    ).fetchall()
                ]
                claimed.append(_flow_task(r, parent_ids=parents, child_ids=children))
            return claimed

    def release_task_claim(self, task_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM task_claims WHERE task_id=?", (task_id,))
            return cursor.rowcount > 0

    def heartbeat_task_claim(
        self,
        task_id: str,
        owner: str,
        *,
        message: str | None = None,
        extend_seconds: int = 300,
    ) -> bool:
        now = utc_now()
        lease_until = _add_seconds(now, extend_seconds)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE task_claims SET lease_until=?, heartbeat_at=?, heartbeat_message=? "
                "WHERE task_id=? AND owner=?",
                (lease_until, now, message, task_id, owner),
            )
            return cursor.rowcount > 0

    def get_task_claim(self, task_id: str) -> TaskClaimRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM task_claims WHERE task_id=?", (task_id,)).fetchone()
            return _task_claim(row) if row is not None else None

    def list_expired_task_claims(self, now: str | None = None) -> list[TaskClaimRecord]:
        now_ts = now or utc_now()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_claims WHERE lease_until < ?", (now_ts,)
            ).fetchall()
            return [_task_claim(r) for r in rows]

    # ── Task Artifact Links ───────────────────────────────────────────

    def link_task_artifact(self, task_id: str, artifact_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO task_artifacts(task_id, artifact_id) VALUES(?, ?)",
                (task_id, artifact_id),
            )

    def list_task_artifacts(self, task_id: str) -> list[Artifact]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT a.* FROM artifacts a JOIN task_artifacts ta ON ta.artifact_id=a.id WHERE ta.task_id=?",
                (task_id,),
            ).fetchall()
            return [_artifact(r) for r in rows]

    # ── Progress Engine & Telemetry Persistence ────────────────────────

    def save_progress_snapshot(self, snapshot_dict: dict[str, Any]) -> None:
        run_id = snapshot_dict["run_id"]
        status = snapshot_dict.get("status", "running")
        current_op = snapshot_dict.get("active_operation_id")
        current_label = snapshot_dict.get("current_label")
        current_detail = snapshot_dict.get("current_detail")
        started_at = snapshot_dict.get("started_at", "")
        last_activity = snapshot_dict.get("last_activity_at", "")
        last_output = snapshot_dict.get("last_output_at")
        last_progress = snapshot_dict.get("last_progress_at")
        version = int(snapshot_dict.get("version", 1))
        snapshot_json = json.dumps(snapshot_dict)
        now_str = datetime.now(UTC).isoformat()

        with self._connect() as connection:
            connection.execute(
                """INSERT INTO run_progress(
                    run_id, status, current_operation_id, current_label, current_detail,
                    started_at, last_activity_at, last_output_at, last_progress_at,
                    snapshot_json, version, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status,
                    current_operation_id=excluded.current_operation_id,
                    current_label=excluded.current_label,
                    current_detail=excluded.current_detail,
                    last_activity_at=excluded.last_activity_at,
                    last_output_at=excluded.last_output_at,
                    last_progress_at=excluded.last_progress_at,
                    snapshot_json=excluded.snapshot_json,
                    version=excluded.version,
                    updated_at=excluded.updated_at
                """,
                (
                    run_id, status, current_op, current_label, current_detail,
                    started_at, last_activity, last_output, last_progress,
                    snapshot_json, version, now_str,
                ),
            )

    def get_progress_snapshot(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM run_progress WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["snapshot_json"])

    def append_telemetry_event(
        self,
        *,
        run_id: str,
        event_id: str,
        event_type: str,
        source: str,
        operation_id: str | None = None,
        parent_operation_id: str | None = None,
        sequence: int = 0,
        tool: str | None = None,
        data: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> int:
        now_str = created_at or datetime.now(UTC).isoformat()
        data_json = json.dumps(data or {})
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO run_telemetry_events(
                    run_id, event_id, operation_id, parent_operation_id, sequence,
                    event_type, source, tool, created_at, data_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, event_id, operation_id, parent_operation_id, sequence,
                    event_type, source, tool, now_str, data_json,
                ),
            )
            return cursor.lastrowid

    def list_telemetry_events(
        self, run_id: str, since_sequence: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM run_telemetry_events
                   WHERE run_id=? AND sequence > ?
                   ORDER BY sequence ASC LIMIT ?""",
                (run_id, since_sequence, limit),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "run_id": r["run_id"],
                "event_id": r["event_id"],
                "operation_id": r["operation_id"],
                "parent_operation_id": r["parent_operation_id"],
                "sequence": r["sequence"],
                "type": r["event_type"],
                "source": r["source"],
                "tool": r["tool"],
                "timestamp": r["created_at"],
                "data": json.loads(r["data_json"]),
            }
            for r in rows
        ]


def _conversation(row: sqlite3.Row) -> Conversation:
    keys = set(row.keys())
    return Conversation(
        row["id"],
        row["workspace_id"],
        row["channel"],
        row["channel_key"],
        row["title"],
        row["agy_conversation_id"],
        row["model_override"],
        row["effort_override"],
        row["created_at"],
        row["updated_at"],
        row["kind"] if "kind" in keys else "normal",
        row["archived_at"] if "archived_at" in keys else None,
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
        generation=int(row["generation"]), version=int(row["version"]),
        next_run_at=row["next_run_at"],
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


def _goal(row: sqlite3.Row) -> GoalRecord:
    return GoalRecord(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        objective=str(row["objective"]),
        acceptance=json.loads(row["acceptance_json"]),
        status=str(row["status"]),
        max_turns=int(row["max_turns"]),
        turns_used=int(row["turns_used"]),
        current_step=row["current_step"],
        last_run_id=row["last_run_id"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _goal_evaluation(row: sqlite3.Row) -> GoalEvaluationRecord:
    return GoalEvaluationRecord(
        id=str(row["id"]),
        goal_id=str(row["goal_id"]),
        run_id=row["run_id"],
        turn_number=int(row["turn_number"]),
        verdict=str(row["verdict"]),
        reason=row["reason"],
        acceptance_state=json.loads(row["acceptance_state_json"]),
        created_at=str(row["created_at"]),
    )


def _learning_job(row: sqlite3.Row) -> "LearningJob":
    from .learning import LearningJob
    return LearningJob(
        id=str(row["id"]),
        run_id=str(row["run_id"]),
        conversation_id=str(row["conversation_id"]),
        state=str(row["state"]),
        gate_score=float(row["gate_score"]),
        gate_signals=tuple(json.loads(row["gate_signals_json"])),
        context_json=str(row["context_json"]),
        result_json=row["result_json"],
        attempts=int(row["attempts"]),
        created_at=str(row["created_at"]),
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def _learning_event(row: sqlite3.Row) -> "LearningEvent":
    from .learning import LearningEvent
    return LearningEvent(
        id=str(row["id"]),
        run_id=str(row["run_id"]),
        conversation_id=str(row["conversation_id"]),
        event_type=str(row["event_type"]),
        target=str(row["target"]),
        status=str(row["status"]),
        summary=str(row["summary"]),
        reviewer_model=str(row["reviewer_model"]),
        created_at=str(row["created_at"]),
    )


def _task_flow(row: sqlite3.Row) -> TaskFlowRecord:
    return TaskFlowRecord(
        id=str(row["id"]),
        title=str(row["title"]),
        objective=str(row["objective"]),
        status=str(row["status"]),
        workspace_id=str(row["workspace_id"]),
        context_profile=str(row["context_profile"]),
        state_json=json.loads(row["state_json"]) if row["state_json"] else {},
        version=int(row["version"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _flow_task(
    row: sqlite3.Row,
    parent_ids: list[str] | None = None,
    child_ids: list[str] | None = None,
) -> FlowTaskRecord:
    acc = json.loads(row["acceptance_json"]) if row["acceptance_json"] else []
    return FlowTaskRecord(
        id=str(row["id"]),
        flow_id=str(row["flow_id"]),
        title=str(row["title"]),
        body=str(row["body"]),
        acceptance_json=acc,
        status=str(row["status"]),
        assignee_profile=str(row["assignee_profile"]),
        priority=str(row["priority"]),
        workspace_id=str(row["workspace_id"]),
        idempotency_key=row["idempotency_key"],
        max_attempts=int(row["max_attempts"]),
        block_reason=row["block_reason"],
        block_detail=row["block_detail"],
        block_recurrence_count=int(row["block_recurrence_count"] or 0),
        version=int(row["version"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        parent_ids=parent_ids or [],
        child_ids=child_ids or [],
    )


def _task_attempt(row: sqlite3.Row) -> TaskAttemptRecord:
    return TaskAttemptRecord(
        id=str(row["id"]),
        task_id=str(row["task_id"]),
        run_id=str(row["run_id"]),
        attempt_no=int(row["attempt_no"]),
        started_at=str(row["started_at"]),
        finished_at=row["finished_at"],
        outcome=row["outcome"],
        summary=row["summary"],
    )


def _task_comment(row: sqlite3.Row) -> TaskCommentRecord:
    return TaskCommentRecord(
        id=str(row["id"]),
        task_id=str(row["task_id"]),
        author_type=str(row["author_type"]),
        author_id=str(row["author_id"]),
        body=str(row["body"]),
        created_at=str(row["created_at"]),
    )


def _task_claim(row: sqlite3.Row) -> TaskClaimRecord:
    return TaskClaimRecord(
        task_id=str(row["task_id"]),
        owner=str(row["owner"]),
        lease_until=str(row["lease_until"]),
        heartbeat_at=str(row["heartbeat_at"]),
        heartbeat_message=row["heartbeat_message"],
    )


def _add_seconds(value: str, seconds: int) -> str:
    return (datetime.fromisoformat(value).astimezone(UTC) +
            timedelta(seconds=seconds)).isoformat()


def _redact_audit(value: Any, *, key: str = "") -> Any:
    """Keep control-plane audit useful without allowing secret material in it."""
    lowered = key.lower()
    if any(marker in lowered for marker in ("token", "secret", "password", "credential", "api_key")):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(k): _redact_audit(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_audit(item, key=key) for item in value]
    return value


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
    model_override TEXT,
    effort_override TEXT,
    kind TEXT NOT NULL DEFAULT 'normal' CHECK(kind IN ('main', 'normal')),
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(channel, channel_key)
);
CREATE INDEX IF NOT EXISTS conversations_archived_updated
    ON conversations(archived_at, updated_at DESC);

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

CREATE TABLE IF NOT EXISTS memory_revisions (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL DEFAULT 1,
    previous_content TEXT NOT NULL,
    new_content TEXT NOT NULL,
    superseded_at TEXT NOT NULL,
    source_run_id TEXT,
    source_conversation_id TEXT,
    reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS memory_curator_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
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
    version INTEGER NOT NULL DEFAULT 1,
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

_CAPABILITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL CHECK(scope IN ('global','workspace')),
    workspace_id TEXT REFERENCES workspaces(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    profiles_json TEXT NOT NULL DEFAULT '[]',
    sha256 TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT 'unversioned',
    validation_state TEXT NOT NULL CHECK(validation_state IN ('VALID','INVALID','MISSING')),
    validation_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((scope='global' AND workspace_id IS NULL) OR (scope='workspace' AND workspace_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS skills_scope ON skills(scope, workspace_id, enabled);

CREATE TABLE IF NOT EXISTS mcp_servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    transport TEXT NOT NULL CHECK(transport IN ('stdio','sse','http')),
    command TEXT,
    url TEXT,
    args_json TEXT NOT NULL DEFAULT '[]',
    env_refs_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    scope TEXT NOT NULL CHECK(scope IN ('global','workspace')),
    workspace_id TEXT REFERENCES workspaces(id) ON DELETE CASCADE,
    config_hash TEXT NOT NULL,
    health_state TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK(health_state IN ('UNKNOWN','HEALTHY','DEGRADED','UNAVAILABLE','MISCONFIGURED')),
    health_error TEXT,
    last_checked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((transport='stdio' AND command IS NOT NULL AND url IS NULL) OR
          (transport IN ('sse','http') AND url IS NOT NULL AND command IS NULL)),
    CHECK((scope='global' AND workspace_id IS NULL) OR (scope='workspace' AND workspace_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS mcp_scope ON mcp_servers(scope, workspace_id, enabled);

CREATE TABLE IF NOT EXISTS capability_bindings (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    capability_type TEXT NOT NULL CHECK(capability_type IN ('skill','mcp')),
    capability_id TEXT NOT NULL,
    profile TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workspace_id, capability_type, capability_id, profile)
);
CREATE INDEX IF NOT EXISTS capability_binding_lookup
    ON capability_bindings(workspace_id, capability_type, profile, enabled);

CREATE TABLE IF NOT EXISTS capability_manifests (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    profile TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    snapshot_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS capability_manifest_workspace
    ON capability_manifests(workspace_id, created_at);
"""

_CONTROL_SCHEMA = """
CREATE TABLE IF NOT EXISTS control_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    expected_version INTEGER,
    resulting_version INTEGER,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS control_audit_created ON control_audit(created_at, id);
CREATE INDEX IF NOT EXISTS control_audit_resource
    ON control_audit(resource_type, resource_id, id);
"""

_IDENTITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS identity_revisions (
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(name, version)
);
CREATE INDEX IF NOT EXISTS identity_revisions_latest
    ON identity_revisions(name, version DESC);
"""

_GOAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    objective TEXT NOT NULL,
    acceptance_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK(status IN ('active','paused','completed','cancelled','failed')),
    max_turns INTEGER NOT NULL DEFAULT 12,
    turns_used INTEGER NOT NULL DEFAULT 0,
    current_step TEXT,
    last_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS goals_conversation ON goals(conversation_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_goal_per_conversation
    ON goals(conversation_id) WHERE status IN ('active','paused');

CREATE TABLE IF NOT EXISTS goal_evaluations (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
    turn_number INTEGER NOT NULL,
    verdict TEXT NOT NULL CHECK(verdict IN ('continue','done','failed','paused')),
    reason TEXT,
    acceptance_state_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS goal_evaluations_goal ON goal_evaluations(goal_id, turn_number);
"""

_LEARNING_SCHEMA = """
CREATE TABLE IF NOT EXISTS learning_jobs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN ('pending','running','completed','failed','expired')),
    gate_score REAL NOT NULL,
    gate_signals_json TEXT NOT NULL DEFAULT '[]',
    context_json TEXT NOT NULL,
    result_json TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS learning_jobs_state ON learning_jobs(state, created_at);
CREATE INDEX IF NOT EXISTS learning_jobs_run ON learning_jobs(run_id);
CREATE INDEX IF NOT EXISTS learning_jobs_conversation ON learning_jobs(conversation_id);

CREATE TABLE IF NOT EXISTS learning_events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'memory_upsert','memory_remove','skill_candidate','review_skipped'
    )),
    target TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('applied','pending_approval','rejected','failed')),
    summary TEXT NOT NULL DEFAULT '',
    reviewer_model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS learning_events_run ON learning_events(run_id);
CREATE INDEX IF NOT EXISTS learning_events_conversation ON learning_events(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS learning_events_type ON learning_events(event_type, status);
"""

_SKILLS_SCHEMA = """
CREATE TABLE IF NOT EXISTS learned_skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL,
    owner TEXT NOT NULL CHECK(owner IN ('user','agent','bundled')),
    state TEXT NOT NULL CHECK(state IN ('active','stale','archived')),
    trust TEXT NOT NULL CHECK(trust IN ('unreviewed','approved')),
    revision INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS learned_skills_name ON learned_skills(name);
CREATE INDEX IF NOT EXISTS learned_skills_state ON learned_skills(state);
CREATE INDEX IF NOT EXISTS learned_skills_owner ON learned_skills(owner);

CREATE TABLE IF NOT EXISTS skill_proposals (
    id TEXT PRIMARY KEY,
    skill_id TEXT REFERENCES learned_skills(id) ON DELETE SET NULL,
    skill_name TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('create','patch','archive')),
    description TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.0,
    content TEXT NOT NULL DEFAULT '',
    before TEXT,
    base_revision INTEGER,
    source_run_id TEXT,
    review_model TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','expired','conflict')),
    status_reason TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS skill_proposals_status ON skill_proposals(status);
CREATE INDEX IF NOT EXISTS skill_proposals_skill ON skill_proposals(skill_id);
CREATE INDEX IF NOT EXISTS skill_proposals_name ON skill_proposals(skill_name);

CREATE TABLE IF NOT EXISTS skill_revisions (
    id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL REFERENCES learned_skills(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    parent_revision INTEGER,
    operation TEXT NOT NULL CHECK(operation IN ('create','patch','rollback','archive')),
    source_run_id TEXT,
    proposal_id TEXT REFERENCES skill_proposals(id) ON DELETE SET NULL,
    model TEXT,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS skill_revisions_skill ON skill_revisions(skill_id, revision);

CREATE TABLE IF NOT EXISTS skill_usage (
    id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL REFERENCES learned_skills(id) ON DELETE CASCADE,
    run_id TEXT,
    event TEXT NOT NULL CHECK(event IN (
        'discovered','selected','loaded','executed',
        'successful','failed','corrected','proposal_generated',
        'matched','presented'
    )),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS skill_usage_skill ON skill_usage(skill_id, event);
CREATE INDEX IF NOT EXISTS skill_usage_run ON skill_usage(run_id);

CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
    name, description
);

CREATE TABLE IF NOT EXISTS run_skill_context (
    run_id TEXT PRIMARY KEY,
    context_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

_CONTEXT_SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_context_snapshots (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    context_limit INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER,
    token_source TEXT NOT NULL CHECK(token_source IN ('provider', 'tokenizer', 'estimated')),
    segments_json TEXT NOT NULL DEFAULT '[]',
    skills_json TEXT NOT NULL DEFAULT '[]',
    memory_json TEXT NOT NULL DEFAULT '[]',
    transformations_json TEXT,
    conversation_tokens INTEGER,
    last_invocation_tokens INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS run_context_snapshots_created
    ON run_context_snapshots(created_at);
"""


_ATTACHMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
    filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('image', 'audio', 'video', 'document', 'archive', 'other')),
    size_bytes INTEGER NOT NULL,
    storage_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('web', 'telegram', 'api', 'agent')),
    width INTEGER,
    height INTEGER,
    duration_ms INTEGER,
    metadata_json TEXT,
    state TEXT NOT NULL DEFAULT 'ready' CHECK(state IN ('queued', 'ready', 'failed')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS attachments_message
    ON attachments(message_id) WHERE message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS attachments_conversation
    ON attachments(conversation_id);
CREATE INDEX IF NOT EXISTS attachments_workspace
    ON attachments(workspace_id);
"""


_TASKFLOW_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_flows (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('QUEUED', 'RUNNING', 'WAITING', 'BLOCKED', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    context_profile TEXT NOT NULL DEFAULT 'TASKFLOW_WORKER',
    state_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_flows_status ON task_flows(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS task_flows_workspace ON task_flows(workspace_id);

CREATE TABLE IF NOT EXISTS flow_tasks (
    id TEXT PRIMARY KEY,
    flow_id TEXT NOT NULL REFERENCES task_flows(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    acceptance_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK(status IN ('TRIAGE', 'TODO', 'READY', 'RUNNING', 'BLOCKED', 'DONE', 'FAILED', 'CANCELLED', 'ARCHIVED')),
    assignee_profile TEXT NOT NULL DEFAULT 'default',
    priority TEXT NOT NULL DEFAULT 'MEDIUM' CHECK(priority IN ('LOW', 'MEDIUM', 'HIGH', 'URGENT')),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    idempotency_key TEXT,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    block_reason TEXT CHECK(block_reason IS NULL OR block_reason IN ('dependency', 'needs_user_input', 'missing_capability', 'transient_failure', 'external_service', 'review_required')),
    block_detail TEXT,
    block_recurrence_count INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS flow_tasks_flow_status ON flow_tasks(flow_id, status);
CREATE INDEX IF NOT EXISTS flow_tasks_status ON flow_tasks(status);
CREATE UNIQUE INDEX IF NOT EXISTS flow_tasks_flow_idempotency ON flow_tasks(flow_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS task_dependencies (
    parent_task_id TEXT NOT NULL REFERENCES flow_tasks(id) ON DELETE CASCADE,
    child_task_id TEXT NOT NULL REFERENCES flow_tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (parent_task_id, child_task_id)
);
CREATE INDEX IF NOT EXISTS task_dependencies_child ON task_dependencies(child_task_id);

CREATE TABLE IF NOT EXISTS task_attempts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES flow_tasks(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT CHECK(outcome IS NULL OR outcome IN ('COMPLETED', 'FAILED', 'CANCELLED', 'INTERRUPTED', 'BLOCKED')),
    summary TEXT
);
CREATE INDEX IF NOT EXISTS task_attempts_task ON task_attempts(task_id, attempt_no);
CREATE INDEX IF NOT EXISTS task_attempts_run ON task_attempts(run_id);

CREATE TABLE IF NOT EXISTS task_comments (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES flow_tasks(id) ON DELETE CASCADE,
    author_type TEXT NOT NULL CHECK(author_type IN ('user', 'agent', 'system')),
    author_id TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_comments_task ON task_comments(task_id, created_at);

CREATE TABLE IF NOT EXISTS task_claims (
    task_id TEXT PRIMARY KEY REFERENCES flow_tasks(id) ON DELETE CASCADE,
    owner TEXT NOT NULL,
    lease_until TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    heartbeat_message TEXT
);
CREATE INDEX IF NOT EXISTS task_claims_lease ON task_claims(lease_until);

CREATE TABLE IF NOT EXISTS task_artifacts (
    task_id TEXT NOT NULL REFERENCES flow_tasks(id) ON DELETE CASCADE,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, artifact_id)
);
"""

_TELEMETRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_telemetry_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    operation_id TEXT,
    parent_operation_id TEXT,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    tool TEXT,
    created_at TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_run_seq ON run_telemetry_events (run_id, sequence);

CREATE TABLE IF NOT EXISTS run_progress (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    current_operation_id TEXT,
    current_label TEXT,
    current_detail TEXT,
    started_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    last_output_at TEXT,
    last_progress_at TEXT,
    snapshot_json TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""
