"""Tests for Phase 4A: Learning Studio API.

Validates the full set of Learning Studio API endpoints:
 1. GET  /api/learning/overview returns stats, mode, curator info.
 2. GET  /api/learning/events returns filtered audit trail.
 3. GET  /api/learning/proposals lists proposals with status filter.
 4. GET  /api/learning/proposals/{id} returns full proposal detail.
 5. POST /api/learning/proposals/{id}/approve applies the operation.
 6. POST /api/learning/proposals/{id}/reject resolves as rejected.
 7. Approve detects base revision conflict and returns 409.
 8. Approve/reject on non-pending proposal returns 409.
 9. GET  /api/learning/skills lists skills with telemetry stats.
10. GET  /api/learning/skills/{id} returns detail with content.
11. GET  /api/learning/skills/{id}/revisions returns revision history.
12. GET  /api/learning/skills/{id}/runs returns run usage data.
13. POST /api/learning/skills/{id}/pin toggles pin.
14. POST /api/learning/skills/{id}/unpin toggles unpin.
15. POST /api/learning/skills/{id}/archive archives a skill.
16. POST /api/learning/skills/{id}/restore restores an archived skill.
17. POST /api/learning/skills/{id}/rollback creates new revision.
18. GET  /api/learning/memory lists memories.
19. PATCH /api/learning/memory/{id} updates content.
20. DELETE /api/learning/memory/{id} removes a memory.
21. GET  /api/learning/settings returns current config.
22. PATCH /api/learning/settings accepts runtime changes.
23. Non-existent proposal returns 404.
24. Non-existent skill returns 404.
25. Non-existent memory returns 404.
26. Overview with no skills still returns valid structure.
27. Store update_memory and delete_memory work correctly.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx

from gravityclaw.api import Settings, create_app
from gravityclaw.store import Store


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_app(tmp: Path) -> Any:
    """Create a test app with learning enabled."""
    return create_app(Settings(home=tmp / "data", mode="fake"))


def _make_store(tmp: Path) -> Store:
    store = Store(tmp / "gravityclaw.db")
    store.initialize()
    return store


# ─────────────────────────────────────────────────────────────────────────────
# API Integration Tests (async)
# ─────────────────────────────────────────────────────────────────────────────


class LearningStudioAPITests(unittest.IsolatedAsyncioTestCase):
    """Integration tests for Phase 4A Learning Studio API endpoints."""

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gravityclaw-phase4a-")
        root = Path(self.temporary.name)
        self.app = _make_app(root)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )
        # Access internal services for setup
        self.store: Store = self.app.state.store
        self.skill_service = self.app.state.skill_service

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.temporary.cleanup()

    # ─── Test 1: Overview endpoint ────────────────────────────────────────────

    async def test_overview_returns_structure(self) -> None:
        """Test 1: GET /api/learning/overview returns stats and mode."""
        response = await self.client.get("/api/learning/overview")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("enabled", data)
        self.assertIn("trust_mode", data)
        self.assertIn("stats", data)
        self.assertIn("curator", data)
        self.assertIn("memories", data["stats"])
        self.assertIn("skills", data["stats"])
        self.assertIn("pending_proposals", data["stats"])
        self.assertIn("success_rate", data["stats"])
        self.assertIn("corrections", data["stats"])

    # ─── Test 2: Events endpoint ──────────────────────────────────────────────

    async def test_events_returns_list(self) -> None:
        """Test 2: GET /api/learning/events returns filtered audit events."""
        response = await self.client.get("/api/learning/events")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)

    # ─── Test 3: Proposals list ───────────────────────────────────────────────

    async def test_proposals_list_empty(self) -> None:
        """Test 3: GET /api/learning/proposals returns empty list when none exist."""
        response = await self.client.get("/api/learning/proposals")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    async def test_proposals_list_with_filter(self) -> None:
        """Test 3b: GET /api/learning/proposals?status=pending filters correctly."""
        # Create a proposal
        self.skill_service.registry.create_proposal(
            skill_name="test-skill", operation="create",
            description="A test skill", reason="Testing",
            content="# Test\nStep 1", confidence=0.9,
        )
        response = await self.client.get("/api/learning/proposals?status=pending")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["skill_name"], "test-skill")
        self.assertEqual(data[0]["status"], "pending")

    # ─── Test 4: Proposal detail ──────────────────────────────────────────────

    async def test_proposal_detail(self) -> None:
        """Test 4: GET /api/learning/proposals/{id} returns full detail."""
        proposal = self.skill_service.registry.create_proposal(
            skill_name="detail-skill", operation="create",
            description="Detail test", reason="Testing detail view",
            content="# Detail\nContent here", confidence=0.85,
            source_run_id="run-123", review_model="gemini-flash",
        )
        response = await self.client.get(f"/api/learning/proposals/{proposal.id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["id"], proposal.id)
        self.assertEqual(data["skill_name"], "detail-skill")
        self.assertEqual(data["reason"], "Testing detail view")
        self.assertEqual(data["confidence"], 0.85)
        self.assertEqual(data["source_run_id"], "run-123")
        self.assertEqual(data["review_model"], "gemini-flash")

    # ─── Test 5: Approve proposal ─────────────────────────────────────────────

    async def test_approve_create_proposal(self) -> None:
        """Test 5: POST /api/learning/proposals/{id}/approve creates the skill."""
        proposal = self.skill_service.registry.create_proposal(
            skill_name="approve-test", operation="create",
            description="Approved skill", reason="Approve test",
            content="# Approved\nStep 1", confidence=0.95,
        )
        response = await self.client.post(
            f"/api/learning/proposals/{proposal.id}/approve",
            json={"reason": "Looks good"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "approved")
        # Verify skill was created
        skill = self.skill_service.get("approve-test")
        self.assertIsNotNone(skill)

    # ─── Test 6: Reject proposal ──────────────────────────────────────────────

    async def test_reject_proposal(self) -> None:
        """Test 6: POST /api/learning/proposals/{id}/reject resolves as rejected."""
        proposal = self.skill_service.registry.create_proposal(
            skill_name="reject-test", operation="create",
            description="Rejected skill", reason="Reject test",
            content="# Rejected\nStep 1", confidence=0.6,
        )
        response = await self.client.post(
            f"/api/learning/proposals/{proposal.id}/reject",
            json={"reason": "Not useful"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "rejected")

    # ─── Test 7: Approve with revision conflict ───────────────────────────────

    async def test_approve_revision_conflict(self) -> None:
        """Test 7: Approve detects base revision mismatch and returns 409."""
        # Create a skill first
        skill = self.skill_service.create(
            "conflict-test", "Conflict test", "# V1\nContent",
            source_run_id="run-1",
        )
        # Create a patch proposal targeting revision 1
        proposal = self.skill_service.registry.create_proposal(
            skill_name="conflict-test", operation="patch",
            description="Patch", reason="Improvement",
            content="# V2\nImproved", confidence=0.9,
            skill_id=skill.skill_id, base_revision=1,
        )
        # Advance the skill to revision 2 (simulating concurrent patch)
        self.skill_service.patch("conflict-test", "# V2b\nConcurrent", "concurrent edit")
        # Now approve should fail with conflict
        response = await self.client.post(
            f"/api/learning/proposals/{proposal.id}/approve",
            json={},
        )
        self.assertEqual(response.status_code, 409)

    # ─── Test 8: Approve/reject already resolved ──────────────────────────────

    async def test_approve_already_resolved(self) -> None:
        """Test 8: Approve/reject on non-pending proposal returns 409."""
        proposal = self.skill_service.registry.create_proposal(
            skill_name="resolved-test", operation="create",
            description="Resolved", reason="Test",
            content="# X", confidence=0.9,
        )
        # Reject it first
        self.skill_service.registry.resolve_proposal(proposal.id, "rejected")
        # Now try to approve
        response = await self.client.post(
            f"/api/learning/proposals/{proposal.id}/approve",
            json={},
        )
        self.assertEqual(response.status_code, 409)

    # ─── Test 9: Skills list ──────────────────────────────────────────────────

    async def test_skills_list_with_stats(self) -> None:
        """Test 9: GET /api/learning/skills returns skills with telemetry stats."""
        skill = self.skill_service.create(
            "stats-test", "Stats test skill", "# Stats\nContent",
        )
        # Record some telemetry
        self.skill_service.record_skill_execution(skill.skill_id, success=True)
        self.skill_service.record_skill_execution(skill.skill_id, success=True)
        self.skill_service.record_skill_execution(skill.skill_id, success=False)

        response = await self.client.get("/api/learning/skills")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(len(data) >= 1)
        found = next((s for s in data if s["name"] == "stats-test"), None)
        self.assertIsNotNone(found)
        self.assertEqual(found["stats"]["executed"], 3)
        self.assertEqual(found["stats"]["successful"], 2)
        self.assertEqual(found["stats"]["failed"], 1)
        self.assertAlmostEqual(found["stats"]["success_rate"], 66.7, places=0)

    # ─── Test 10: Skill detail ────────────────────────────────────────────────

    async def test_skill_detail(self) -> None:
        """Test 10: GET /api/learning/skills/{id} returns content and stats."""
        skill = self.skill_service.create(
            "detail-test", "Detail skill", "# Detail Content\nHello",
        )
        response = await self.client.get(f"/api/learning/skills/{skill.skill_id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["name"], "detail-test")
        self.assertIn("content", data)
        self.assertIn("stats", data)

    # ─── Test 11: Skill revisions ─────────────────────────────────────────────

    async def test_skill_revisions(self) -> None:
        """Test 11: GET /api/learning/skills/{id}/revisions returns history."""
        skill = self.skill_service.create(
            "rev-test", "Revision test", "# V1",
        )
        self.skill_service.patch("rev-test", "# V2", "second revision")

        response = await self.client.get(f"/api/learning/skills/{skill.skill_id}/revisions")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 2)
        # Most recent first
        self.assertEqual(data[0]["revision"], 2)
        self.assertEqual(data[1]["revision"], 1)

    # ─── Test 12: Skill runs ─────────────────────────────────────────────────

    async def test_skill_runs(self) -> None:
        """Test 12: GET /api/learning/skills/{id}/runs returns run telemetry."""
        skill = self.skill_service.create(
            "run-test", "Run test", "# Run\nContent",
        )
        # Record telemetry with run_id
        self.skill_service.record_skill_execution(skill.skill_id, run_id="run-abc", success=True)
        self.skill_service.record_skill_execution(skill.skill_id, run_id="run-def", success=False)

        response = await self.client.get(f"/api/learning/skills/{skill.skill_id}/runs")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(len(data) >= 2)
        run_ids = {item["run_id"] for item in data}
        self.assertIn("run-abc", run_ids)
        self.assertIn("run-def", run_ids)

    # ─── Test 13: Pin skill ───────────────────────────────────────────────────

    async def test_pin_skill(self) -> None:
        """Test 13: POST /api/learning/skills/{id}/pin sets pinned=true."""
        skill = self.skill_service.create("pin-test", "Pin test", "# Pin")
        self.assertFalse(skill.pinned)

        response = await self.client.post(f"/api/learning/skills/{skill.skill_id}/pin")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["pinned"])

    # ─── Test 14: Unpin skill ─────────────────────────────────────────────────

    async def test_unpin_skill(self) -> None:
        """Test 14: POST /api/learning/skills/{id}/unpin sets pinned=false."""
        skill = self.skill_service.create("unpin-test", "Unpin test", "# Unpin")
        self.skill_service.pin("unpin-test")

        response = await self.client.post(f"/api/learning/skills/{skill.skill_id}/unpin")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["pinned"])

    # ─── Test 15: Archive skill ───────────────────────────────────────────────

    async def test_archive_skill(self) -> None:
        """Test 15: POST /api/learning/skills/{id}/archive soft-deletes."""
        skill = self.skill_service.create("archive-test", "Archive test", "# Archive")

        response = await self.client.post(f"/api/learning/skills/{skill.skill_id}/archive")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["state"], "archived")

    # ─── Test 16: Restore skill ───────────────────────────────────────────────

    async def test_restore_skill(self) -> None:
        """Test 16: POST /api/learning/skills/{id}/restore reactivates."""
        skill = self.skill_service.create("restore-test", "Restore test", "# Restore")
        self.skill_service.archive("restore-test")

        response = await self.client.post(f"/api/learning/skills/{skill.skill_id}/restore")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["state"], "active")

    # ─── Test 17: Rollback skill ──────────────────────────────────────────────

    async def test_rollback_skill(self) -> None:
        """Test 17: POST /api/learning/skills/{id}/rollback creates new revision."""
        skill = self.skill_service.create("rollback-test", "Rollback", "# V1")
        self.skill_service.patch("rollback-test", "# V2", "changed")
        current = self.skill_service.registry.get_skill(skill.skill_id)
        self.assertEqual(current.revision, 2)

        response = await self.client.post(
            f"/api/learning/skills/{skill.skill_id}/rollback",
            json={"target_revision": 1, "reason": "revert to v1"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["revision"], 3)  # Rollback creates rev 3

    # ─── Test 18: Memory list ─────────────────────────────────────────────────

    async def test_memory_list(self) -> None:
        """Test 18: GET /api/learning/memory lists memories."""
        from gravityclaw.memory import MemoryService
        mem_service = MemoryService(Path(self.temporary.name) / "data", self.store)
        mem_service.record_episode("Test memory content", source="test")

        response = await self.client.get("/api/learning/memory")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)

    # ─── Test 19: Memory update ───────────────────────────────────────────────

    async def test_memory_update(self) -> None:
        """Test 19: PATCH /api/learning/memory/{id} updates content."""
        from gravityclaw.memory import MemoryService
        mem_service = MemoryService(Path(self.temporary.name) / "data", self.store)
        mem_id = mem_service.record_episode("Original content", source="test")

        response = await self.client.patch(
            f"/api/learning/memory/{mem_id}",
            json={"content": "Updated content"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["content"], "Updated content")

    # ─── Test 20: Memory delete ───────────────────────────────────────────────

    async def test_memory_delete(self) -> None:
        """Test 20: DELETE /api/learning/memory/{id} removes record."""
        from gravityclaw.memory import MemoryService
        mem_service = MemoryService(Path(self.temporary.name) / "data", self.store)
        mem_id = mem_service.record_episode("To be deleted", source="test")

        response = await self.client.delete(f"/api/learning/memory/{mem_id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["deleted"])

        # Verify it's gone
        response = await self.client.patch(
            f"/api/learning/memory/{mem_id}",
            json={"content": "Should fail"},
        )
        self.assertEqual(response.status_code, 404)

    # ─── Test 21: Settings GET ────────────────────────────────────────────────

    async def test_settings_get(self) -> None:
        """Test 21: GET /api/learning/settings returns full config."""
        response = await self.client.get("/api/learning/settings")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("enabled", data)
        self.assertIn("skills", data)
        self.assertIn("trust_mode", data["skills"])
        self.assertIn("reviewer", data)
        self.assertIn("curator", data)
        self.assertIn("notifications", data)

    # ─── Test 22: Settings PATCH ──────────────────────────────────────────────

    async def test_settings_patch(self) -> None:
        """Test 22: PATCH /api/learning/settings accepts runtime changes."""
        response = await self.client.patch(
            "/api/learning/settings",
            json={"enabled": False, "trust_mode": "autonomous"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("runtime_changes_pending", data)
        self.assertEqual(data["runtime_changes_pending"]["enabled"], False)
        self.assertEqual(data["runtime_changes_pending"]["trust_mode"], "autonomous")

    # ─── Test 23: Non-existent proposal ───────────────────────────────────────

    async def test_proposal_404(self) -> None:
        """Test 23: Non-existent proposal returns 404."""
        response = await self.client.get("/api/learning/proposals/nonexistent-id")
        self.assertEqual(response.status_code, 404)

    # ─── Test 24: Non-existent skill ──────────────────────────────────────────

    async def test_skill_404(self) -> None:
        """Test 24: Non-existent skill returns 404."""
        response = await self.client.get("/api/learning/skills/nonexistent-id")
        self.assertEqual(response.status_code, 404)

    # ─── Test 25: Non-existent memory ─────────────────────────────────────────

    async def test_memory_404(self) -> None:
        """Test 25: Non-existent memory returns 404."""
        response = await self.client.patch(
            "/api/learning/memory/nonexistent-id",
            json={"content": "test"},
        )
        self.assertEqual(response.status_code, 404)

        response = await self.client.delete("/api/learning/memory/nonexistent-id")
        self.assertEqual(response.status_code, 404)

    # ─── Test 26: Overview with no data ───────────────────────────────────────

    async def test_overview_empty_state(self) -> None:
        """Test 26: Overview with no skills returns valid structure with zeros."""
        response = await self.client.get("/api/learning/overview")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["enabled"])
        self.assertEqual(data["stats"]["skills"], 0)
        self.assertEqual(data["stats"]["pending_proposals"], 0)
        self.assertIsNone(data["stats"]["success_rate"])


# ─────────────────────────────────────────────────────────────────────────────
# Store unit tests for new methods
# ─────────────────────────────────────────────────────────────────────────────


class StoreMemoryMutationTests(unittest.TestCase):
    """Test 27: Store update_memory and delete_memory work correctly."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gravityclaw-store-")
        self.store = _make_store(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _insert_memory(self, content: str = "test") -> str:
        """Insert a memory directly for testing."""
        import uuid
        from gravityclaw.store import utc_now
        memory_id = str(uuid.uuid4())
        with self.store._connect() as conn:
            conn.execute(
                """INSERT INTO memories(id, kind, content, source, confidence, created_at, updated_at)
                   VALUES(?, 'episodic', ?, 'test', 1.0, ?, ?)""",
                (memory_id, content, utc_now(), utc_now()),
            )
        return memory_id

    def test_update_memory_changes_content(self) -> None:
        memory_id = self._insert_memory("original")
        self.store.update_memory(memory_id, content="updated")
        record = self.store.get_memory(memory_id)
        self.assertEqual(record["content"], "updated")

    def test_update_nonexistent_memory_raises(self) -> None:
        with self.assertRaises(KeyError):
            self.store.update_memory("nonexistent", content="x")

    def test_delete_memory_removes_record(self) -> None:
        memory_id = self._insert_memory("to-delete")
        self.store.delete_memory(memory_id)
        with self.assertRaises(KeyError):
            self.store.get_memory(memory_id)

    def test_delete_nonexistent_memory_raises(self) -> None:
        with self.assertRaises(KeyError):
            self.store.delete_memory("nonexistent")


if __name__ == "__main__":
    unittest.main()
