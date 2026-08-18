import tempfile
import tempfile
import unittest
from pathlib import Path

import httpx

from gravityclaw.api import Settings, create_app


CONTROL_TOKEN = "gateway-test-token"


class ProductionGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gravityclaw-gateway-")
        root = Path(self.temporary.name)
        self.frontend = root / "web_dist"
        self.frontend.mkdir()
        (self.frontend / "index.html").write_text(
            "<!doctype html><html><body>gravityclaw-console</body></html>",
            encoding="utf-8",
        )
        (self.frontend / "assets").mkdir()
        (self.frontend / "assets" / "app.js").write_text("console.log('ok');", encoding="utf-8")
        self.app = create_app(
            Settings(home=root / "data", mode="fake", frontend_dir=self.frontend)
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.temporary.cleanup()

    async def test_gateway_serves_static_shell_and_history_fallback(self) -> None:
        root = await self.client.get("/")
        self.assertEqual(root.status_code, 200)
        self.assertIn("gravityclaw-console", root.text)

        history = await self.client.get("/conversations/123")
        self.assertEqual(history.status_code, 200)
        self.assertIn("gravityclaw-console", history.text)

        asset = await self.client.get("/assets/app.js")
        self.assertEqual(asset.status_code, 200)
        self.assertIn("console.log", asset.text)

    async def test_control_token_leaves_frontend_shell_public_but_protects_api(self) -> None:
        app = create_app(
            Settings(
                home=Path(self.temporary.name) / "authenticated-data",
                mode="fake",
                frontend_dir=self.frontend,
                control_token=CONTROL_TOKEN,
            )
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        try:
            root = await client.get("/")
            self.assertEqual(root.status_code, 200)
            self.assertIn("gravityclaw-console", root.text)
            asset = await client.get("/assets/app.js")
            self.assertEqual(asset.status_code, 200)
            history = await client.get("/conversations/123")
            self.assertEqual(history.status_code, 200)
            self.assertEqual((await client.get("/docs")).status_code, 401)
            self.assertEqual((await client.get("/api/v1/control/home")).status_code, 401)
            self.assertEqual(
                (await client.get(
                    "/api/v1/control/home",
                    headers={"authorization": f"Bearer {CONTROL_TOKEN}"},
                )).status_code,
                200,
            )
        finally:
            await client.aclose()

    async def test_api_paths_do_not_fall_back_to_html(self) -> None:
        health = await self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")

        missing_api = await self.client.get("/api/not-a-route")
        self.assertEqual(missing_api.status_code, 404)
        self.assertNotIn("gravityclaw-console", missing_api.text)

        missing_asset = await self.client.get("/assets/missing.js")
        self.assertEqual(missing_asset.status_code, 404)


if __name__ == "__main__":
    unittest.main()
