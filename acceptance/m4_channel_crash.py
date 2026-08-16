#!/usr/bin/env python3
"""M4 hard gate: kill the gateway across durable Telegram channel boundaries."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

from gravityclaw.channel_store import ChannelStore
from gravityclaw.channels import InboundMessage
from gravityclaw.execution import MANAGED_LABEL
from gravityclaw.store import Store, TERMINAL_RUN_STATUSES, utc_now


ROOT = Path(__file__).resolve().parents[1]
TEST_IMAGE = "localhost/gravityclaw-test-worker:latest"
TOKEN = "m4-secret-token"


class TelegramState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.updates: dict[int, dict[str, Any]] = {}
        self.messages: dict[str, dict[str, str]] = {}
        self.next_message_id = 1
        self.drop_send_chat_ids: set[str] = set()
        self.drop_edit_message_ids: set[str] = set()
        self.send_calls = 0
        self.edit_calls = 0

    def add_update(
        self, update_id: int, text: str, *, sender: str = "42", chat: str = "100"
    ) -> None:
        with self.lock:
            self.updates[update_id] = {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "date": int(time.time()),
                    "text": text,
                    "from": {"id": int(sender)},
                    "chat": {"id": int(chat), "type": "private"},
                },
            }

    def create_message(self, chat_id: str, text: str) -> str:
        with self.lock:
            message_id = str(self.next_message_id)
            self.next_message_id += 1
            self.messages[message_id] = {"chat_id": chat_id, "text": text}
            return message_id


def handler_for(state: TelegramState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            method = self.path.rsplit("/", 1)[-1]
            if method == "getUpdates":
                offset = int(payload.get("offset", 0))
                with state.lock:
                    result = [
                        state.updates[key]
                        for key in sorted(state.updates)
                        if key >= offset
                    ]
                if not result:
                    time.sleep(0.05)
                self._json({"ok": True, "result": result})
                return
            if method == "sendMessage":
                with state.lock:
                    state.send_calls += 1
                    message_id = str(state.next_message_id)
                    state.next_message_id += 1
                    state.messages[message_id] = {
                        "chat_id": str(payload["chat_id"]),
                        "text": str(payload["text"]),
                    }
                    chat_id = str(payload["chat_id"])
                    drop = chat_id in state.drop_send_chat_ids
                    state.drop_send_chat_ids.discard(chat_id)
                if drop:
                    self._drop()
                    return
                self._json({"ok": True, "result": {"message_id": int(message_id)}})
                return
            if method == "editMessageText":
                message_id = str(payload["message_id"])
                text = str(payload["text"])
                with state.lock:
                    state.edit_calls += 1
                    current = state.messages.get(message_id)
                    if current is None:
                        self._json(
                            {
                                "ok": False,
                                "error_code": 400,
                                "description": "Bad Request: message to edit not found",
                            }
                        )
                        return
                    unchanged = current["text"] == text
                    current["text"] = text
                    drop = message_id in state.drop_edit_message_ids
                    state.drop_edit_message_ids.discard(message_id)
                if drop:
                    self._drop()
                    return
                if unchanged:
                    self._json(
                        {
                            "ok": False,
                            "error_code": 400,
                            "description": "Bad Request: message is not modified",
                        }
                    )
                    return
                self._json({"ok": True, "result": True})
                return
            self._json({"ok": False, "error_code": 404, "description": "unknown"})

        def _json(self, value: dict[str, Any]) -> None:
            body = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def _drop(self) -> None:
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()

    return Handler


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def start_gateway(home: Path, port: int, telegram_port: int) -> subprocess.Popen[bytes]:
    environment = os.environ.copy()
    environment.update(
        {
            "GRAVITYCLAW_HOME": str(home),
            "GRAVITYCLAW_MODE": "fake",
            "GRAVITYCLAW_WORKER_IMAGE": TEST_IMAGE,
            "GRAVITYCLAW_POLL_INTERVAL": "0.1",
            "GRAVITYCLAW_TELEGRAM_BOT_TOKEN": TOKEN,
            "GRAVITYCLAW_TELEGRAM_USER_ID": "42",
            "GRAVITYCLAW_TELEGRAM_DEFAULT_WORKSPACE": "gravityclaw",
            "GRAVITYCLAW_TELEGRAM_API_ROOT": f"http://127.0.0.1:{telegram_port}",
        }
    )
    log = (home / "gateway.log").open("ab")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "gravityclaw.server",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log.close()
    return process


def wait_until(predicate: Any, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise TimeoutError(message)


def wait_health(client: httpx.Client, timeout: float = 20) -> dict[str, Any]:
    result: dict[str, Any] = {}

    def ready() -> bool:
        nonlocal result
        try:
            response = client.get("/health")
            if response.status_code == 200:
                result = response.json()
                return True
        except httpx.HTTPError:
            pass
        return False

    wait_until(ready, timeout, "gateway did not become healthy")
    return result


def insert_test_outbox(
    store: Store,
    *,
    logical_key: str,
    chat_id: str,
    text: str,
    provider_message_id: str | None,
) -> str:
    outbox_id = str(uuid.uuid4())
    now = utc_now()
    with store._connect() as connection:
        connection.execute(
            """INSERT INTO channel_outbox(
                id, channel, logical_key, kind, chat_id, provider_message_id,
                desired_text, delivered_text, status, available_at, created_at, updated_at
            ) VALUES(?, 'telegram', ?, 'MESSAGE', ?, ?, ?, ?, 'PENDING', ?, ?, ?)""",
            (
                outbox_id,
                logical_key,
                chat_id,
                provider_message_id,
                text,
                "old" if provider_message_id else None,
                now,
                now,
                now,
            ),
        )
    return outbox_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    if not args.skip_build:
        subprocess.run(
            ["podman", "build", "-f", "worker/Containerfile.test", "-t", TEST_IMAGE, "."],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )

    state = TelegramState()
    telegram_port = free_port()
    telegram_server = ThreadingHTTPServer(
        ("127.0.0.1", telegram_port), handler_for(state)
    )
    server_thread = threading.Thread(target=telegram_server.serve_forever, daemon=True)
    server_thread.start()

    gateway: subprocess.Popen[bytes] | None = None
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="gravityclaw-m4-") as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        workspace_path = root / "workspace"
        port = free_port()
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10)
        tracked_workers: set[str] = set()
        try:
            gateway = start_gateway(home, port, telegram_port)
            wait_health(client)
            workspace = client.post(
                "/workspaces", json={"name": "gravityclaw", "path": str(workspace_path)}
            ).raise_for_status().json()
            client.post(
                "/workspace-aliases",
                json={"alias": "gravityclaw", "workspace_id": workspace["id"]},
            ).raise_for_status()

            state.add_update(1, "first channel run")
            state.add_update(2, "unauthorized", sender="666")
            store = Store(home / "gravityclaw.db")

            def first_running() -> bool:
                runs = store.list_runs()
                return bool(
                    runs
                    and runs[0].status == "running"
                    and runs[0].worker_id
                    and ChannelStore(store).cursor("telegram") >= 2
                )

            wait_until(first_running, 20, "first Telegram run did not start")
            first_run = store.list_runs()[0]
            tracked_workers.add(first_run.worker_id)
            os.killpg(gateway.pid, signal.SIGKILL)
            gateway.wait(timeout=10)
            gateway = None

            channel_store = ChannelStore(store)
            persisted = channel_store.ingest(
                InboundMessage("telegram", 3, "42", "200", "persisted before start"),
                default_workspace_alias="gravityclaw",
            )
            with store._connect() as connection:
                request = json.loads(
                    connection.execute(
                        "SELECT request_json FROM runs WHERE id=?", (persisted.run_id,)
                    ).fetchone()[0]
                )
                request.update({"scenario": "long-command", "delay": 20})
                connection.execute(
                    "UPDATE runs SET request_json=? WHERE id=?",
                    (json.dumps(request, separators=(",", ":")), persisted.run_id),
                )
            followup = channel_store.ingest(
                InboundMessage("telegram", 4, "42", "200", "queued follow-up"),
                default_workspace_alias="gravityclaw",
            )
            stop = channel_store.ingest(
                InboundMessage("telegram", 5, "42", "200", "/stop"),
                default_workspace_alias="gravityclaw",
            )
            if stop.run_id != persisted.run_id:
                raise AssertionError("durable stop did not target the first queued run")

            existing_message = state.create_message("300", "old")
            final_outbox = insert_test_outbox(
                store,
                logical_key="acceptance:final-edit",
                chat_id="300",
                text="final after lost acknowledgement",
                provider_message_id=existing_message,
            )
            uncertain_outbox = insert_test_outbox(
                store,
                logical_key="acceptance:initial-send",
                chat_id="400",
                text="ambiguous initial send",
                provider_message_id=None,
            )
            with state.lock:
                state.drop_edit_message_ids.add(existing_message)
                state.drop_send_chat_ids.add("400")

            gateway = start_gateway(home, port, telegram_port)
            recovered_health = wait_health(client)

            def channel_settled() -> bool:
                runs = {run.id: run for run in store.list_runs()}
                final = channel_store.get_outbox(final_outbox)
                uncertain = channel_store.get_outbox(uncertain_outbox)
                with store._connect() as connection:
                    cancellation_row = connection.execute(
                        "SELECT status FROM cancellation_requests WHERE run_id=?",
                        (persisted.run_id,),
                    ).fetchone()
                return (
                    runs[first_run.id].status in TERMINAL_RUN_STATUSES
                    and runs[persisted.run_id].status == "cancelled"
                    and runs[followup.run_id].status == "completed"
                    and cancellation_row is not None
                    and cancellation_row["status"] == "COMPLETED"
                    and final.status == "DELIVERED"
                    and uncertain.status == "UNCERTAIN"
                )

            wait_until(channel_settled, 40, "channel recovery did not settle")

            def deliveries_settled() -> bool:
                with store._connect() as connection:
                    pending = connection.execute(
                        """SELECT COUNT(*) FROM channel_outbox
                           WHERE status IN ('PENDING', 'SENDING', 'RETRY_WAIT')"""
                    ).fetchone()[0]
                return pending == 0

            wait_until(deliveries_settled, 15, "channel deliveries did not settle")
            for run in store.list_runs():
                if run.worker_id:
                    tracked_workers.add(run.worker_id)

            with store._connect() as connection:
                authorized = connection.execute(
                    "SELECT COUNT(*) FROM channel_inbox WHERE provider_update_id=1"
                ).fetchone()[0]
                unauthorized = connection.execute(
                    "SELECT COUNT(*) FROM channel_inbox WHERE provider_update_id=2"
                ).fetchone()[0]
                cancellation = connection.execute(
                    "SELECT status FROM cancellation_requests WHERE run_id=?",
                    (persisted.run_id,),
                ).fetchone()[0]
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            if authorized != 1 or unauthorized != 0:
                raise AssertionError("Telegram authorization/deduplication invariant failed")
            if cancellation != "COMPLETED":
                raise AssertionError(f"cancellation was not reconciled: {cancellation}")
            if channel_store.cursor("telegram") < 5:
                raise AssertionError("polling cursor did not advance durably")
            if state.messages[existing_message]["text"] != "final after lost acknowledgement":
                raise AssertionError("lost-ack final edit was not recovered")
            messages_before_restart = len(state.messages)

            os.killpg(gateway.pid, signal.SIGTERM)
            gateway.wait(timeout=15)
            gateway = start_gateway(home, port, telegram_port)
            second_recovery = wait_health(client)
            time.sleep(3)
            if len(state.messages) != messages_before_restart:
                raise AssertionError("idempotent restart created a duplicate Telegram message")
            if len([run for run in store.list_runs() if run.id == first_run.id]) != 1:
                raise AssertionError("duplicate inbound update created multiple runs")

            log_text = (home / "gateway.log").read_text(errors="replace")
            database_bytes = (home / "gravityclaw.db").read_bytes()
            if TOKEN in log_text or TOKEN.encode() in database_bytes:
                raise AssertionError("Telegram token leaked into logs or SQLite")
            if integrity != "ok" or journal_mode.lower() != "wal":
                raise AssertionError("SQLite integrity/WAL check failed")

            result = {
                "verdict": "PASSED",
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "authorized_inbox_rows": authorized,
                "unauthorized_inbox_rows": unauthorized,
                "polling_cursor": channel_store.cursor("telegram"),
                "persisted_before_start": store.get_run(persisted.run_id).status,
                "queued_followup": store.get_run(followup.run_id).status,
                "durable_cancellation": cancellation,
                "initial_send_boundary": channel_store.get_outbox(uncertain_outbox).status,
                "final_edit_boundary": channel_store.get_outbox(final_outbox).status,
                "telegram_messages": len(state.messages),
                "token_leak": False,
                "sqlite": {"integrity": integrity, "journal_mode": journal_mode},
                "first_reconciliation": recovered_health["reconciliation"],
                "idempotent_reconciliation": second_recovery["reconciliation"],
            }
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        finally:
            client.close()
            if gateway is not None and gateway.poll() is None:
                try:
                    os.killpg(gateway.pid, signal.SIGTERM)
                    gateway.wait(timeout=10)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(gateway.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            for worker_id in tracked_workers:
                subprocess.run(
                    ["podman", "rm", "--force", worker_id],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            subprocess.run(
                [
                    "podman",
                    "ps",
                    "--all",
                    "--filter",
                    f"label={MANAGED_LABEL}=true",
                    "--format",
                    "{{.ID}}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            telegram_server.shutdown()
            telegram_server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
