from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gravityclaw.capabilities import CapabilityError, CapabilityManager
from gravityclaw.execution import AgyContainerSpecFactory
from gravityclaw.store import Store


class CapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gravityclaw-m7-")
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "gravityclaw.db")
        self.store.initialize()
        self.workspace_a = self.store.create_workspace("a", self.root / "a")
        self.workspace_b = self.store.create_workspace("b", self.root / "b")
        self.secret_dir = self.root / "secrets"
        self.secret_dir.mkdir(mode=0o700)
        (self.secret_dir / "github-token").write_text("super-secret-value\n", encoding="utf-8")
        self.capabilities = CapabilityManager(self.root, self.store, secret_dir=self.secret_dir)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _skill(self, workspace: Path, name: str, content: str = "# Skill\n") -> Path:
        path = workspace / ".agents" / "skills" / name
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(content, encoding="utf-8")
        return path

    def test_skill_validation_discovery_and_workspace_scope(self) -> None:
        local = self._skill(self.workspace_a.path, "local")
        invalid = self.workspace_a.path / ".agents" / "skills" / "invalid"
        invalid.mkdir(parents=True)
        records = self.capabilities.discover_skills(self.workspace_a.id)
        self.assertEqual({item.id for item in records}, {
            f"workspace:{self.workspace_a.id}:local",
            f"workspace:{self.workspace_a.id}:invalid",
        })
        self.assertEqual(self.capabilities.get_skill(records[0].id).path.parent.name, "skills")
        self.assertEqual(self.capabilities.get_skill(f"workspace:{self.workspace_a.id}:invalid").validation_state, "INVALID")
        with self.assertRaises(CapabilityError):
            self.capabilities.register_skill(
                skill_id="escape", name="escape", path=self.root / "outside",
                workspace_id=self.workspace_a.id,
            )
        self.assertTrue(local.is_dir())

    def test_mcp_validation_health_and_secret_references(self) -> None:
        server = self.capabilities.register_mcp(
            server_id="github", name="GitHub", transport="stdio", command="echo",
            args=("mcp",), env_refs={"GITHUB_TOKEN": "secret:github-token"},
            workspace_id=self.workspace_a.id,
        )
        self.assertEqual(server.env_refs["GITHUB_TOKEN"], "secret:github-token")
        self.assertNotIn("super-secret-value", json.dumps(server.__dict__ if hasattr(server, "__dict__") else str(server)))
        self.assertEqual(self.capabilities.health_check("github").health_state, "HEALTHY")
        with self.assertRaises(CapabilityError):
            self.capabilities.register_mcp(
                server_id="bad", name="bad", transport="stdio", command="echo",
                env_refs={"TOKEN": "super-secret-value"},
            )

    def test_run_manifest_is_immutable_and_worker_gets_only_selected_material(self) -> None:
        skill_path = self._skill(self.workspace_a.path, "python", "# Python skill\n")
        self.capabilities.register_skill(
            skill_id="python", name="Python", path=skill_path,
            workspace_id=self.workspace_a.id, profiles=("coding",), version="1",
        )
        self.capabilities.register_mcp(
            server_id="github", name="GitHub", transport="stdio", command="echo",
            env_refs={"GITHUB_TOKEN": "secret:github-token"},
            workspace_id=self.workspace_a.id,
        )
        conversation = self.store.create_conversation(self.workspace_a.id)
        run = self.store.submit_run(
            conversation.id, {"prompt": "code", "context_profile": "coding", "allow_all": True}
        )
        claimed = self.store.claim_run(run.id)
        assert claimed is not None
        prepared = self.capabilities.prepare_run(
            claimed, conversation, self.workspace_a
        )
        manifest = self.store.get_capability_manifest(run.id)
        self.assertEqual([item["id"] for item in manifest["skills"]], ["python"])
        self.assertEqual([item["id"] for item in manifest["mcp"]], ["github"])
        self.assertIn("secret:github-token", json.dumps(manifest))
        self.assertNotIn("super-secret-value", json.dumps(manifest))
        self.assertNotIn("super-secret-value", json.dumps(prepared.request))

        spec = AgyContainerSpecFactory().build(prepared, conversation, self.workspace_a)
        applied = self.capabilities.apply_to_spec(spec, prepared)
        self.assertIn("--add-dir", applied.command)
        self.assertTrue(any(target == "/gravityclaw/capabilities/skills" for _, target, _ in applied.mounts))
        self.assertTrue(any(target.endswith("mcp_config.json") for _, target, _ in applied.mounts))
        self.assertEqual(applied.environment["GRAVITYCLAW_SECRET_GITHUB_TOKEN"], "super-secret-value")
        snapshot = Path(prepared.request["capability_snapshot"])
        config = json.loads((snapshot / "mcp_config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["mcpServers"]["github"]["env"]["GITHUB_TOKEN"], "${GRAVITYCLAW_SECRET_GITHUB_TOKEN}")
        manifest_hash = manifest["manifest_hash"]

        # Registry changes do not alter the already-published worker snapshot.
        (skill_path / "SKILL.md").write_text("# Changed after run\n", encoding="utf-8")
        self.capabilities.set_skill_enabled("python", False)
        self.assertEqual(self.store.get_capability_manifest(run.id)["manifest_hash"], manifest_hash)
        self.assertTrue((snapshot / "skills" / ".agents" / "skills" / "python" / "SKILL.md").read_text(encoding="utf-8").startswith("# Python"))

    def test_workspace_capabilities_do_not_cross_over(self) -> None:
        path = self._skill(self.workspace_a.path, "private")
        self.capabilities.register_skill(
            skill_id="private", name="Private", path=path, workspace_id=self.workspace_a.id
        )
        conversation = self.store.create_conversation(self.workspace_b.id)
        run = self.store.submit_run(conversation.id, {"prompt": "b", "context_profile": "coding"})
        claimed = self.store.claim_run(run.id)
        assert claimed is not None
        prepared = self.capabilities.prepare_run(claimed, conversation, self.workspace_b)
        self.assertEqual(prepared.request["capability_manifest"]["skills"], [])

    def test_schema_v6_migrates_capability_tables_without_losing_state(self) -> None:
        with self.store._connect() as connection:
            connection.execute("UPDATE metadata SET value='6' WHERE key='schema_version'")
            for table in ("skills", "mcp_servers", "capability_bindings", "capability_manifests"):
                connection.execute(f"DROP TABLE {table}")
        self.store.initialize()
        with self.store._connect() as connection:
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertEqual(version, "7")
        self.assertTrue({"skills", "mcp_servers", "capability_bindings", "capability_manifests"} <= tables)


if __name__ == "__main__":
    unittest.main()
