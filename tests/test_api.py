from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from gravityclaw.api import Settings, create_app


class MilestoneThreeApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gravityclaw-api-m3-")
        self.home = Path(self.temporary.name)
        app = create_app(Settings(home=self.home, mode="fake"))
        self.app = app
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.temporary.cleanup()

    async def test_identity_bootstrap_and_explicit_memory_round_trip(self) -> None:
        identity = await self.client.get("/identity")
        self.assertEqual(identity.status_code, 200)
        self.assertEqual(
            [item["name"] for item in identity.json()],
            ["SOUL.md", "USER.md", "AGENTS.md", "TOOLS.md", "HEARTBEAT.md"],
        )
        created = await self.client.post(
            "/memories",
            json={
                "content": "Ahmad prefers concise status reports.",
                "source": "user",
                "confidence": 1.0,
            },
        )
        self.assertEqual(created.status_code, 201)
        results = await self.client.get(
            "/memories/search", params={"q": "concise reports"}
        )
        self.assertEqual(results.status_code, 200)
        self.assertEqual(results.json()[0]["id"], created.json()["id"])

    async def test_context_manifest_and_artifact_inspection_endpoints(self) -> None:
        store = self.app.state.store
        workspace = store.create_workspace("api", self.home / "workspace")
        conversation = store.create_conversation(workspace.id)
        run = store.submit_run(conversation.id, {"prompt": "inspect"})
        self.assertIsNotNone(store.claim_run(run.id))
        store.prepare_run_context(
            run.id, "sealed prompt",
            {"version": 2, "profile": "chat", "estimated_tokens": 4,
             "budget_tokens": 100, "sources": []},
        )
        inspected = await self.client.get(f"/runs/{run.id}/context")
        self.assertEqual(inspected.status_code, 200)
        self.assertEqual(inspected.json()["lifecycle"], "COMPILED")
        artifact = await self.client.post(
            f"/runs/{run.id}/artifacts",
            json={"kind": "log", "content": "failure details", "summary": "failure"},
        )
        self.assertEqual(artifact.status_code, 201)
        summaries = await self.client.get(f"/conversations/{conversation.id}/summaries")
        self.assertEqual(summaries.status_code, 200)
        self.assertEqual(summaries.json(), [])


class TelegramSettingsTests(unittest.TestCase):
    def test_token_file_is_loaded_without_appearing_in_settings_repr(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gravityclaw-token-") as temporary:
            token_file = Path(temporary) / "telegram-token"
            token_file.write_text("123456789:test-secret\n", encoding="utf-8")
            with patch.dict(
                "os.environ",
                {
                    "GRAVITYCLAW_HOME": temporary,
                    "GRAVITYCLAW_TELEGRAM_BOT_TOKEN_FILE": str(token_file),
                    "GRAVITYCLAW_TELEGRAM_USER_ID": "42",
                },
                clear=True,
            ):
                settings = Settings.from_environment()

        self.assertEqual(settings.telegram_token, "123456789:test-secret")
        self.assertNotIn("test-secret", repr(settings))

    def test_direct_token_and_token_file_are_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gravityclaw-token-") as temporary:
            token_file = Path(temporary) / "telegram-token"
            token_file.write_text("file-token", encoding="utf-8")
            with patch.dict(
                "os.environ",
                {
                    "GRAVITYCLAW_TELEGRAM_BOT_TOKEN": "direct-token",
                    "GRAVITYCLAW_TELEGRAM_BOT_TOKEN_FILE": str(token_file),
                },
                clear=True,
            ):
                with self.assertRaisesRegex(ValueError, "set only one"):
                    Settings.from_environment()


if __name__ == "__main__":
    unittest.main()
