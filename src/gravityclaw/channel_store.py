"""Transactional inbox, routing, cancellation, presentation, and outbox state."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .channels import InboundMessage
from .store import RunRecord, Store, utc_now


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    inbox_id: str
    duplicate: bool
    result_kind: str
    conversation_id: str | None
    run_id: str | None
    outbox_id: str | None


@dataclass(frozen=True, slots=True)
class ChannelBinding:
    id: str
    channel: str
    sender_id: str
    chat_id: str
    thread_key: str
    workspace_id: str
    conversation_id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    id: str
    channel: str
    logical_key: str
    kind: str
    run_id: str | None
    chat_id: str
    thread_key: str
    provider_message_id: str | None
    desired_text: str
    delivered_text: str | None
    status: str
    event_sequence: int
    delivery_version: int
    attempt_count: int
    available_at: str
    lease_until: str | None
    last_error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CancellationRequest:
    id: str
    run_id: str
    source: str
    status: str
    attempt_count: int
    error: str | None
    created_at: str
    updated_at: str


class ChannelStore:
    def __init__(self, store: Store) -> None:
        self.store = store

    def set_workspace_alias(self, alias: str, workspace_id: str) -> None:
        normalized = _alias(alias)
        self.store.get_workspace(workspace_id)
        now = utc_now()
        with self.store._connect() as connection:
            connection.execute(
                """INSERT INTO workspace_aliases(alias, workspace_id, created_at, updated_at)
                   VALUES(?, ?, ?, ?)
                   ON CONFLICT(alias) DO UPDATE SET
                       workspace_id=excluded.workspace_id,
                       updated_at=excluded.updated_at""",
                (normalized, workspace_id, now, now),
            )

    def enqueue_scheduled_notification(
        self, *, channel: str, chat_id: str, text: str, logical_key: str,
        thread_key: str = "",
    ) -> str:
        """Create a normal durable MESSAGE outbox row for proactive output."""
        if not text.strip():
            raise ValueError("scheduled notification text must not be empty")
        now = utc_now()
        outbox_id = str(uuid.uuid4())
        with self.store._connect() as connection:
            connection.execute(
                """INSERT INTO channel_outbox(
                    id, channel, logical_key, kind, chat_id, thread_key,
                    desired_text, status, available_at, created_at, updated_at
                ) VALUES(?, ?, ?, 'MESSAGE', ?, ?, ?, 'PENDING', ?, ?, ?)
                ON CONFLICT(channel, logical_key) DO NOTHING""",
                (outbox_id, channel, logical_key, chat_id, thread_key,
                 text.strip(), now, now, now),
            )
            row = connection.execute(
                "SELECT id FROM channel_outbox WHERE channel=? AND logical_key=?",
                (channel, logical_key),
            ).fetchone()
        return str(row["id"])

    def list_workspace_aliases(self) -> list[dict[str, str]]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT alias, workspace_id FROM workspace_aliases ORDER BY alias"
            ).fetchall()
        return [dict(row) for row in rows]

    def cursor(self, channel: str) -> int:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT last_update_id FROM channel_cursors WHERE channel=?", (channel,)
            ).fetchone()
        return int(row["last_update_id"]) if row is not None else -1

    def advance_cursor(self, channel: str, update_id: int) -> None:
        with self.store._connect() as connection:
            self._advance_cursor_tx(connection, channel, update_id)

    def binding(
        self, channel: str, sender_id: str, chat_id: str, thread_key: str = ""
    ) -> ChannelBinding | None:
        with self.store._connect() as connection:
            row = connection.execute(
                """SELECT * FROM channel_bindings
                   WHERE channel=? AND sender_id=? AND chat_id=? AND thread_key=?""",
                (channel, sender_id, chat_id, thread_key),
            ).fetchone()
        return _binding(row) if row is not None else None

    def ingest(
        self, message: InboundMessage, *, default_workspace_alias: str | None = None
    ) -> IngestOutcome:
        text = message.text.strip()
        if not text:
            raise ValueError("inbound message text must not be empty")
        now = utc_now()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT * FROM channel_inbox WHERE channel=? AND provider_update_id=?",
                (message.channel, message.provider_update_id),
            ).fetchone()
            if duplicate is not None:
                self._advance_cursor_tx(
                    connection, message.channel, message.provider_update_id
                )
                return _outcome(duplicate, duplicate=True)

            inbox_id = str(uuid.uuid4())
            command, argument = _command(text)
            binding = self._binding_tx(connection, message)
            if binding is None and default_workspace_alias and command != "/workspace":
                binding = self._switch_workspace_tx(
                    connection, message, default_workspace_alias, now
                )

            conversation_id: str | None = binding.conversation_id if binding else None
            run_id: str | None = None
            outbox_id: str | None = None
            result_kind = "message"

            if command == "/workspace":
                result_kind = "command"
                if not argument:
                    response = self._workspace_help_tx(connection)
                else:
                    binding = self._switch_workspace_tx(
                        connection, message, argument, now
                    )
                    if binding is None:
                        response = f"Unknown workspace: {argument}\n" + self._workspace_help_tx(
                            connection
                        )
                        conversation_id = None
                    else:
                        conversation_id = binding.conversation_id
                        response = f"Workspace selected: {_alias(argument)}"
                outbox_id = self._message_outbox_tx(
                    connection, message, inbox_id, response, now
                )
            elif command == "/new":
                result_kind = "command"
                if binding is None:
                    response = self._workspace_help_tx(connection)
                else:
                    binding = self._new_conversation_tx(connection, message, binding, now)
                    conversation_id = binding.conversation_id
                    response = "Started a new conversation."
                outbox_id = self._message_outbox_tx(
                    connection, message, inbox_id, response, now
                )
            elif command == "/status":
                result_kind = "command"
                response = self._status_text_tx(connection, binding)
                outbox_id = self._message_outbox_tx(
                    connection, message, inbox_id, response, now
                )
            elif command == "/stop":
                result_kind = "cancellation"
                target = self._active_run_tx(connection, binding)
                if target is None:
                    response = "No queued or running task to stop."
                else:
                    run_id = target.id
                    request_id = str(uuid.uuid4())
                    connection.execute(
                        """INSERT INTO cancellation_requests(
                            id, run_id, source, status, created_at, updated_at
                        ) VALUES(?, ?, ?, 'PENDING', ?, ?)
                        ON CONFLICT(run_id) DO NOTHING""",
                        (request_id, run_id, f"{message.channel}:{inbox_id}", now, now),
                    )
                    response = f"Cancellation requested for run {run_id[:8]}."
                outbox_id = self._message_outbox_tx(
                    connection, message, inbox_id, response, now
                )
            elif command is not None:
                result_kind = "command"
                outbox_id = self._message_outbox_tx(
                    connection,
                    message,
                    inbox_id,
                    "Unknown command. Use /new, /status, /stop, or /workspace.",
                    now,
                )
            elif binding is None:
                result_kind = "routing_error"
                outbox_id = self._message_outbox_tx(
                    connection,
                    message,
                    inbox_id,
                    self._workspace_help_tx(connection),
                    now,
                )
            else:
                conversation_id = binding.conversation_id
                run_id = self._submit_run_tx(
                    connection, conversation_id, text, inbox_id, now
                )
                outbox_id = self._presentation_outbox_tx(
                    connection, message, run_id, now
                )
                result_kind = "run"

            connection.execute(
                """INSERT INTO channel_inbox(
                    id, channel, provider_update_id, sender_id, chat_id, thread_key,
                    provider_message_id, text, command, conversation_id, run_id,
                    outbox_id, result_kind, payload_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    inbox_id,
                    message.channel,
                    message.provider_update_id,
                    message.sender_id,
                    message.chat_id,
                    message.thread_key,
                    message.provider_message_id,
                    text,
                    command,
                    conversation_id,
                    run_id,
                    outbox_id,
                    result_kind,
                    json.dumps(message.payload, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
            self._advance_cursor_tx(connection, message.channel, message.provider_update_id)
        return IngestOutcome(
            inbox_id, False, result_kind, conversation_id, run_id, outbox_id
        )

    def claim_cancellations(self, limit: int = 20) -> list[CancellationRequest]:
        now = utc_now()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT * FROM cancellation_requests WHERE status='PENDING'
                   ORDER BY created_at LIMIT ?""",
                (limit,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """UPDATE cancellation_requests SET status='PROCESSING',
                       attempt_count=attempt_count+1, updated_at=? """
                    "WHERE id=? AND status='PENDING'",
                    (now, row["id"]),
                )
        return [
            CancellationRequest(
                **dict(row)
                | {
                    "status": "PROCESSING",
                    "attempt_count": int(row["attempt_count"]) + 1,
                }
            )
            for row in rows
        ]

    def recover_cancellations(self) -> None:
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE cancellation_requests SET status='PENDING', updated_at=? "
                "WHERE status='PROCESSING'",
                (utc_now(),),
            )

    def finish_cancellation(self, request_id: str, error: str | None = None) -> None:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT attempt_count FROM cancellation_requests WHERE id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"cancellation request not found: {request_id}")
            if error is None:
                status = "COMPLETED"
            else:
                status = "FAILED" if int(row["attempt_count"]) >= 8 else "PENDING"
            connection.execute(
                """UPDATE cancellation_requests SET status=?, error=?, updated_at=?
                   WHERE id=?""",
                (status, error, utc_now(), request_id),
            )

    def recover_deliveries(self) -> dict[str, int]:
        now = utc_now()
        with self.store._connect() as connection:
            uncertain = connection.execute(
                """UPDATE channel_outbox SET status='UNCERTAIN', lease_until=NULL,
                   last_error='gateway restarted during initial send', updated_at=?
                   WHERE status='SENDING' AND provider_message_id IS NULL""",
                (now,),
            ).rowcount
            retry = connection.execute(
                """UPDATE channel_outbox SET status='PENDING', lease_until=NULL,
                   available_at=?, updated_at=?
                   WHERE status='SENDING' AND provider_message_id IS NOT NULL""",
                (now, now),
            ).rowcount
        return {"uncertain": uncertain, "retry": retry}

    def claim_outbox(
        self, channel: str, limit: int = 20, lease_seconds: float = 30
    ) -> list[OutboxRecord]:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        lease = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE channel_outbox SET status='UNCERTAIN', lease_until=NULL,
                   last_error='delivery lease expired during initial send', updated_at=?
                   WHERE status='SENDING' AND provider_message_id IS NULL
                     AND lease_until IS NOT NULL AND lease_until<=?""",
                (now, now),
            )
            connection.execute(
                """UPDATE channel_outbox SET status='PENDING', lease_until=NULL,
                   available_at=?, updated_at=?
                   WHERE status='SENDING' AND provider_message_id IS NOT NULL
                     AND lease_until IS NOT NULL AND lease_until<=?""",
                (now, now, now),
            )
            rows = connection.execute(
                """SELECT * FROM channel_outbox
                   WHERE channel=? AND status IN ('PENDING', 'RETRY_WAIT')
                     AND available_at<=?
                   ORDER BY created_at LIMIT ?""",
                (channel, now, limit),
            ).fetchall()
            claimed: list[OutboxRecord] = []
            for row in rows:
                cursor = connection.execute(
                    """UPDATE channel_outbox SET status='SENDING', lease_until=?,
                       attempt_count=attempt_count+1, updated_at=?
                       WHERE id=? AND status IN ('PENDING', 'RETRY_WAIT')""",
                    (lease, now, row["id"]),
                )
                if cursor.rowcount:
                    value = dict(row)
                    value.update(
                        status="SENDING",
                        lease_until=lease,
                        attempt_count=int(row["attempt_count"]) + 1,
                        updated_at=now,
                    )
                    claimed.append(OutboxRecord(**value))
        return claimed

    def mark_delivered(
        self,
        outbox_id: str,
        claimed_version: int,
        delivered_text: str,
        provider_message_id: str | None = None,
    ) -> OutboxRecord:
        now = utc_now()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM channel_outbox WHERE id=?", (outbox_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"outbox record not found: {outbox_id}")
            current = dict(row)
            settled = (
                int(row["delivery_version"]) == claimed_version
                and row["desired_text"] == delivered_text
            )
            status = "DELIVERED" if settled else "PENDING"
            resolved_provider_id = provider_message_id or row["provider_message_id"]
            connection.execute(
                """UPDATE channel_outbox SET provider_message_id=?, delivered_text=?,
                   status=?, available_at=?, lease_until=NULL, last_error=NULL,
                   updated_at=? WHERE id=?""",
                (resolved_provider_id, delivered_text, status, now, now, outbox_id),
            )
        return self.get_outbox(outbox_id)

    def mark_retry(
        self,
        outbox_id: str,
        error: str,
        *,
        ambiguous: bool,
        retry_after: float = 1,
        max_attempts: int = 8,
    ) -> OutboxRecord:
        now_dt = datetime.now(UTC)
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM channel_outbox WHERE id=?", (outbox_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"outbox record not found: {outbox_id}")
            if ambiguous and row["provider_message_id"] is None:
                status = "UNCERTAIN"
            elif int(row["attempt_count"]) >= max_attempts:
                status = "FAILED"
            else:
                status = "RETRY_WAIT"
            available = (now_dt + timedelta(seconds=max(retry_after, 0))).isoformat()
            connection.execute(
                """UPDATE channel_outbox SET status=?, available_at=?, lease_until=NULL,
                   last_error=?, updated_at=? WHERE id=?""",
                (status, available, error[:1000], now_dt.isoformat(), outbox_id),
            )
        return self.get_outbox(outbox_id)

    def update_presentation(
        self,
        run_id: str,
        text: str,
        event_sequence: int,
        *,
        throttle_seconds: float = 1,
    ) -> OutboxRecord | None:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        available = (now_dt + timedelta(seconds=max(throttle_seconds, 0))).isoformat()
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM channel_outbox WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            if int(row["event_sequence"]) >= event_sequence and row["desired_text"] == text:
                return _outbox(row)
            if row["status"] in {"UNCERTAIN", "FAILED"} and row["provider_message_id"] is None:
                status = row["status"]
            elif row["status"] == "SENDING":
                status = "SENDING"
            else:
                status = "PENDING"
            connection.execute(
                """UPDATE channel_outbox SET desired_text=?, event_sequence=?,
                   delivery_version=delivery_version+1, status=?, available_at=?,
                   updated_at=? WHERE id=?""",
                (text, event_sequence, status, available, now, row["id"]),
            )
        return self.get_outbox(str(row["id"]))

    def list_presentations(self, channel: str | None = None) -> list[OutboxRecord]:
        where = " AND channel=?" if channel else ""
        parameters: tuple[str, ...] = (channel,) if channel else ()
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM channel_outbox WHERE kind='PRESENTATION'"
                + where
                + " ORDER BY created_at",
                parameters,
            ).fetchall()
        return [_outbox(row) for row in rows]

    def get_outbox(self, outbox_id: str) -> OutboxRecord:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM channel_outbox WHERE id=?", (outbox_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"outbox record not found: {outbox_id}")
        return _outbox(row)

    def _binding_tx(
        self, connection: sqlite3.Connection, message: InboundMessage
    ) -> ChannelBinding | None:
        row = connection.execute(
            """SELECT * FROM channel_bindings
               WHERE channel=? AND sender_id=? AND chat_id=? AND thread_key=?""",
            (message.channel, message.sender_id, message.chat_id, message.thread_key),
        ).fetchone()
        return _binding(row) if row is not None else None

    def _switch_workspace_tx(
        self,
        connection: sqlite3.Connection,
        message: InboundMessage,
        alias: str,
        now: str,
    ) -> ChannelBinding | None:
        try:
            normalized_alias = _alias(alias)
        except ValueError:
            return None
        row = connection.execute(
            "SELECT workspace_id FROM workspace_aliases WHERE alias=?",
            (normalized_alias,),
        ).fetchone()
        if row is None:
            return None
        conversation_id = self._create_conversation_tx(
            connection, str(row["workspace_id"]), message, now
        )
        existing = self._binding_tx(connection, message)
        binding_id = existing.id if existing else str(uuid.uuid4())
        created_at = existing.created_at if existing else now
        connection.execute(
            """INSERT INTO channel_bindings(
                id, channel, sender_id, chat_id, thread_key, workspace_id,
                conversation_id, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel, sender_id, chat_id, thread_key) DO UPDATE SET
                workspace_id=excluded.workspace_id,
                conversation_id=excluded.conversation_id,
                updated_at=excluded.updated_at""",
            (
                binding_id,
                message.channel,
                message.sender_id,
                message.chat_id,
                message.thread_key,
                row["workspace_id"],
                conversation_id,
                created_at,
                now,
            ),
        )
        return ChannelBinding(
            binding_id,
            message.channel,
            message.sender_id,
            message.chat_id,
            message.thread_key,
            str(row["workspace_id"]),
            conversation_id,
            created_at,
            now,
        )

    def _new_conversation_tx(
        self,
        connection: sqlite3.Connection,
        message: InboundMessage,
        binding: ChannelBinding,
        now: str,
    ) -> ChannelBinding:
        conversation_id = self._create_conversation_tx(
            connection, binding.workspace_id, message, now
        )
        connection.execute(
            "UPDATE channel_bindings SET conversation_id=?, updated_at=? WHERE id=?",
            (conversation_id, now, binding.id),
        )
        return ChannelBinding(
            binding.id,
            binding.channel,
            binding.sender_id,
            binding.chat_id,
            binding.thread_key,
            binding.workspace_id,
            conversation_id,
            binding.created_at,
            now,
        )

    @staticmethod
    def _create_conversation_tx(
        connection: sqlite3.Connection,
        workspace_id: str,
        message: InboundMessage,
        now: str,
    ) -> str:
        conversation_id = str(uuid.uuid4())
        channel_key = (
            f"{message.channel}:{message.sender_id}:{message.chat_id}:"
            f"{message.thread_key}:{conversation_id}"
        )
        connection.execute(
            """INSERT INTO conversations(
                id, workspace_id, channel, channel_key, title, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
            (
                conversation_id,
                workspace_id,
                message.channel,
                channel_key,
                f"{message.channel} chat",
                now,
                now,
            ),
        )
        return conversation_id

    def _submit_run_tx(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        prompt: str,
        inbox_id: str,
        now: str,
    ) -> str:
        run_id = str(uuid.uuid4())
        request = {"prompt": prompt, "channel_inbox_id": inbox_id}
        connection.execute(
            """INSERT INTO runs(
                id, conversation_id, status, backend, request_json, created_at, version
            ) VALUES(?, ?, 'queued', 'agy-container', ?, ?, 1)""",
            (run_id, conversation_id, json.dumps(request, separators=(",", ":")), now),
        )
        connection.execute(
            """INSERT INTO messages(
                id, conversation_id, role, content, created_at, source_run_id
            ) VALUES(?, ?, 'user', ?, ?, ?)""",
            (str(uuid.uuid4()), conversation_id, prompt, now, run_id),
        )
        connection.execute(
            "UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id)
        )
        self.store._append_event_tx(
            connection, run_id, "run.queued", None, {"status": "queued"}, None, None
        )
        return run_id

    @staticmethod
    def _presentation_outbox_tx(
        connection: sqlite3.Connection,
        message: InboundMessage,
        run_id: str,
        now: str,
    ) -> str:
        outbox_id = str(uuid.uuid4())
        connection.execute(
            """INSERT INTO channel_outbox(
                id, channel, logical_key, kind, run_id, chat_id, thread_key,
                desired_text, status, available_at, created_at, updated_at
            ) VALUES(?, ?, ?, 'PRESENTATION', ?, ?, ?, ?, 'PENDING', ?, ?, ?)""",
            (
                outbox_id,
                message.channel,
                f"run:{run_id}",
                run_id,
                message.chat_id,
                message.thread_key,
                "◉ Working…",
                now,
                now,
                now,
            ),
        )
        return outbox_id

    @staticmethod
    def _message_outbox_tx(
        connection: sqlite3.Connection,
        message: InboundMessage,
        inbox_id: str,
        text: str,
        now: str,
    ) -> str:
        outbox_id = str(uuid.uuid4())
        connection.execute(
            """INSERT INTO channel_outbox(
                id, channel, logical_key, kind, chat_id, thread_key, desired_text,
                status, available_at, created_at, updated_at
            ) VALUES(?, ?, ?, 'MESSAGE', ?, ?, ?, 'PENDING', ?, ?, ?)""",
            (
                outbox_id,
                message.channel,
                f"inbound:{inbox_id}:response",
                message.chat_id,
                message.thread_key,
                text,
                now,
                now,
                now,
            ),
        )
        return outbox_id

    @staticmethod
    def _active_run_tx(
        connection: sqlite3.Connection, binding: ChannelBinding | None
    ) -> RunRecord | None:
        if binding is None:
            return None
        row = connection.execute(
            """SELECT * FROM runs WHERE conversation_id=?
               AND status IN ('running', 'queued')
               ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, created_at
               LIMIT 1""",
            (binding.conversation_id,),
        ).fetchone()
        if row is None:
            return None
        from .store import _run

        return _run(row)

    @staticmethod
    def _status_text_tx(
        connection: sqlite3.Connection, binding: ChannelBinding | None
    ) -> str:
        if binding is None:
            return "No workspace selected."
        row = connection.execute(
            "SELECT id, status FROM runs WHERE conversation_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (binding.conversation_id,),
        ).fetchone()
        if row is None:
            return "No runs in the current conversation."
        return f"Run {str(row['id'])[:8]}: {str(row['status']).upper()}"

    @staticmethod
    def _workspace_help_tx(connection: sqlite3.Connection) -> str:
        aliases = [
            str(row["alias"])
            for row in connection.execute(
                "SELECT alias FROM workspace_aliases ORDER BY alias"
            ).fetchall()
        ]
        if not aliases:
            return "No Telegram workspaces are approved on the server."
        return "Select a workspace with /workspace <alias>\nAvailable: " + ", ".join(
            aliases
        )

    @staticmethod
    def _advance_cursor_tx(
        connection: sqlite3.Connection, channel: str, update_id: int
    ) -> None:
        now = utc_now()
        connection.execute(
            """INSERT INTO channel_cursors(channel, last_update_id, updated_at)
               VALUES(?, ?, ?)
               ON CONFLICT(channel) DO UPDATE SET
                   last_update_id=MAX(channel_cursors.last_update_id, excluded.last_update_id),
                   updated_at=excluded.updated_at""",
            (channel, update_id, now),
        )


def _command(text: str) -> tuple[str | None, str]:
    if not text.startswith("/"):
        return None, ""
    first, _, remainder = text.partition(" ")
    command = first.split("@", 1)[0].lower()
    return command, remainder.strip()


def _alias(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized or len(normalized) > 64:
        raise ValueError("workspace alias must contain 1-64 characters")
    if not all(character.isalnum() or character in {"-", "_"} for character in normalized):
        raise ValueError("workspace alias may only contain letters, numbers, '-' and '_'")
    return normalized


def _binding(row: sqlite3.Row) -> ChannelBinding:
    return ChannelBinding(**dict(row))


def _outbox(row: sqlite3.Row) -> OutboxRecord:
    return OutboxRecord(**dict(row))


def _outcome(row: sqlite3.Row, *, duplicate: bool) -> IngestOutcome:
    return IngestOutcome(
        inbox_id=row["id"],
        duplicate=duplicate,
        result_kind=row["result_kind"],
        conversation_id=row["conversation_id"],
        run_id=row["run_id"],
        outbox_id=row["outbox_id"],
    )
