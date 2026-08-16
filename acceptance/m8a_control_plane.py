"""Deterministic M8A control-plane acceptance gate."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import tempfile
from pathlib import Path

import httpx
from starlette.testclient import TestClient

from gravityclaw.api import Settings, create_app
from gravityclaw.store import Store


def _abrupt_writer(database: str) -> None:
    store = Store(Path(database))
    store.initialize()
    store.record_audit(
        actor="crash-test", action="crash.boundary", resource_type="acceptance",
        payload={"token": "must-not-persist"},
    )
    os._exit(0)


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="gravityclaw-m8a-") as temporary:
        home = Path(temporary)
        app = create_app(Settings(home=home, mode="fake", control_token="m8a-token"))
        headers = {"authorization": "Bearer m8a-token"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/api/v1/control/home")).status_code == 401
            assert (await client.get("/api/v1/control/home", headers=headers)).status_code == 200
            workspace = app.state.store.create_workspace("m8a", home / "workspace")
            conversation = app.state.store.create_conversation(workspace.id)
            run = app.state.store.submit_run(conversation.id, {"prompt": "replay"})
            model = await client.get("/api/v1/control/home", headers=headers)
            assert model.json()["counts"]["queued_runs"] == 1
            timeline = await client.get(
                f"/api/v1/runs/{run.id}/timeline", headers=headers
            )
            assert timeline.status_code == 200

            schedule = await client.post(
                "/schedules", headers=headers, json={
                    "name": "m8a", "trigger_type": "one_shot",
                    "expression": "2026-08-16T12:00:00+00:00",
                    "prompt": "acceptance", "workspace_id": workspace.id,
                }
            )
            assert schedule.status_code == 201
            changed = await client.post(
                f"/schedules/{schedule.json()['id']}/disable", headers=headers,
                json={"expected_version": 1},
            )
            assert changed.status_code == 200
            stale = await client.post(
                f"/schedules/{schedule.json()['id']}/enable", headers=headers,
                json={"expected_version": 1},
            )
            assert stale.status_code == 409

        child = multiprocessing.Process(target=_abrupt_writer, args=(str(home / "gravityclaw.db"),))
        child.start()
        child.join(10)
        assert child.exitcode == 0
        reopened = Store(home / "gravityclaw.db")
        reopened.initialize()
        assert reopened.list_audit(limit=100)
        assert all("must-not-persist" not in str(item.payload) for item in reopened.list_audit(limit=100))
        assert reopened.latest_event_id() >= 1

        # Browser-compatible authenticated WebSocket handshake and snapshot.
        with TestClient(create_app(Settings(home=home, mode="fake", control_token="m8a-token"))) as client:
            with client.websocket_connect("/ws/control?access_token=m8a-token") as websocket:
                assert websocket.receive_json()["type"] == "control.snapshot"

    print("M8A_CONTROL_PLANE_OK")


if __name__ == "__main__":
    asyncio.run(main())
