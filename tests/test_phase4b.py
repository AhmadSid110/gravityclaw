"""Tests for Phase 4B: Journey Graph API.

Validates the provenance graph endpoint that aggregates learning lifecycle
data into a nodes/edges structure for the Journey visualization:

 1. GET /api/learning/journey returns empty graph when no skills exist.
 2. GET /api/learning/journey returns skill node after skill registration.
 3. GET /api/learning/journey returns revision nodes linked to skill.
 4. GET /api/learning/journey returns proposal nodes linked to skill and source run.
 5. GET /api/learning/journey returns usage-based run nodes with correct edge relations.
 6. GET /api/learning/journey?skill_id= scopes to a single skill.
 7. GET /api/learning/journey?skill_id= returns 404 for non-existent skill.
 8. Journey graph has correct stats summary.
 9. Multiple skills produce a combined graph.
10. Revision parent chain produces evolves_to edges.
11. Correction usage event produces corrects edge.
12. Proposal with source_run_id produces generates_proposal edge.
13. Approved proposal linked to revision produces approved_as edge.
14. Full lifecycle: run → proposal → approve → revision → reuse → success → correct → improve.
"""

from __future__ import annotations

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


# ─────────────────────────────────────────────────────────────────────────────
# Journey Graph API Tests
# ─────────────────────────────────────────────────────────────────────────────


class JourneyGraphAPITests(unittest.IsolatedAsyncioTestCase):
    """Integration tests for Phase 4B Journey Graph API endpoint."""

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gravityclaw-phase4b-")
        root = Path(self.temporary.name)
        self.app = _make_app(root)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )
        self.store: Store = self.app.state.store
        self.skill_service = self.app.state.skill_service

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.temporary.cleanup()

    # ─── Test 1: Empty graph ──────────────────────────────────────────────────

    async def test_journey_empty_graph(self) -> None:
        """Test 1: Returns empty graph structure when no skills exist."""
        response = await self.client.get("/api/learning/journey")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("nodes", data)
        self.assertIn("edges", data)
        self.assertIn("stats", data)
        self.assertEqual(data["nodes"], [])
        self.assertEqual(data["edges"], [])
        self.assertEqual(data["stats"]["total_nodes"], 0)
        self.assertEqual(data["stats"]["total_edges"], 0)
        self.assertEqual(data["stats"]["by_kind"], {})

    # ─── Test 2: Skill node appears ──────────────────────────────────────────

    async def test_journey_skill_node(self) -> None:
        """Test 2: Registered skill appears as a node."""
        skill = self.skill_service.registry.register_skill(
            name="test-skill",
            description="A test skill",
            path="skills/test-skill",
        )
        response = await self.client.get("/api/learning/journey")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        skill_nodes = [n for n in data["nodes"] if n["kind"] == "skill"]
        self.assertEqual(len(skill_nodes), 1)
        self.assertEqual(skill_nodes[0]["id"], f"skill:{skill.skill_id}")
        self.assertEqual(skill_nodes[0]["label"], "test-skill")
        self.assertEqual(skill_nodes[0]["description"], "A test skill")
        self.assertEqual(data["stats"]["by_kind"]["skill"], 1)

    # ─── Test 3: Revision nodes ──────────────────────────────────────────────

    async def test_journey_revision_nodes(self) -> None:
        """Test 3: Revisions appear as nodes linked to skill."""
        skill = self.skill_service.registry.register_skill(
            name="rev-skill", description="Has revisions", path="skills/rev-skill",
        )
        rev = self.skill_service.registry.record_revision(
            skill_id=skill.skill_id, revision=1, operation="create",
            reason="initial creation",
        )
        response = await self.client.get("/api/learning/journey")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        rev_nodes = [n for n in data["nodes"] if n["kind"] == "revision"]
        self.assertEqual(len(rev_nodes), 1)
        self.assertEqual(rev_nodes[0]["id"], f"revision:{rev.id}")
        self.assertIn("Rev 1", rev_nodes[0]["label"])
        # Check edge: revision produces skill
        produces_edges = [e for e in data["edges"] if e["relation"] == "produces"]
        self.assertTrue(
            any(e["source"] == f"revision:{rev.id}" and e["target"] == f"skill:{skill.skill_id}"
                for e in produces_edges)
        )

    # ─── Test 4: Proposal nodes with source run ──────────────────────────────

    async def test_journey_proposal_nodes(self) -> None:
        """Test 4: Proposals appear as nodes linked to skill and source run."""
        skill = self.skill_service.registry.register_skill(
            name="prop-skill", description="Has proposals", path="skills/prop-skill",
        )
        proposal = self.skill_service.registry.create_proposal(
            skill_name="prop-skill",
            operation="patch",
            description="improve handling",
            reason="observed failure",
            content="new content",
            confidence=0.85,
            skill_id=skill.skill_id,
            source_run_id="run-abc-123",
        )
        response = await self.client.get("/api/learning/journey")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        prop_nodes = [n for n in data["nodes"] if n["kind"] == "proposal"]
        self.assertEqual(len(prop_nodes), 1)
        self.assertEqual(prop_nodes[0]["status"], "pending")
        self.assertAlmostEqual(prop_nodes[0]["confidence"], 0.85)
        # Run node created for source_run_id
        run_nodes = [n for n in data["nodes"] if n["kind"] == "run"]
        self.assertTrue(any(n["run_id"] == "run-abc-123" for n in run_nodes))
        # Edge: run generates_proposal
        gen_edges = [e for e in data["edges"] if e["relation"] == "generates_proposal"]
        self.assertTrue(
            any(e["source"] == "run:run-abc-123" and e["target"] == f"proposal:{proposal.id}"
                for e in gen_edges)
        )
        # Edge: proposal targets skill
        target_edges = [e for e in data["edges"] if e["relation"] == "targets"]
        self.assertTrue(
            any(e["source"] == f"proposal:{proposal.id}" and e["target"] == f"skill:{skill.skill_id}"
                for e in target_edges)
        )

    # ─── Test 5: Usage events create run nodes with edges ────────────────────

    async def test_journey_usage_run_nodes(self) -> None:
        """Test 5: Skill usage events produce run nodes with correct edge relations."""
        skill = self.skill_service.registry.register_skill(
            name="used-skill", description="Gets used", path="skills/used-skill",
        )
        # Record usage events
        self.skill_service.registry.record_usage(
            skill_id=skill.skill_id, run_id="run-use-1", event="executed",
        )
        self.skill_service.registry.record_usage(
            skill_id=skill.skill_id, run_id="run-use-2", event="successful",
        )
        response = await self.client.get("/api/learning/journey")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        run_nodes = [n for n in data["nodes"] if n["kind"] == "run"]
        run_ids = {n["run_id"] for n in run_nodes}
        self.assertIn("run-use-1", run_ids)
        self.assertIn("run-use-2", run_ids)
        # used_in edge for executed
        used_edges = [e for e in data["edges"] if e["relation"] == "used_in"]
        self.assertTrue(
            any(e["source"] == f"skill:{skill.skill_id}" and e["target"] == "run:run-use-1"
                for e in used_edges)
        )
        # validates edge for successful
        val_edges = [e for e in data["edges"] if e["relation"] == "validates"]
        self.assertTrue(
            any(e["source"] == "run:run-use-2" and e["target"] == f"skill:{skill.skill_id}"
                for e in val_edges)
        )

    # ─── Test 6: Skill-scoped journey ────────────────────────────────────────

    async def test_journey_skill_scoped(self) -> None:
        """Test 6: ?skill_id= scopes the graph to one skill."""
        skill_a = self.skill_service.registry.register_skill(
            name="skill-a", description="Skill A", path="skills/skill-a",
        )
        self.skill_service.registry.register_skill(
            name="skill-b", description="Skill B", path="skills/skill-b",
        )
        response = await self.client.get(f"/api/learning/journey?skill_id={skill_a.skill_id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        skill_nodes = [n for n in data["nodes"] if n["kind"] == "skill"]
        self.assertEqual(len(skill_nodes), 1)
        self.assertEqual(skill_nodes[0]["label"], "skill-a")

    # ─── Test 7: Skill not found ─────────────────────────────────────────────

    async def test_journey_skill_not_found(self) -> None:
        """Test 7: Non-existent skill_id returns 404."""
        response = await self.client.get("/api/learning/journey?skill_id=nonexistent-id")
        self.assertEqual(response.status_code, 404)

    # ─── Test 8: Stats summary ───────────────────────────────────────────────

    async def test_journey_stats_summary(self) -> None:
        """Test 8: Stats correctly count nodes and edges by kind."""
        skill = self.skill_service.registry.register_skill(
            name="stats-skill", description="For stats", path="skills/stats-skill",
        )
        self.skill_service.registry.record_revision(
            skill_id=skill.skill_id, revision=1, operation="create",
            reason="initial", source_run_id="run-stats-1",
        )
        self.skill_service.registry.create_proposal(
            skill_name="stats-skill", operation="patch",
            description="improve", reason="test", content="new",
            confidence=0.9, skill_id=skill.skill_id,
        )
        response = await self.client.get("/api/learning/journey")
        data = response.json()
        stats = data["stats"]
        self.assertGreaterEqual(stats["total_nodes"], 3)  # skill + revision + run (from source_run_id)
        self.assertGreaterEqual(stats["total_edges"], 2)  # produces + triggers_creation
        self.assertEqual(stats["by_kind"]["skill"], 1)
        self.assertEqual(stats["by_kind"]["revision"], 1)
        self.assertIn("run", stats["by_kind"])

    # ─── Test 9: Multiple skills combined ────────────────────────────────────

    async def test_journey_multiple_skills(self) -> None:
        """Test 9: Multiple skills produce a combined graph."""
        self.skill_service.registry.register_skill(
            name="multi-a", description="First", path="skills/multi-a",
        )
        self.skill_service.registry.register_skill(
            name="multi-b", description="Second", path="skills/multi-b",
        )
        self.skill_service.registry.register_skill(
            name="multi-c", description="Third", path="skills/multi-c",
        )
        response = await self.client.get("/api/learning/journey")
        data = response.json()
        skill_nodes = [n for n in data["nodes"] if n["kind"] == "skill"]
        self.assertEqual(len(skill_nodes), 3)

    # ─── Test 10: Revision parent chain evolves_to edges ─────────────────────

    async def test_journey_revision_chain(self) -> None:
        """Test 10: Revision parent chain produces evolves_to edges."""
        skill = self.skill_service.registry.register_skill(
            name="chain-skill", description="Chain test", path="skills/chain-skill",
        )
        rev1 = self.skill_service.registry.record_revision(
            skill_id=skill.skill_id, revision=1, operation="create",
            reason="initial",
        )
        rev2 = self.skill_service.registry.record_revision(
            skill_id=skill.skill_id, revision=2, operation="patch",
            reason="improvement", parent_revision=1,
        )
        response = await self.client.get("/api/learning/journey")
        data = response.json()
        evolves_edges = [e for e in data["edges"] if e["relation"] == "evolves_to"]
        self.assertTrue(
            any(e["source"] == f"revision:{rev1.id}" and e["target"] == f"revision:{rev2.id}"
                for e in evolves_edges)
        )

    # ─── Test 11: Correction usage ──────────────────────────────────────────

    async def test_journey_correction_edge(self) -> None:
        """Test 11: Corrected usage event produces corrects edge."""
        skill = self.skill_service.registry.register_skill(
            name="corrected-skill", description="Gets corrected", path="skills/corrected-skill",
        )
        self.skill_service.registry.record_usage(
            skill_id=skill.skill_id, run_id="run-correct-1", event="corrected",
        )
        response = await self.client.get("/api/learning/journey")
        data = response.json()
        correct_edges = [e for e in data["edges"] if e["relation"] == "corrects"]
        self.assertTrue(
            any(e["source"] == "run:run-correct-1" and e["target"] == f"skill:{skill.skill_id}"
                for e in correct_edges)
        )

    # ─── Test 12: Proposal generates_proposal edge ──────────────────────────

    async def test_journey_proposal_generates_edge(self) -> None:
        """Test 12: Proposal with source_run_id has generates_proposal edge."""
        skill = self.skill_service.registry.register_skill(
            name="gen-skill", description="Generation test", path="skills/gen-skill",
        )
        proposal = self.skill_service.registry.create_proposal(
            skill_name="gen-skill", operation="create",
            description="new skill from experience", reason="learned",
            content="procedure content", confidence=0.92,
            skill_id=skill.skill_id, source_run_id="run-gen-source",
        )
        response = await self.client.get("/api/learning/journey")
        data = response.json()
        gen_edges = [e for e in data["edges"] if e["relation"] == "generates_proposal"]
        self.assertTrue(
            any(e["source"] == "run:run-gen-source" and e["target"] == f"proposal:{proposal.id}"
                for e in gen_edges)
        )

    # ─── Test 13: Approved proposal → revision link ──────────────────────────

    async def test_journey_approved_proposal_revision_link(self) -> None:
        """Test 13: Approved proposal linked to revision via approved_as edge."""
        skill = self.skill_service.registry.register_skill(
            name="approve-skill", description="Approval test", path="skills/approve-skill",
        )
        proposal = self.skill_service.registry.create_proposal(
            skill_name="approve-skill", operation="create",
            description="new", reason="learned", content="content",
            confidence=0.88, skill_id=skill.skill_id,
        )
        self.skill_service.registry.resolve_proposal(proposal.id, "approved")
        rev = self.skill_service.registry.record_revision(
            skill_id=skill.skill_id, revision=1, operation="create",
            reason="from proposal", proposal_id=proposal.id,
        )
        response = await self.client.get("/api/learning/journey")
        data = response.json()
        approved_edges = [e for e in data["edges"] if e["relation"] == "approved_as"]
        self.assertTrue(
            any(e["source"] == f"proposal:{proposal.id}" and e["target"] == f"revision:{rev.id}"
                for e in approved_edges)
        )

    # ─── Test 14: Full lifecycle ─────────────────────────────────────────────

    async def test_journey_full_lifecycle(self) -> None:
        """Test 14: Complete learning lifecycle produces correct graph structure.

        Lifecycle: run → proposal → approve → revision → reuse → success → correct → improve
        """
        # Step 1: Experience (source run triggers a create proposal)
        skill = self.skill_service.registry.register_skill(
            name="lifecycle-skill", description="Full lifecycle", path="skills/lifecycle-skill",
        )
        create_proposal = self.skill_service.registry.create_proposal(
            skill_name="lifecycle-skill", operation="create",
            description="learned from experience", reason="observed pattern",
            content="initial procedure", confidence=0.9,
            skill_id=skill.skill_id, source_run_id="run-experience-1",
        )

        # Step 2: Approve → revision
        self.skill_service.registry.resolve_proposal(create_proposal.id, "approved")
        rev1 = self.skill_service.registry.record_revision(
            skill_id=skill.skill_id, revision=1, operation="create",
            reason="approved proposal", proposal_id=create_proposal.id,
            source_run_id="run-experience-1",
        )
        self.skill_service.registry.update_skill(skill.skill_id, revision=1)

        # Step 3: Reuse in another run
        self.skill_service.registry.record_usage(
            skill_id=skill.skill_id, run_id="run-reuse-1", event="executed",
        )
        self.skill_service.registry.record_usage(
            skill_id=skill.skill_id, run_id="run-reuse-1", event="successful",
        )

        # Step 4: Correction detected
        self.skill_service.registry.record_usage(
            skill_id=skill.skill_id, run_id="run-correct-1", event="executed",
        )
        self.skill_service.registry.record_usage(
            skill_id=skill.skill_id, run_id="run-correct-1", event="corrected",
        )

        # Step 5: Improvement proposal from correction
        patch_proposal = self.skill_service.registry.create_proposal(
            skill_name="lifecycle-skill", operation="patch",
            description="fix edge case", reason="observed correction",
            content="improved procedure", confidence=0.87,
            skill_id=skill.skill_id, source_run_id="run-correct-1",
            base_revision=1,
        )
        self.skill_service.registry.resolve_proposal(patch_proposal.id, "approved")
        rev2 = self.skill_service.registry.record_revision(
            skill_id=skill.skill_id, revision=2, operation="patch",
            reason="correction-driven improvement", parent_revision=1,
            proposal_id=patch_proposal.id, source_run_id="run-correct-1",
        )

        # Step 6: Reuse improved version
        self.skill_service.registry.record_usage(
            skill_id=skill.skill_id, run_id="run-reuse-improved", event="executed",
        )
        self.skill_service.registry.record_usage(
            skill_id=skill.skill_id, run_id="run-reuse-improved", event="successful",
        )

        # Fetch the journey
        response = await self.client.get(f"/api/learning/journey?skill_id={skill.skill_id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Verify structure
        node_kinds = {n["kind"] for n in data["nodes"]}
        self.assertIn("skill", node_kinds)
        self.assertIn("revision", node_kinds)
        self.assertIn("proposal", node_kinds)
        self.assertIn("run", node_kinds)

        # Verify key edges exist
        edge_relations = {e["relation"] for e in data["edges"]}
        self.assertIn("generates_proposal", edge_relations)  # run → proposal
        self.assertIn("approved_as", edge_relations)          # proposal → revision
        self.assertIn("produces", edge_relations)             # revision → skill
        self.assertIn("used_in", edge_relations)              # skill → run (reuse)
        self.assertIn("validates", edge_relations)            # run → skill (success)
        self.assertIn("corrects", edge_relations)             # run → skill (correction)
        self.assertIn("triggers_creation", edge_relations)    # run → revision (create)
        self.assertIn("triggers_improvement", edge_relations) # run → revision (patch)
        self.assertIn("evolves_to", edge_relations)           # rev1 → rev2
        self.assertIn("targets", edge_relations)              # proposal → skill

        # Verify node counts
        skill_nodes = [n for n in data["nodes"] if n["kind"] == "skill"]
        self.assertEqual(len(skill_nodes), 1)
        rev_nodes = [n for n in data["nodes"] if n["kind"] == "revision"]
        self.assertEqual(len(rev_nodes), 2)
        prop_nodes = [n for n in data["nodes"] if n["kind"] == "proposal"]
        self.assertEqual(len(prop_nodes), 2)
        run_nodes = [n for n in data["nodes"] if n["kind"] == "run"]
        # Runs: experience-1, reuse-1, correct-1, reuse-improved (4 unique)
        run_ids = {n["run_id"] for n in run_nodes}
        self.assertIn("run-experience-1", run_ids)
        self.assertIn("run-reuse-1", run_ids)
        self.assertIn("run-correct-1", run_ids)
        self.assertIn("run-reuse-improved", run_ids)

        # Stats sanity
        self.assertGreaterEqual(data["stats"]["total_nodes"], 9)
        self.assertGreaterEqual(data["stats"]["total_edges"], 10)


if __name__ == "__main__":
    unittest.main()
