from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from gravityclaw.api import Settings, create_app


class MilestoneThreeApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gravityclaw-api-m3-")
        self.home = Path(self.temporary.name)
        app = create_app(Settings(home=self.home, mode="fake"))
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


if __name__ == "__main__":
    unittest.main()
