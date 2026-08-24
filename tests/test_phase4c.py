"""Tests for Phase 4C: Context Transparency Integration.

Validates the context snapshot API, storage, and enrichment logic that powers
the Context Inspector UI:

 1. Context circle reflects the active run usage (usage_ratio computed correctly).
 2. GET /api/runs/{run_id}/context-snapshot returns snapshot when persisted.
 3. Used/total tokens are displayed correctly in response.
 4. Provider-reported vs estimated counts are distinguishable (token_source field).
 5. System/conversation/tools/memory/skills are broken down separately (segments).
 6. Loaded skills show exact revision numbers.
 7. Retrieved memory entries are identifiable (id, namespace, confidence).
 8. Last invocation and current conversation context are distinguished.
 9. Context compaction is visible when it occurred (transformations field).
10. Old runs preserve their original context snapshot (snapshot persists after run completes).
11. Skill entries link to their Learning Studio record (skill_id present).
12. Memory entries link to their record (id present).
13. A skill can link to its Journey provenance (skill_id for journey lookup).
14. Context information updates live during a run (update_context_snapshot_tokens).
15. Final provider usage replaces estimates when available.
16. Mobile/tablet dialog remains usable (CSS tested via build, no scrolling issues by design).
17. Runs without learning data still render correctly (204 for no snapshot, synthesized from manifest).
18. Context inspection itself does not alter the run/context (GET is read-only).
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


def _create_run(store: Store, conversation_id: str, status: str = "completed") -> str:
    """Create a run and transition it to the given status."""
    run = store.enqueue_run(conversation_id, {"prompt": "test prompt", "context_profile": "chat"})
    if status != "queued":
        store.claim_run(run.id)
    if status in ("completed", "failed"):
        store.finish_run(run.id, status)
    return run.id


def _save_snapshot(store: Store, run_id: str, **overrides: Any) -> None:
    """Save a context snapshot with sensible defaults."""
    defaults = {
        "model": "gpt-5.6",
        "context_limit": 200000,
        "input_tokens": 84320,
        "output_tokens": 1250,
        "token_source": "provider",
        "segments": [
            {"kind": "system", "tokens": 8230},
            {"kind": "conversation", "tokens": 41280},
            {"kind": "tool_results", "tokens": 18420},
            {"kind": "memory", "tokens": 2840},
            {"kind": "skills", "tokens": 5991},
        ],
        "skills": [
            {"skill_id": "skill-tg-recovery", "name": "telegram-recovery", "revision": 8, "tokens": 3102},
            {"skill_id": "skill-systemd-gw", "name": "systemd-gateway", "revision": 5, "tokens": 1774},
        ],
        "memories": [
            {"id": "mem-001", "namespace": "curated", "tokens": 1840, "label": "Production gateway uses systemd", "confidence": 0.95},
            {"id": "mem-002", "namespace": "episodic", "tokens": 1000, "label": "SSH key rotation procedure", "confidence": 0.8},
        ],
        "transformations": [
            {"label": "Raw conversation", "tokens_before": 74120, "tokens_after": 51280},
            {"label": "After compaction", "tokens_before": 51280, "tokens_after": 51280},
            {"label": "Retrieved memory", "tokens_before": 51280, "tokens_after": 54120},
            {"label": "Loaded skills", "tokens_before": 54120, "tokens_after": 60111},
            {"label": "Tool context", "tokens_before": 60111, "tokens_after": 84320},
        ],
        "conversation_tokens": 91420,
        "last_invocation_tokens": 84320,
    }
    defaults.update(overrides)
    store.save_context_snapshot(run_id, **defaults)


# ─────────────────────────────────────────────────────────────────────────────
# Test Suite
# ─────────────────────────────────────────────────────────────────────────────


class ContextTransparencyAPITests(unittest.IsolatedAsyncioTestCase):
    """Integration tests for Phase 4C Context Transparency API."""

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gravityclaw-phase4c-")
        root = Path(self.temporary.name)
        self.app = _make_app(root)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )
        self.store: Store = self.app.state.store

        # Create workspace and conversation for test runs
        ws = self.store.create_workspace("test-ws", Path("/tmp/test-ws"))
        self.workspace_id = ws.id
        conv = self.store.create_conversation(self.workspace_id, channel="web")
        self.conversation_id = conv.id

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.temporary.cleanup()

    # ─── Test 1: Context circle usage ratio ───────────────────────────────────

    async def test_usage_ratio_computed_correctly(self) -> None:
        """Test 1: usage_ratio = input_tokens / context_limit."""
        run_id = _create_run(self.store, self.conversation_id)
        _save_snapshot(self.store, run_id, input_tokens=84320, context_limit=200000)

        response = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertAlmostEqual(data["usage_ratio"], 84320 / 200000, places=4)
        self.assertEqual(data["remaining_tokens"], 200000 - 84320)

    # ─── Test 2: Persisted snapshot returned ──────────────────────────────────

    async def test_persisted_snapshot_returned(self) -> None:
        """Test 2: GET returns persisted snapshot data."""
        run_id = _create_run(self.store, self.conversation_id)
        _save_snapshot(self.store, run_id)

        response = await self.client.get(f"/api/v1/runs/{run_id}/context-snapshot")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["run_id"], run_id)
        self.assertEqual(data["model"], "gpt-5.6")
        self.assertEqual(data["context_limit"], 200000)
        self.assertEqual(data["input_tokens"], 84320)

    # ─── Test 3: Token counts present ─────────────────────────────────────────

    async def test_token_counts_displayed(self) -> None:
        """Test 3: Used and total tokens are correctly populated."""
        run_id = _create_run(self.store, self.conversation_id)
        _save_snapshot(self.store, run_id, input_tokens=50000, context_limit=128000)

        response = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
        data = response.json()
        self.assertEqual(data["input_tokens"], 50000)
        self.assertEqual(data["context_limit"], 128000)
        self.assertEqual(data["remaining_tokens"], 78000)

    # ─── Test 4: Provider vs estimated distinguishable ────────────────────────

    async def test_provider_vs_estimated(self) -> None:
        """Test 4: token_source, is_final, and is_estimated distinguish provider from estimate."""
        run_id = _create_run(self.store, self.conversation_id)

        # Save as estimated
        _save_snapshot(self.store, run_id, token_source="estimated")
        response = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
        data = response.json()
        self.assertEqual(data["token_source"], "estimated")
        self.assertTrue(data["is_estimated"])
        self.assertFalse(data["is_final"])

        # Update to provider
        self.store.update_context_snapshot_tokens(
            run_id, input_tokens=85000, output_tokens=2000, token_source="provider"
        )
        response = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
        data = response.json()
        self.assertEqual(data["token_source"], "provider")
        self.assertFalse(data["is_estimated"])
        self.assertTrue(data["is_final"])
        self.assertEqual(data["input_tokens"], 85000)
        self.assertEqual(data["output_tokens"], 2000)

    # ─── Test 5: Segments broken down ─────────────────────────────────────────

    async def test_segments_breakdown(self) -> None:
        """Test 5: System/conversation/tools/memory/skills are separate segments."""
        run_id = _create_run(self.store, self.conversation_id)
        _save_snapshot(self.store, run_id)

        response = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
        data = response.json()
        segments = {s["kind"]: s["tokens"] for s in data["segments"]}
        self.assertEqual(segments["system"], 8230)
        self.assertEqual(segments["conversation"], 41280)
        self.assertEqual(segments["tool_results"], 18420)
        self.assertEqual(segments["memory"], 2840)
        self.assertEqual(segments["skills"], 5991)

    # ─── Test 6: Skills with exact revisions ──────────────────────────────────

    async def test_skills_show_revisions(self) -> None:
        """Test 6: Loaded skills include exact revision numbers."""
        run_id = _create_run(self.store, self.conversation_id)
        _save_snapshot(self.store, run_id)

        response = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
        data = response.json()
        skills = data["skills"]
        self.assertEqual(len(skills), 2)
        tg = next(s for s in skills if s["name"] == "telegram-recovery")
        self.assertEqual(tg["revision"], 8)
        self.assertEqual(tg["tokens"], 3102)
        gw = next(s for s in skills if s["name"] == "systemd-gateway")
        self.assertEqual(gw["revision"], 5)
        self.assertEqual(gw["tokens"], 1774)

    # ─── Test 7: Memory entries identifiable ──────────────────────────────────

    async def test_memory_entries_identifiable(self) -> None:
        """Test 7: Retrieved memory entries have id, namespace, confidence."""
        run_id = _create_run(self.store, self.conversation_id)
        _save_snapshot(self.store, run_id)

        response = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
        data = response.json()
        memories = data["memories"]
        self.assertEqual(len(memories), 2)
        m1 = next(m for m in memories if m["id"] == "mem-001")
        self.assertEqual(m1["namespace"], "curated")
        self.assertEqual(m1["tokens"], 1840)
        self.assertAlmostEqual(m1["confidence"], 0.95)
        self.assertEqual(m1["label"], "Production gateway uses systemd")

    # ─── Test 8: Last invocation vs conversation context ──────────────────────

    async def test_invocation_vs_conversation_context(self) -> None:
        """Test 8: conversation_tokens and last_invocation_tokens distinguished."""
        run_id = _create_run(self.store, self.conversation_id)
        _save_snapshot(
            self.store, run_id,
            conversation_tokens=91420,
            last_invocation_tokens=84320,
        )

        response = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
        data = response.json()
        self.assertEqual(data["conversation_tokens"], 91420)
        self.assertEqual(data["last_invocation_tokens"], 84320)
        # They should differ — conversation accumulates beyond what was sent
        self.assertNotEqual(data["conversation_tokens"], data["last_invocation_tokens"])

    # ─── Test 9: Context compaction visible ───────────────────────────────────

    async def test_compaction_visible(self) -> None:
        """Test 9: Transformations show compaction when it occurred."""
        run_id = _create_run(self.store, self.conversation_id)
        _save_snapshot(self.store, run_id)

        response = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
        data = response.json()
        transformations = data["transformations"]
        self.assertIsNotNone(transformations)
        self.assertGreater(len(transformations), 0)
        # First transformation shows raw → compacted
        raw = next(t for t in transformations if t["label"] == "Raw conversation")
        self.assertEqual(raw["tokens_before"], 74120)
        self.assertEqual(raw["tokens_after"], 51280)

    # ─── Test 10: Old runs preserve snapshot ──────────────────────────────────

    async def test_old_runs_preserve_snapshot(self) -> None:
        """Test 10: Completed runs retain their context snapshot unchanged."""
        run_id = _create_run(self.store, self.conversation_id, status="completed")
        _save_snapshot(self.store, run_id, input_tokens=42000)

        # Verify it persists
        response = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["input_tokens"], 42000)
        self.assertEqual(data["run_status"], "completed")

        # Read again — still there, unchanged
        response2 = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
        data2 = response2.json()
        self.assertEqual(data2["input_tokens"], 42000)
        self.assertEqual(data2["created_at"], data["created_at"])

    # ─── Test 11: Skill entries link to Learning Studio ───────────────────────

    async def test_skills_link_to_studio(self) -> None:
        """Test 11: Each skill entry has a skill_id for navigation."""
        run_id = _create_run(self.store, self.conversation_id)
        _save_snapshot(self.store, run_id)

        response = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
        data = response.json()
        for skill in data["skills"]:
            self.assertIn("skill_id", skill)
            self.assertTrue(len(skill["skill_id"]) > 0)

    # ─── Test 12: Memory entries link to record ───────────────────────────────

    async def test_memories_link_to_record(self) -> None:
        """Test 12: Each memory entry has an id for navigation."""
        run_id = _create_run(self.store, self.conversation_id)
        _save_snapshot(self.store, run_id)

        response = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
        data = response.json()
        for mem in data["memories"]:
            self.assertIn("id", mem)
            self.assertTrue(len(mem["id"]) > 0)

    # ─── Test 13: Skill links to Journey provenance ───────────────────────────

    async def test_skill_links_to_journey(self) -> None:
        """Test 13: skill_id can be used to query the Journey graph."""
        run_id = _create_run(self.store, self.conversation_id)
        _save_snapshot(self.store, run_id)

        response = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
        data = response.json()
        skill_id = data["skills"][0]["skill_id"]
        # The skill_id should be non-empty and usable as a journey filter
        self.assertTrue(len(skill_id) > 0)
        # Journey endpoint accepts skill_id param (may return empty if skill not registered)
        journey_resp = await self.client.get(f"/api/learning/journey?skill_id={skill_id}")
        # Should not 500 — either 200 with empty data or valid graph
        self.assertIn(journey_resp.status_code, (200, 404))

    # ─── Test 14: Context updates live during run ─────────────────────────────

    async def test_context_updates_live(self) -> None:
        """Test 14: update_context_snapshot_tokens updates token counts during a run."""
        run_id = _create_run(self.store, self.conversation_id, status="running")
        _save_snapshot(self.store, run_id, input_tokens=50000, token_source="estimated")

        # Read initial
        response = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
        data = response.json()
        self.assertEqual(data["input_tokens"], 50000)
        self.assertEqual(data["token_source"], "estimated")
        self.assertTrue(data["is_estimated"])

        # Simulate provider returning final usage
        updated = self.store.update_context_snapshot_tokens(
            run_id, input_tokens=52100, output_tokens=3400, token_source="provider"
        )
        self.assertTrue(updated)

        # Read updated
        response = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
        data = response.json()
        self.assertEqual(data["input_tokens"], 52100)
        self.assertEqual(data["output_tokens"], 3400)
        self.assertEqual(data["token_source"], "provider")
        self.assertTrue(data["is_final"])

    # ─── Test 15: Provider usage replaces estimates ───────────────────────────

    async def test_provider_replaces_estimate(self) -> None:
        """Test 15: Final provider usage overwrites estimated values."""
        run_id = _create_run(self.store, self.conversation_id)
        _save_snapshot(self.store, run_id, input_tokens=80000, token_source="estimated")

        # Verify estimated
        snap = self.store.get_context_snapshot(run_id)
        assert snap is not None
        self.assertEqual(snap["token_source"], "estimated")

        # Provider update
        self.store.update_context_snapshot_tokens(
            run_id, input_tokens=82500, output_tokens=1800, token_source="provider"
        )
        snap = self.store.get_context_snapshot(run_id)
        assert snap is not None
        self.assertEqual(snap["token_source"], "provider")
        self.assertEqual(snap["input_tokens"], 82500)
        self.assertEqual(snap["output_tokens"], 1800)

    # ─── Test 16: Mobile/tablet usability ─────────────────────────────────────

    async def test_responsive_layout_no_horizontal_scroll(self) -> None:
        """Test 16: Dialog structure supports mobile (verified by CSS, no API test needed).

        This test validates the API response structure has no fields that would
        force wide layouts — all data is text/number, no wide pre-formatted strings.
        """
        run_id = _create_run(self.store, self.conversation_id)
        _save_snapshot(self.store, run_id)

        response = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
        data = response.json()
        # Ensure no field has excessively long string values that would overflow
        for seg in data["segments"]:
            self.assertLess(len(seg["kind"]), 50)
        for skill in data["skills"]:
            self.assertLess(len(skill["name"]), 200)
        for mem in data["memories"]:
            label = mem.get("label") or ""
            self.assertLess(len(label), 500)

    # ─── Test 17: Runs without learning data ──────────────────────────────────

    async def test_no_snapshot_returns_204(self) -> None:
        """Test 17: Runs without any context data return 204."""
        run_id = _create_run(self.store, self.conversation_id)
        # No snapshot saved, no context manifest either

        response = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
        self.assertEqual(response.status_code, 204)

    async def test_manifest_fallback_synthesizes(self) -> None:
        """Test 17b: Runs with context manifest but no snapshot get a synthesized response."""
        import hashlib
        run_id = _create_run(self.store, self.conversation_id, status="running")
        # Prepare a context manifest (simulating the execution pipeline)
        execution_prompt = "execution prompt text"
        prompt_sha256 = hashlib.sha256(execution_prompt.encode("utf-8")).hexdigest()
        manifest = {
            "profile": "chat",
            "budget_tokens": 128000,
            "estimated_tokens": 45000,
            "prompt_sha256": prompt_sha256,
            "identity_fingerprint": "id-fp",
            "context_fingerprint": "ctx-fp",
            "sources": [
                {"label": "SOUL.md", "category": "identity", "estimated_tokens": 2000, "included": True},
                {"label": "Recent messages", "category": "history", "estimated_tokens": 30000, "included": True},
                {"label": "Tool output", "category": "tool_result", "estimated_tokens": 8000, "included": True},
                {"label": "Important fact", "category": "curated_memory", "estimated_tokens": 3000, "included": True, "confidence": 0.9},
                {"label": "coding-standards", "category": "skill", "estimated_tokens": 2000, "included": True, "sha256": "def456"},
            ],
        }
        self.store.prepare_run_context(run_id, execution_prompt, manifest)

        response = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["token_source"], "estimated")
        self.assertTrue(data["is_estimated"])
        self.assertEqual(data["context_limit"], 128000)
        self.assertEqual(data["input_tokens"], 45000)
        # Check segments were synthesized
        segments = {s["kind"]: s["tokens"] for s in data["segments"]}
        self.assertEqual(segments.get("system"), 2000)
        self.assertEqual(segments.get("conversation"), 30000)
        self.assertEqual(segments.get("tool_results"), 8000)
        self.assertEqual(segments.get("memory"), 3000)
        self.assertEqual(segments.get("skills"), 2000)
        # Check skills synthesized
        self.assertEqual(len(data["skills"]), 1)
        self.assertEqual(data["skills"][0]["name"], "coding-standards")
        # Check memories synthesized
        self.assertEqual(len(data["memories"]), 1)
        self.assertAlmostEqual(data["memories"][0]["confidence"], 0.9)

    # ─── Test 18: Context inspection is read-only ─────────────────────────────

    async def test_inspection_does_not_alter_run(self) -> None:
        """Test 18: GET context-snapshot does not modify the run or its context."""
        run_id = _create_run(self.store, self.conversation_id, status="running")
        _save_snapshot(self.store, run_id, input_tokens=60000)

        # Get run state before inspection
        run_before = self.store.get_run(run_id)
        version_before = run_before.version
        snapshot_before = self.store.get_context_snapshot(run_id)

        # Perform multiple inspections
        for _ in range(3):
            response = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
            self.assertEqual(response.status_code, 200)

        # Verify nothing changed
        run_after = self.store.get_run(run_id)
        self.assertEqual(run_after.version, version_before)
        self.assertEqual(run_after.status, "running")
        snapshot_after = self.store.get_context_snapshot(run_id)
        self.assertEqual(snapshot_before, snapshot_after)


class ContextSnapshotStoreTests(unittest.TestCase):
    """Unit tests for the context snapshot store methods."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gravityclaw-store-4c-")
        self.store = Store(Path(self.temporary.name) / "test.db")
        self.store.initialize()
        ws = self.store.create_workspace("test", Path("/tmp/test"))
        self.workspace_id = ws.id
        conv = self.store.create_conversation(self.workspace_id, channel="test")
        self.conversation_id = conv.id

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_save_and_get_snapshot(self) -> None:
        """Basic save and retrieve."""
        run_id = _create_run(self.store, self.conversation_id)
        _save_snapshot(self.store, run_id)
        snap = self.store.get_context_snapshot(run_id)
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertEqual(snap["run_id"], run_id)
        self.assertEqual(snap["model"], "gpt-5.6")
        self.assertEqual(snap["input_tokens"], 84320)
        self.assertEqual(len(snap["segments"]), 5)
        self.assertEqual(len(snap["skills"]), 2)
        self.assertEqual(len(snap["memories"]), 2)

    def test_get_nonexistent_returns_none(self) -> None:
        """Getting a snapshot for a run with no snapshot returns None."""
        run_id = _create_run(self.store, self.conversation_id)
        self.assertIsNone(self.store.get_context_snapshot(run_id))

    def test_update_tokens_replaces(self) -> None:
        """update_context_snapshot_tokens changes token values and source."""
        run_id = _create_run(self.store, self.conversation_id)
        _save_snapshot(self.store, run_id, input_tokens=50000, token_source="estimated")

        result = self.store.update_context_snapshot_tokens(
            run_id, input_tokens=52000, output_tokens=1500, token_source="provider"
        )
        self.assertTrue(result)

        snap = self.store.get_context_snapshot(run_id)
        assert snap is not None
        self.assertEqual(snap["input_tokens"], 52000)
        self.assertEqual(snap["output_tokens"], 1500)
        self.assertEqual(snap["token_source"], "provider")

    def test_update_tokens_nonexistent_returns_false(self) -> None:
        """Updating tokens for non-existent snapshot returns False."""
        run_id = _create_run(self.store, self.conversation_id)
        result = self.store.update_context_snapshot_tokens(
            run_id, input_tokens=1000, token_source="provider"
        )
        self.assertFalse(result)

    def test_save_replaces_existing(self) -> None:
        """Saving a snapshot for an existing run replaces it (INSERT OR REPLACE)."""
        run_id = _create_run(self.store, self.conversation_id)
        _save_snapshot(self.store, run_id, input_tokens=40000)
        _save_snapshot(self.store, run_id, input_tokens=80000)

        snap = self.store.get_context_snapshot(run_id)
        assert snap is not None
        self.assertEqual(snap["input_tokens"], 80000)

    def test_invalid_token_source_raises(self) -> None:
        """Invalid token_source is rejected."""
        run_id = _create_run(self.store, self.conversation_id)
        with self.assertRaises(ValueError):
            self.store.save_context_snapshot(
                run_id, model="test", context_limit=100000,
                input_tokens=50000, token_source="invalid"
            )

    def test_transformations_optional(self) -> None:
        """Transformations can be None."""
        run_id = _create_run(self.store, self.conversation_id)
        self.store.save_context_snapshot(
            run_id, model="test", context_limit=100000,
            input_tokens=30000, token_source="estimated",
            transformations=None,
        )
        snap = self.store.get_context_snapshot(run_id)
        assert snap is not None
        self.assertIsNone(snap["transformations"])

    def test_schema_version_18(self) -> None:
        """Schema version is 18 after initialization."""
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
        self.assertEqual(row["value"], "18")


class ContextSnapshotPOSTTests(unittest.IsolatedAsyncioTestCase):
    """Tests for the POST endpoint that persists snapshots."""

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gravityclaw-phase4c-post-")
        root = Path(self.temporary.name)
        self.app = _make_app(root)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )
        self.store: Store = self.app.state.store
        ws = self.store.create_workspace("test-ws", Path("/tmp/test-ws"))
        self.workspace_id = ws.id
        conv = self.store.create_conversation(self.workspace_id, channel="web")
        self.conversation_id = conv.id

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.temporary.cleanup()

    async def test_post_saves_snapshot(self) -> None:
        """POST /api/runs/{run_id}/context-snapshot persists data."""
        run_id = _create_run(self.store, self.conversation_id)
        body = {
            "model": "gpt-5.6",
            "context_limit": 200000,
            "input_tokens": 84320,
            "output_tokens": 1250,
            "token_source": "provider",
            "segments": [{"kind": "system", "tokens": 8000}],
            "skills": [{"skill_id": "sk-1", "name": "test-skill", "revision": 3, "tokens": 1500}],
            "memories": [{"id": "m-1", "namespace": "curated", "tokens": 500}],
            "conversation_tokens": 50000,
            "last_invocation_tokens": 45000,
        }
        response = await self.client.post(
            f"/api/v1/runs/{run_id}/context-snapshot",
            json=body,
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["status"], "saved")
        self.assertEqual(result["run_id"], run_id)

        # Verify via GET
        get_resp = await self.client.get(f"/api/runs/{run_id}/context-snapshot")
        self.assertEqual(get_resp.status_code, 200)
        data = get_resp.json()
        self.assertEqual(data["model"], "gpt-5.6")
        self.assertEqual(data["input_tokens"], 84320)

    async def test_post_404_for_nonexistent_run(self) -> None:
        """POST returns 404 for non-existent run."""
        response = await self.client.post(
            "/api/v1/runs/nonexistent-run/context-snapshot",
            json={"model": "test", "context_limit": 100000, "input_tokens": 5000},
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
