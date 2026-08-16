from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path

import httpx

from gravityclaw.api import Settings, create_app


class ControlPlaneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gravityclaw-control-")
        self.home = Path(self.temporary.name)
        self.app = create_app(Settings(
            home=self.home, mode="fake", control_token="m8-test-token"
        ))
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )
        self.headers = {"authorization": "Bearer m8-test-token"}

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.temporary.cleanup()

    async def test_http_control_plane_requires_bearer_token_but_health_is_public(self) -> None:
        self.assertEqual((await self.client.get("/health")).status_code, 200)
        self.assertEqual((await self.client.get("/docs")).status_code, 401)
        self.assertEqual((await self.client.get("/docs", headers=self.headers)).status_code, 200)
        denied = await self.client.get("/api/v1/control/home")
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(
            (await self.client.get("/api/v1/control/home", headers=self.headers)).status_code,
            200,
        )
        wrong = await self.client.get(
            "/api/v1/control/home", headers={"authorization": "Bearer wrong"}
        )
        self.assertEqual(wrong.status_code, 401)

    async def test_browser_session_uses_httponly_cookie_without_local_storage_token(self) -> None:
        invalid = await self.client.post("/auth/session", json={"token": "wrong"})
        self.assertEqual(invalid.status_code, 401)
        login = await self.client.post("/auth/session", json={"token": "m8-test-token"})
        self.assertEqual(login.status_code, 200)
        self.assertIn("gravityclaw_session", login.headers.get("set-cookie", ""))
        self.assertEqual(
            (await self.client.get("/api/v1/control/home")).status_code, 200
        )
        logout = await self.client.delete("/auth/session")
        self.assertEqual(logout.status_code, 200)
        self.assertEqual(
            (await self.client.get("/api/v1/control/home")).status_code, 401
        )

    async def test_schedule_mutations_use_versions_and_write_audit_records(self) -> None:
        workspace = self.app.state.store.create_workspace("control", self.home / "workspace")
        created = await self.client.post(
            "/schedules", headers=self.headers, json={
                "name": "control test", "trigger_type": "one_shot",
                "expression": "2026-08-16T12:00:00+00:00", "prompt": "check",
                "workspace_id": workspace.id,
            }
        )
        self.assertEqual(created.status_code, 201)
        schedule = created.json()
        self.assertEqual(schedule["version"], 1)
        enabled = await self.client.post(
            f"/schedules/{schedule['id']}/enable", headers=self.headers,
            json={"expected_version": 1},
        )
        self.assertEqual(enabled.status_code, 200)
        self.assertEqual(enabled.json()["version"], 2)
        stale = await self.client.post(
            f"/schedules/{schedule['id']}/disable", headers=self.headers,
            json={"expected_version": 1},
        )
        self.assertEqual(stale.status_code, 409)
        audit = await self.client.get("/api/v1/audit", headers=self.headers)
        self.assertEqual(audit.status_code, 200)
        self.assertGreaterEqual(len(audit.json()), 2)
        self.assertTrue(all(item["actor"] == "control-token" for item in audit.json()))

    async def test_read_models_and_global_event_cursor_are_replayable(self) -> None:
        store = self.app.state.store
        workspace = store.create_workspace("control", self.home / "workspace")
        conversation = store.create_conversation(workspace.id, title="Replay")
        run = store.submit_run(conversation.id, {"prompt": "hello"})
        home = await self.client.get("/api/v1/control/home", headers=self.headers)
        self.assertEqual(home.status_code, 200)
        self.assertEqual(home.json()["counts"]["queued_runs"], 1)
        detail = await self.client.get(
            f"/api/v1/conversations/{conversation.id}", headers=self.headers
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["runs"][0]["id"], run.id)
        first = store.list_events_global(after_id=0)
        self.assertEqual([item.id for item in first], sorted(item.id for item in first))
        cursor = first[0].id
        replay = store.list_events_global(after_id=cursor)
        self.assertTrue(all(item.id > cursor for item in replay))

    async def test_web_and_telegram_origins_share_the_same_conversation_contract(self) -> None:
        store = self.app.state.store
        workspace = store.create_workspace("shared", self.home / "shared")
        telegram = store.create_conversation(
            workspace.id, channel="telegram", channel_key="chat:42", title="Telegram thread"
        )
        listed = await self.client.get("/api/v1/conversations", headers=self.headers)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()[0]["id"], telegram.id)
        submitted = await self.client.post(
            f"/conversations/{telegram.id}/runs", headers=self.headers,
            json={"prompt": "Continue from Web"},
        )
        self.assertEqual(submitted.status_code, 202)
        detail = await self.client.get(
            f"/api/v1/conversations/{telegram.id}", headers=self.headers
        )
        self.assertEqual(detail.json()["conversation"]["channel"], "telegram")
        self.assertEqual(detail.json()["messages"][-1]["content"], "Continue from Web")

    async def test_audit_redacts_secret_shaped_values(self) -> None:
        self.app.state.store.record_audit(
            actor="control-token", action="test", resource_type="mcp",
            payload={"env_refs": {"GITHUB_TOKEN": "secret:github"}, "token": "raw-secret"},
        )
        response = await self.client.get("/api/v1/audit", headers=self.headers)
        body = response.text
        self.assertNotIn("raw-secret", body)
        self.assertNotIn("secret:github", body)
        self.assertIn("<redacted>", body)

    async def test_inspector_events_redact_secret_shaped_values(self) -> None:
        from gravityclaw.events import AgentEvent

        workspace = self.app.state.store.create_workspace("redaction", self.home / "redaction")
        conversation = self.app.state.store.create_conversation(workspace.id)
        run = self.app.state.store.submit_run(conversation.id, {"prompt": "inspect event"})
        self.app.state.store.append_event(
            run.id,
            AgentEvent(
                "agent.step", run.id,
                data={"token": "raw-token", "nested": {"password": "raw-password"}},
                raw={"event": "step_update", "api_key": "raw-key"},
            ),
        )
        response = await self.client.get(f"/api/v1/runs/{run.id}/timeline", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertNotIn("raw-token", body)
        self.assertNotIn("raw-password", body)
        self.assertNotIn("raw-key", body)
        self.assertIn("<redacted>", body)

    async def test_inspector_timeline_is_cursor_paginated(self) -> None:
        from gravityclaw.events import AgentEvent

        workspace = self.app.state.store.create_workspace("pagination", self.home / "pagination")
        conversation = self.app.state.store.create_conversation(workspace.id)
        run = self.app.state.store.submit_run(conversation.id, {"prompt": "paginate"})
        for index in range(3):
            self.app.state.store.append_event(run.id, AgentEvent("test.event", run.id, data={"index": index}))
        first = await self.client.get(f"/api/v1/runs/{run.id}/timeline?limit=2", headers=self.headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(first.json()["events"]), 2)
        self.assertTrue(first.json()["has_more"])
        second = await self.client.get(
            f"/api/v1/runs/{run.id}/timeline?after={first.json()['next_after']}&limit=2",
            headers=self.headers,
        )
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["has_more"])
        self.assertEqual(len(second.json()["events"]), 2)

    async def test_inspector_optional_manifests_use_empty_success_responses(self) -> None:
        workspace = self.app.state.store.create_workspace("optional", self.home / "optional")
        conversation = self.app.state.store.create_conversation(workspace.id)
        run = self.app.state.store.submit_run(conversation.id, {"prompt": "historical"})
        context = await self.client.get(f"/api/v1/runs/{run.id}/context", headers=self.headers)
        capabilities = await self.client.get(f"/api/v1/runs/{run.id}/capabilities", headers=self.headers)
        self.assertEqual(context.status_code, 204)
        self.assertEqual(capabilities.status_code, 204)

    async def test_m8b6_automation_run_now_is_idempotent_and_capabilities_are_scoped(self) -> None:
        store = self.app.state.store
        workspace = store.create_workspace("automation", self.home / "automation")
        skill_path = workspace.path / ".agents" / "skills" / "coding"
        skill_path.mkdir(parents=True)
        (skill_path / "SKILL.md").write_text("# Coding\n", encoding="utf-8")
        skill = await self.client.post(
            "/capabilities/skills", headers=self.headers,
            json={"id": "coding", "name": "Coding", "path": str(skill_path), "workspace_id": workspace.id},
        )
        self.assertEqual(skill.status_code, 201)
        mcp = await self.client.post(
            "/capabilities/mcp", headers=self.headers,
            json={"id": "github", "name": "GitHub", "transport": "stdio", "command": "sh", "env_refs": {"TOKEN": "secret:github-token"}, "workspace_id": workspace.id},
        )
        self.assertEqual(mcp.status_code, 201)
        capabilities = await self.client.get(
            "/api/v1/capabilities", headers=self.headers, params={"workspace_id": workspace.id}
        )
        self.assertEqual(capabilities.status_code, 200)
        self.assertEqual(capabilities.json()["workspace"]["id"], workspace.id)
        self.assertIn("secret:github-token", capabilities.text)
        self.assertNotIn("raw-secret", capabilities.text)
        self.assertNotIn("GRAVITYCLAW_SECRET", capabilities.text)

        created = await self.client.post(
            "/api/v1/automations", headers=self.headers,
            json={"name": "Manual check", "trigger_type": "interval", "expression": "3600", "prompt": "check", "workspace_id": workspace.id},
        )
        self.assertEqual(created.status_code, 201)
        schedule = created.json()
        with patch.object(self.app.state.scheduler.manager, "activate", new=AsyncMock()):
            first = await self.client.post(
                f"/api/v1/automations/{schedule['id']}/run-now", headers=self.headers,
                json={"request_id": "browser-retry-1"},
            )
            second = await self.client.post(
                f"/api/v1/automations/{schedule['id']}/run-now", headers=self.headers,
                json={"request_id": "browser-retry-1"},
            )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()["trigger"]["id"], second.json()["trigger"]["id"])
        triggers = await self.client.get(f"/api/v1/automations/{schedule['id']}", headers=self.headers)
        self.assertEqual(len([item for item in triggers.json()["triggers"] if ":manual:browser-retry-1" in item["execution_key"]]), 1)

        stale = await self.client.post(
            f"/api/v1/capabilities/mcp/github/disable", headers=self.headers,
            json={"expected_updated_at": "stale"},
        )
        self.assertEqual(stale.status_code, 409)


if __name__ == "__main__":
    unittest.main()
