#!/usr/bin/env python3
"""Milestone 2 hard gate: SIGKILL the gateway during concurrent Podman jobs."""

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
import time
from pathlib import Path
from typing import Any

import httpx
from websockets.sync.client import connect as websocket_connect

from gravityclaw.execution import MANAGED_LABEL, PodmanExecutionBackend
from gravityclaw.store import Store, TERMINAL_RUN_STATUSES


ROOT = Path(__file__).resolve().parents[1]
TEST_IMAGE = "localhost/gravityclaw-test-worker:latest"


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def start_gateway(home: Path, port: int) -> subprocess.Popen[bytes]:
    home.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "GRAVITYCLAW_HOME": str(home),
            "GRAVITYCLAW_MODE": "fake",
            "GRAVITYCLAW_WORKER_IMAGE": TEST_IMAGE,
            "GRAVITYCLAW_POLL_INTERVAL": "0.1",
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


def wait_health(client: httpx.Client, timeout: float = 20) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = client.get("/health")
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise TimeoutError("gateway did not become healthy")


def wait_until(predicate: Any, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise TimeoutError(message)


def create_workspace_and_conversation(
    client: httpx.Client, root: Path, name: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    workspace_path = root / name
    workspace = client.post(
        "/workspaces", json={"name": name, "path": str(workspace_path)}
    ).raise_for_status().json()
    conversation = client.post(
        "/conversations", json={"workspace_id": workspace["id"], "title": name}
    ).raise_for_status().json()
    return workspace, conversation


def submit(
    client: httpx.Client,
    conversation_id: str,
    scenario: str,
    *,
    delay: float | None = None,
    forbidden_path: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"prompt": f"acceptance scenario {scenario}", "scenario": scenario}
    if delay is not None:
        body["delay"] = delay
    if forbidden_path is not None:
        body["forbidden_path"] = forbidden_path
    return client.post(
        f"/conversations/{conversation_id}/runs", json=body
    ).raise_for_status().json()


def run_state(client: httpx.Client, run_id: str) -> dict[str, Any]:
    return client.get(f"/runs/{run_id}").raise_for_status().json()


def event_types(client: httpx.Client, run_id: str) -> list[str]:
    return [
        event["event_type"]
        for event in client.get(f"/runs/{run_id}/events").raise_for_status().json()
    ]


def build_image() -> None:
    subprocess.run(
        [
            "podman",
            "build",
            "-f",
            "worker/Containerfile.test",
            "-t",
            TEST_IMAGE,
            ".",
        ],
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    if not args.skip_build:
        build_image()

    gateway: subprocess.Popen[bytes] | None = None
    tracked_workers: set[str] = set()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="gravityclaw-m2-") as temporary:
        root = Path(temporary)
        home = root / "home"
        workspace_root = root / "workspaces"
        port = free_port()
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30)
        try:
            gateway = start_gateway(home, port)
            initial_health = wait_health(client)

            entries: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
            for name in (
                "a-text",
                "b-shell",
                "c-subagent",
                "d-queue",
                "e-failure",
                "h-disappeared",
            ):
                entries[name] = create_workspace_and_conversation(
                    client, workspace_root, name
                )

            runs = {
                "a": submit(client, entries["a-text"][1]["id"], "text", delay=10),
                "b": submit(client, entries["b-shell"][1]["id"], "long-command", delay=45),
                "c": submit(client, entries["c-subagent"][1]["id"], "subagent", delay=30),
                "d1": submit(client, entries["d-queue"][1]["id"], "long-command", delay=30),
                "d2": submit(client, entries["d-queue"][1]["id"], "text"),
                "e": submit(client, entries["e-failure"][1]["id"], "tool-failure", delay=30),
                "h": submit(
                    client,
                    entries["h-disappeared"][1]["id"],
                    "long-command",
                    delay=60,
                ),
            }

            def pre_crash_ready() -> bool:
                states = {key: run_state(client, value["id"])["status"] for key, value in runs.items()}
                if not all(
                    states[key] == "running"
                    for key in ("a", "b", "c", "d1", "e", "h")
                ):
                    return False
                if states["d2"] != "queued":
                    return False
                return (
                    "message.delta" in event_types(client, runs["a"]["id"])
                    and "tool.started" in event_types(client, runs["b"]["id"])
                    and "subagent.updated" in event_types(client, runs["c"]["id"])
                    and "tool.started" in event_types(client, runs["h"]["id"])
                )

            try:
                wait_until(pre_crash_ready, 25, "concurrent pre-crash scenarios were not ready")
            except TimeoutError as exc:
                diagnostics = {
                    key: {
                        "status": run_state(client, value["id"])["status"],
                        "events": event_types(client, value["id"]),
                    }
                    for key, value in runs.items()
                }
                raise AssertionError(
                    "pre-crash readiness failed: "
                    + json.dumps(diagnostics, sort_keys=True)
                    + "\ngateway log:\n"
                    + (home / "gateway.log").read_text(errors="replace")
                ) from exc
            store = Store(home / "gravityclaw.db")
            tracked_workers.update(
                run.worker_id
                for run in store.list_runs()
                if run.worker_id is not None
            )
            disappeared_worker = store.get_run(runs["h"]["id"]).worker_id
            if disappeared_worker is None:
                raise AssertionError("disappeared-worker scenario had no container")

            orphan_name = f"gravityclaw-orphan-{int(time.time())}"
            orphan_id = subprocess.run(
                [
                    "podman",
                    "run",
                    "--detach",
                    "--name",
                    orphan_name,
                    "--label",
                    f"{MANAGED_LABEL}=true",
                    "--label",
                    "io.gravityclaw.run-id=unknown-run",
                    "--label",
                    f"io.gravityclaw.workspace-id={entries['a-text'][0]['id']}",
                    "docker.io/library/alpine:3.22",
                    "sleep",
                    "60",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            tracked_workers.add(orphan_id)

            os.killpg(gateway.pid, signal.SIGKILL)
            gateway.wait(timeout=10)
            gateway = None
            subprocess.run(
                ["podman", "rm", "--force", disappeared_worker],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            time.sleep(32)

            survivors = subprocess.run(
                [
                    "podman",
                    "ps",
                    "--all",
                    "--filter",
                    f"label={MANAGED_LABEL}=true",
                    "--format",
                    "json",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            if len(json.loads(survivors)) < 6:
                raise AssertionError("workers did not survive the gateway SIGKILL")

            gateway = start_gateway(home, port)
            recovered_health = wait_health(client)
            recovered_report = recovered_health["reconciliation"]
            if (
                recovered_report["reattached"] < 1
                or recovered_report["interrupted"] < 1
                or recovered_report["orphaned"] < 1
            ):
                raise AssertionError(
                    "reconciliation did not exercise reattach/interrupted/orphan paths: "
                    + json.dumps(recovered_report, sort_keys=True)
                )

            terminal_expected = {
                "a": "completed",
                "b": "completed",
                "c": "completed",
                "d1": "completed",
                "d2": "completed",
                "e": "failed",
                "h": "interrupted",
            }

            def all_terminal() -> bool:
                return all(
                    run_state(client, runs[key]["id"])["status"] == status
                    for key, status in terminal_expected.items()
                )

            wait_until(all_terminal, 30, "recovered jobs did not reach deterministic states")

            isolation_workspace, isolation_conversation = create_workspace_and_conversation(
                client, workspace_root, "f-isolation"
            )
            isolation_run = submit(
                client,
                isolation_conversation["id"],
                "isolation",
                forbidden_path=entries["b-shell"][0]["path"],
            )
            wait_until(
                lambda: run_state(client, isolation_run["id"])["status"] == "completed",
                15,
                "workspace isolation run did not complete",
            )
            runs["f"] = isolation_run
            terminal_expected["f"] = "completed"

            _, cancel_conversation = create_workspace_and_conversation(
                client, workspace_root, "g-cancel"
            )
            cancel_run = submit(
                client, cancel_conversation["id"], "long-command", delay=30
            )
            wait_until(
                lambda: "tool.started" in event_types(client, cancel_run["id"]),
                15,
                "cancellation run never entered its command",
            )
            cancelled = client.post(
                f"/runs/{cancel_run['id']}/cancel"
            ).raise_for_status().json()
            if cancelled["status"] != "cancelled":
                raise AssertionError("cancellation was not confirmed")
            runs["g"] = cancel_run
            terminal_expected["g"] = "cancelled"

            with sqlite3.connect(home / "gravityclaw.db") as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            if integrity != "ok" or journal_mode.lower() != "wal":
                raise AssertionError(f"SQLite check failed: {integrity=}, {journal_mode=}")

            store = Store(home / "gravityclaw.db")
            for key, expected_status in terminal_expected.items():
                run = store.get_run(runs[key]["id"])
                if run.status != expected_status:
                    raise AssertionError(f"{key} was {run.status}, expected {expected_status}")
                events = store.list_events(run.id)
                sequences = [event.sequence for event in events]
                if sequences != list(range(1, len(sequences) + 1)):
                    raise AssertionError(f"non-contiguous event sequence for {key}")
                terminal_events = [
                    event
                    for event in events
                    if event.event_type
                    in {
                        "run.completed",
                        "run.failed",
                        "run.cancelled",
                        "run.interrupted",
                        "run.orphaned",
                    }
                ]
                if len(terminal_events) != 1:
                    raise AssertionError(f"duplicate/missing terminal event for {key}")

            d1 = store.get_run(runs["d1"]["id"])
            d2 = store.get_run(runs["d2"]["id"])
            if not d1.backend_conversation_id or d1.backend_conversation_id != d2.backend_conversation_id:
                raise AssertionError("queued follow-up did not resume the correct AGY conversation")

            workers = store.list_workers()
            if not any(worker.external_id == orphan_id and worker.state == "orphaned" for worker in workers):
                raise AssertionError("unknown worker was not recorded as orphaned")

            ws_url = f"ws://127.0.0.1:{port}/ws/runs/{runs['a']['id']}?after=0"
            replayed_sequences: list[int] = []
            with websocket_connect(ws_url, open_timeout=5) as websocket:
                while True:
                    message = json.loads(websocket.recv(timeout=10))
                    if message["type"] == "run.event":
                        replayed_sequences.append(message["event"]["sequence"])
                    if message["type"] == "run.terminal":
                        break
            expected_sequences = [
                event.sequence for event in store.list_events(runs["a"]["id"])
            ]
            if replayed_sequences != expected_sequences:
                raise AssertionError("WebSocket replay did not reconstruct persisted state")

            gateway.terminate()
            gateway.wait(timeout=10)
            gateway = start_gateway(home, port)
            idempotent_health = wait_health(client)
            report = idempotent_health["reconciliation"]
            if any(report.values()):
                raise AssertionError(f"reconciliation was not idempotent: {report}")

            running_managed = subprocess.run(
                [
                    "podman",
                    "ps",
                    "--filter",
                    f"label={MANAGED_LABEL}=true",
                    "--format",
                    "json",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            if json.loads(running_managed):
                raise AssertionError("managed containers or child processes remain running")
            process_scan = subprocess.run(
                ["ps", "-eo", "args="],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            if "/opt/gravityclaw/fake_agent.py" in process_scan:
                raise AssertionError("a worker child process escaped container cleanup")

            result = {
                "verdict": "PASSED",
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "initial_reconciliation": initial_health["reconciliation"],
                "post_crash_reconciliation": recovered_health["reconciliation"],
                "idempotent_reconciliation": report,
                "statuses": {
                    key: store.get_run(value["id"]).status for key, value in runs.items()
                },
                "sqlite": {"integrity": integrity, "journal_mode": journal_mode},
                "websocket_replayed_events": len(replayed_sequences),
                "orphan_terminated": True,
                "workspace_isolation": "passed",
            }
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        finally:
            client.close()
            if gateway is not None and gateway.poll() is None:
                gateway.terminate()
                try:
                    gateway.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(gateway.pid, signal.SIGKILL)
            database = home / "gravityclaw.db"
            if database.exists():
                try:
                    stored_runs = Store(database).list_runs()
                    stored_run_ids = {run.id for run in stored_runs}
                    tracked_workers.update(
                        run.worker_id
                        for run in stored_runs
                        if run.worker_id is not None
                    )
                    import asyncio

                    for snapshot in asyncio.run(PodmanExecutionBackend().list_managed()):
                        if snapshot.labels.get("io.gravityclaw.run-id") in stored_run_ids:
                            tracked_workers.add(snapshot.external_id)
                except Exception:
                    pass
            backend = PodmanExecutionBackend()
            for external_id in tracked_workers:
                try:
                    import asyncio

                    asyncio.run(backend.remove(external_id))
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
