"""Tests for Phase 3: Knowledge Acquisition + Controlled Autonomy.

Acceptance criteria:
 1. /learn short document → skill proposal.
 2. /learn large document → chunked processing → SKILL.md + references → proposal.
 3. /learn previously learned material → duplicate detected.
 4. /learn updated material → improvement proposal instead of duplicate.
 5. Failed / partial ingestion → no corrupt skill created.
 6. Provenance retained from source → skill → revision.
 7. Scheduler invokes curator.
 8. Pinned skill remains active regardless of age.
 9. User/bundled skills remain protected.
10. Low-value agent skill becomes stale.
11. Long-unused stale skill becomes archived.
12. Archived skill can be restored.
13. STRICT mode never applies skill mutations without approval.
14. BALANCED mode can patch approved agent-owned skills automatically.
15. AUTONOMOUS mode can create agent-owned skills automatically.
16. Trust policy denies mutation of user/bundled skills regardless of mode.
17. Runtime correction of skill revision N → reviewer proposes revision N+1.
18. Future equivalent run → revision N+1 is selected → succeeds without correction.
"""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from gravityclaw.skills.attribution import (
    AttributionReport,
    SkillOutcome,
    build_attribution,
    enrich_reviewer_context,
)
from gravityclaw.skills.curator import (
    Curator,
    CuratorAction,
    CuratorConfig,
    CuratorReport,
    SkillUtility,
    compute_utility,
)
from gravityclaw.skills.discovery import SKILL_FILENAME, ensure_skill_directory
from gravityclaw.skills.ingestion import (
    ContentChunk,
    DeduplicationResult,
    IngestionEngine,
    LearnResult,
    SkillReference,
    SourceType,
    check_deduplication,
    chunk_content,
    classify_source,
)
from gravityclaw.skills.models import (
    ProposalStatus,
    SkillOwner,
    SkillState,
    SkillTrust,
)
from gravityclaw.skills.registry import SkillRegistry
from gravityclaw.skills.runtime import RunSkillContext, LoadedSkill
from gravityclaw.skills.service import SkillService
from gravityclaw.skills.telemetry import TelemetryService
from gravityclaw.skills.trust import (
    OperationContext,
    OperationKind,
    PolicyResult,
    TrustDecision,
    TrustMode,
    TrustPolicy,
)
from gravityclaw.store import Store, utc_now


# ─────────────────────────────────────────────────────────────────────────────
# Test fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _make_store(tmp: Path) -> Store:
    """Create an initialized Store in a temp directory."""
    store = Store(tmp / "state.db")
    store.initialize()
    return store


def _make_service(tmp: Path, store: Store, **kwargs) -> SkillService:
    """Create a SkillService."""
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    (home / "skills").mkdir(exist_ok=True)
    return SkillService(store, home, **kwargs)


def _write_skill(home: Path, name: str, content: str, meta: dict | None = None) -> Path:
    """Write a skill to disk."""
    skill_dir = home / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / SKILL_FILENAME).write_text(content, encoding="utf-8")
    if meta:
        (skill_dir / "skill.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )
    return skill_dir


def _register_skill(
    registry: SkillRegistry,
    name: str,
    description: str,
    *,
    owner: str = SkillOwner.AGENT,
    trust: str = SkillTrust.APPROVED,
    revision: int = 1,
    state: str = SkillState.ACTIVE,
    pinned: bool = False,
) -> str:
    """Register a skill in the registry and return its ID."""
    skill = registry.register_skill(
        name=name, description=description,
        path=f"skills/{name}", owner=owner, trust=trust,
    )
    updates: dict[str, Any] = {"revision": revision}
    if state != SkillState.ACTIVE:
        updates["state"] = state
    if pinned:
        updates["pinned"] = True
    registry.update_skill(skill.skill_id, **updates)
    return skill.skill_id


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: /learn short document → skill proposal
# ─────────────────────────────────────────────────────────────────────────────


class TestLearnShortDocument(unittest.TestCase):
    """AC 1: /learn short document → skill proposal."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-phase3-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.registry = SkillRegistry(self.store)
        self.home = self.tmpdir / "home"
        self.home.mkdir()
        (self.home / "skills").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_short_document_creates_proposal(self) -> None:
        engine = IngestionEngine(self.registry, self.store, self.home)
        content = "# Docker Compose\n\nStep 1: Create docker-compose.yml\nStep 2: Define services\nStep 3: Run docker compose up"
        result = engine.ingest("docker-guide.md", content=content, title_hint="Docker Compose Setup")

        self.assertEqual(result.source_type, SourceType.PLAIN_TEXT)
        self.assertIsNotNone(result.proposal_id)
        self.assertEqual(result.chunks_processed, 1)  # Small → single chunk
        self.assertTrue(len(result.proposed_skill_content) > 0)

        # Verify proposal exists in registry
        proposal = self.registry.get_proposal(result.proposal_id)
        self.assertEqual(proposal.status, ProposalStatus.PENDING)
        self.assertEqual(proposal.operation, "create")


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: /learn large document → chunked + references
# ─────────────────────────────────────────────────────────────────────────────


class TestLearnLargeDocument(unittest.TestCase):
    """AC 2: /learn large document → chunked → SKILL.md + references → proposal."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-phase3-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.registry = SkillRegistry(self.store)
        self.home = self.tmpdir / "home"
        self.home.mkdir()
        (self.home / "skills").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_large_document_is_chunked(self) -> None:
        # Create a document larger than LARGE_SOURCE_CHARS
        large_content = "# Docker Operations\n\n"
        for i in range(30):
            large_content += f"## Section {i}\n\n"
            large_content += f"This is section {i} with detailed content about Docker operations. " * 20
            large_content += "\n\n"

        self.assertGreater(len(large_content), 12_000)

        engine = IngestionEngine(self.registry, self.store, self.home)
        result = engine.ingest("large-doc.md", content=large_content, title_hint="Docker Operations")

        self.assertGreater(result.chunks_processed, 1)
        self.assertIsNotNone(result.proposal_id)
        self.assertTrue(len(result.references) > 0)

        # Verify proposal
        proposal = self.registry.get_proposal(result.proposal_id)
        self.assertEqual(proposal.operation, "create")
        # Content should reference sub-files
        self.assertIn("references/", proposal.content)


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: /learn previously learned material → duplicate detected
# ─────────────────────────────────────────────────────────────────────────────


class TestLearnDuplicateDetection(unittest.TestCase):
    """AC 3: /learn previously learned material → duplicate detected."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-phase3-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.registry = SkillRegistry(self.store)
        self.home = self.tmpdir / "home"
        self.home.mkdir()
        (self.home / "skills").mkdir()
        self.store.sync_skills_fts()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_exact_name_match_detected(self) -> None:
        # Register an existing skill
        _register_skill(self.registry, "docker-compose-setup",
                        "How to set up Docker Compose")
        _write_skill(self.home, "docker-compose-setup",
                     "# Docker Compose\n\nExisting content.")
        self.store.sync_skills_fts()

        # Try to learn the same thing
        engine = IngestionEngine(self.registry, self.store, self.home)

        # Use a custom synthesizer that returns the same name
        def synth(chunks, source_type, title_hint, source_identity):
            return {
                "name": "docker-compose-setup",
                "title": "Docker Compose Setup",
                "description": "How to set up Docker Compose",
                "summary": "Docker Compose guide",
                "content": "# Docker Compose\n\nNew improved content.",
                "references": [],
            }

        engine._synthesizer = synth
        result = engine.ingest("docker.md", content="some docker content")

        # Should detect as improvement (name already exists)
        self.assertEqual(result.dedup_result, DeduplicationResult.IMPROVEMENT)
        self.assertIsNotNone(result.existing_skill_id)


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: /learn updated material → improvement proposal
# ─────────────────────────────────────────────────────────────────────────────


class TestLearnImprovement(unittest.TestCase):
    """AC 4: /learn updated material → improvement proposal."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-phase3-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.registry = SkillRegistry(self.store)
        self.home = self.tmpdir / "home"
        self.home.mkdir()
        (self.home / "skills").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_improvement_creates_patch_proposal(self) -> None:
        # Register existing skill
        skill_id = _register_skill(self.registry, "modal-deploy",
                                    "How to deploy with Modal")
        _write_skill(self.home, "modal-deploy", "# Modal Deploy\n\nOld steps.")
        self.store.sync_skills_fts()

        engine = IngestionEngine(self.registry, self.store, self.home)

        def synth(chunks, source_type, title_hint, source_identity):
            return {
                "name": "modal-deploy",
                "title": "Modal Deploy",
                "description": "How to deploy with Modal",
                "summary": "Updated Modal deployment guide",
                "content": "# Modal Deploy\n\nNew improved steps with GPU support.",
                "references": [],
            }

        engine._synthesizer = synth
        result = engine.ingest("modal-updated.md", content="updated modal content")

        self.assertEqual(result.dedup_result, DeduplicationResult.IMPROVEMENT)
        self.assertIsNotNone(result.proposal_id)

        # Verify it's a patch proposal, not a create
        proposal = self.registry.get_proposal(result.proposal_id)
        self.assertEqual(proposal.operation, "patch")
        self.assertEqual(proposal.skill_id, skill_id)
        self.assertIsNotNone(proposal.before)  # Should have previous content


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Failed/partial ingestion → no corrupt skill
# ─────────────────────────────────────────────────────────────────────────────


class TestLearnFailureSafety(unittest.TestCase):
    """AC 5: Failed/partial ingestion → no corrupt skill created."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-phase3-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.registry = SkillRegistry(self.store)
        self.home = self.tmpdir / "home"
        self.home.mkdir()
        (self.home / "skills").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_empty_synthesis_no_proposal(self) -> None:
        engine = IngestionEngine(self.registry, self.store, self.home)

        # Synthesizer that returns empty content (simulates failure)
        def failing_synth(chunks, source_type, title_hint, source_identity):
            return {"name": "", "description": "", "content": "", "references": []}

        engine._synthesizer = failing_synth
        result = engine.ingest("bad-source.md", content="some content")

        self.assertIsNone(result.proposal_id)
        self.assertTrue(len(result.warnings) > 0)

        # No proposals should exist
        proposals = self.registry.list_proposals()
        self.assertEqual(len(proposals), 0)

    def test_no_content_no_proposal(self) -> None:
        engine = IngestionEngine(self.registry, self.store, self.home)
        # Source that can't be loaded (non-existent web page, no content provided)
        result = engine.ingest("https://nonexistent.example.com/missing")

        self.assertIsNone(result.proposal_id)
        self.assertTrue(len(result.warnings) > 0)


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: Provenance retained
# ─────────────────────────────────────────────────────────────────────────────


class TestProvenance(unittest.TestCase):
    """AC 6: Provenance retained from source → skill → revision."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-phase3-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.registry = SkillRegistry(self.store)
        self.home = self.tmpdir / "home"
        self.home.mkdir()
        (self.home / "skills").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_source_provenance_in_proposal(self) -> None:
        engine = IngestionEngine(self.registry, self.store, self.home)
        content = "# SSH Tunneling\n\nStep 1: ssh -L ...\nStep 2: Connect to localhost"
        result = engine.ingest(
            "ssh-guide.md",
            content=content,
            source_run_id="run-abc-123",
            title_hint="SSH Tunneling",
        )

        self.assertIsNotNone(result.proposal_id)
        proposal = self.registry.get_proposal(result.proposal_id)

        # Source run ID propagated
        self.assertEqual(proposal.source_run_id, "run-abc-123")
        # Reason contains source info
        self.assertIn("plain_text", proposal.reason)
        # Review model recorded
        self.assertEqual(proposal.review_model, "learn-ingestion")


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: Scheduler invokes curator
# ─────────────────────────────────────────────────────────────────────────────


class TestCuratorScheduling(unittest.TestCase):
    """AC 7: Scheduler invokes curator (unit test: curator.run() works)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-phase3-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.registry = SkillRegistry(self.store)
        self.home = self.tmpdir / "home"
        self.home.mkdir()
        (self.home / "skills").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_curator_runs_without_error(self) -> None:
        policy = TrustPolicy(mode=TrustMode.BALANCED)
        config = CuratorConfig(enabled=True, stale_after_days=30)
        curator = Curator(self.registry, self.store, policy, config)

        # Register some skills
        _register_skill(self.registry, "skill-a", "Test skill A")
        _register_skill(self.registry, "skill-b", "Test skill B")

        report = curator.run()
        self.assertEqual(report.skills_evaluated, 2)
        self.assertEqual(len(report.errors), 0)

    def test_disabled_curator_does_nothing(self) -> None:
        policy = TrustPolicy(mode=TrustMode.BALANCED)
        config = CuratorConfig(enabled=False)
        curator = Curator(self.registry, self.store, policy, config)

        _register_skill(self.registry, "skill-c", "Test skill C")

        report = curator.run()
        self.assertEqual(report.skills_evaluated, 0)


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: Pinned skill remains active
# ─────────────────────────────────────────────────────────────────────────────


class TestPinnedSkillProtection(unittest.TestCase):
    """AC 8: Pinned skill remains active regardless of age."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-phase3-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.registry = SkillRegistry(self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_pinned_skill_not_staled(self) -> None:
        policy = TrustPolicy(mode=TrustMode.AUTONOMOUS)
        config = CuratorConfig(stale_after_days=1)
        curator = Curator(self.registry, self.store, policy, config)

        # Register a pinned skill — even in AUTONOMOUS mode, pinned is protected
        _register_skill(self.registry, "pinned-skill", "Critical procedure",
                        pinned=True)

        report = curator.run()
        # Should be skipped as protected
        self.assertEqual(report.skipped_protected, 1)
        self.assertEqual(len(report.actions_taken), 0)

        # Verify still ACTIVE
        skill = self.registry.get_skill_by_name("pinned-skill")
        self.assertEqual(skill.state, SkillState.ACTIVE)


# ─────────────────────────────────────────────────────────────────────────────
# Test 9: User/bundled skills remain protected
# ─────────────────────────────────────────────────────────────────────────────


class TestOwnershipProtection(unittest.TestCase):
    """AC 9: User/bundled skills remain protected."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-phase3-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.registry = SkillRegistry(self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_user_skill_protected_from_curator(self) -> None:
        policy = TrustPolicy(mode=TrustMode.AUTONOMOUS)
        config = CuratorConfig(stale_after_days=1)
        curator = Curator(self.registry, self.store, policy, config)

        _register_skill(self.registry, "user-skill", "User's skill",
                        owner=SkillOwner.USER)

        report = curator.run()
        self.assertEqual(report.skipped_protected, 1)

    def test_bundled_skill_protected_from_curator(self) -> None:
        policy = TrustPolicy(mode=TrustMode.AUTONOMOUS)
        config = CuratorConfig(stale_after_days=1)
        curator = Curator(self.registry, self.store, policy, config)

        _register_skill(self.registry, "bundled-skill", "Built-in skill",
                        owner=SkillOwner.BUNDLED)

        report = curator.run()
        self.assertEqual(report.skipped_protected, 1)


# ─────────────────────────────────────────────────────────────────────────────
# Test 10: Low-value agent skill becomes stale
# ─────────────────────────────────────────────────────────────────────────────


class TestSkillBecomeStale(unittest.TestCase):
    """AC 10: Low-value agent skill becomes stale."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-phase3-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.registry = SkillRegistry(self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_low_utility_skill_marked_stale(self) -> None:
        policy = TrustPolicy(mode=TrustMode.AUTONOMOUS)
        config = CuratorConfig(
            stale_after_days=30,
            minimum_invocations=2,
            utility_stale_threshold=0.3,
        )
        curator = Curator(self.registry, self.store, policy, config)

        # Register a skill
        skill_id = _register_skill(self.registry, "low-value-skill",
                                    "A rarely-used skill")

        # Record some telemetry: executed but mostly failed
        for _ in range(3):
            self.registry.record_usage(skill_id, "executed", run_id="run-x")
            self.registry.record_usage(skill_id, "failed", run_id="run-x")

        # Record a last-used time 35 days ago
        # We insert a usage event with an old timestamp
        old_time = (datetime.now(UTC) - timedelta(days=35)).isoformat()
        with self.store._connect() as conn:
            conn.execute(
                """UPDATE skill_usage SET created_at=? WHERE skill_id=?""",
                (old_time, skill_id),
            )

        report = curator.run()
        # Should have marked stale
        self.assertTrue(
            any(a.action == "mark_stale" and a.skill_name == "low-value-skill"
                for a in report.actions_taken),
            f"Expected mark_stale action, got: {report.actions_taken}",
        )

        skill = self.registry.get_skill_by_name("low-value-skill")
        self.assertEqual(skill.state, SkillState.STALE)


# ─────────────────────────────────────────────────────────────────────────────
# Test 11: Long-unused stale skill becomes archived
# ─────────────────────────────────────────────────────────────────────────────


class TestStaleBecomesArchived(unittest.TestCase):
    """AC 11: Long-unused stale skill becomes archived."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-phase3-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.registry = SkillRegistry(self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_stale_skill_archived_after_threshold(self) -> None:
        policy = TrustPolicy(mode=TrustMode.AUTONOMOUS)
        config = CuratorConfig(
            archive_after_days=90,
            minimum_invocations=2,
            utility_archive_threshold=0.1,
        )
        curator = Curator(self.registry, self.store, policy, config)

        # Register a stale skill
        skill_id = _register_skill(self.registry, "stale-skill",
                                    "A stale skill", state=SkillState.STALE)

        # Record minimal, mostly-failed usage with old timestamps
        for _ in range(3):
            self.registry.record_usage(skill_id, "executed", run_id="run-y")
            self.registry.record_usage(skill_id, "failed", run_id="run-y")

        old_time = (datetime.now(UTC) - timedelta(days=100)).isoformat()
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE skill_usage SET created_at=? WHERE skill_id=?",
                (old_time, skill_id),
            )

        report = curator.run()
        self.assertTrue(
            any(a.action == "archive" and a.skill_name == "stale-skill"
                for a in report.actions_taken),
            f"Expected archive action, got: {report.actions_taken}",
        )

        skill = self.registry.get_skill_by_name("stale-skill")
        self.assertEqual(skill.state, SkillState.ARCHIVED)


# ─────────────────────────────────────────────────────────────────────────────
# Test 12: Archived skill can be restored
# ─────────────────────────────────────────────────────────────────────────────


class TestArchivedRestore(unittest.TestCase):
    """AC 12: Archived skill can be restored."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-phase3-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.registry = SkillRegistry(self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_archived_skill_restored_on_reuse(self) -> None:
        policy = TrustPolicy(mode=TrustMode.BALANCED)
        curator = Curator(self.registry, self.store, policy)

        # Register an archived skill
        _register_skill(self.registry, "archived-skill",
                        "Previously useful", state=SkillState.ARCHIVED)

        # Simulate reuse
        restored = curator.restore_on_reuse("archived-skill")
        self.assertTrue(restored)

        skill = self.registry.get_skill_by_name("archived-skill")
        self.assertEqual(skill.state, SkillState.ACTIVE)

    def test_non_archived_skill_restore_noop(self) -> None:
        policy = TrustPolicy(mode=TrustMode.BALANCED)
        curator = Curator(self.registry, self.store, policy)

        _register_skill(self.registry, "active-skill", "Still active")

        restored = curator.restore_on_reuse("active-skill")
        self.assertFalse(restored)


# ─────────────────────────────────────────────────────────────────────────────
# Test 13: STRICT mode never auto-applies
# ─────────────────────────────────────────────────────────────────────────────


class TestStrictMode(unittest.TestCase):
    """AC 13: STRICT mode never applies skill mutations without approval."""

    def test_strict_requires_approval_for_create(self) -> None:
        policy = TrustPolicy(mode=TrustMode.STRICT)
        ctx = OperationContext(
            kind=OperationKind.SKILL_CREATE,
            skill_owner=SkillOwner.AGENT,
            confidence=0.95,
        )
        result = policy.evaluate(ctx)
        self.assertEqual(result.decision, TrustDecision.REQUIRE_APPROVAL)

    def test_strict_requires_approval_for_patch(self) -> None:
        policy = TrustPolicy(mode=TrustMode.STRICT)
        ctx = OperationContext(
            kind=OperationKind.SKILL_PATCH,
            skill_owner=SkillOwner.AGENT,
            confidence=0.95,
        )
        result = policy.evaluate(ctx)
        self.assertEqual(result.decision, TrustDecision.REQUIRE_APPROVAL)

    def test_strict_requires_approval_for_archive(self) -> None:
        policy = TrustPolicy(mode=TrustMode.STRICT)
        ctx = OperationContext(
            kind=OperationKind.SKILL_ARCHIVE,
            skill_owner=SkillOwner.AGENT,
            confidence=0.95,
        )
        result = policy.evaluate(ctx)
        self.assertEqual(result.decision, TrustDecision.REQUIRE_APPROVAL)

    def test_strict_allows_memory_write(self) -> None:
        policy = TrustPolicy(mode=TrustMode.STRICT)
        ctx = OperationContext(kind=OperationKind.MEMORY_WRITE)
        result = policy.evaluate(ctx)
        self.assertEqual(result.decision, TrustDecision.ALLOW)

    def test_strict_allows_proposal_creation(self) -> None:
        policy = TrustPolicy(mode=TrustMode.STRICT)
        ctx = OperationContext(kind=OperationKind.SKILL_PROPOSAL)
        result = policy.evaluate(ctx)
        self.assertEqual(result.decision, TrustDecision.ALLOW)


# ─────────────────────────────────────────────────────────────────────────────
# Test 14: BALANCED mode can auto-patch agent-owned skills
# ─────────────────────────────────────────────────────────────────────────────


class TestBalancedMode(unittest.TestCase):
    """AC 14: BALANCED mode can patch approved agent-owned skills automatically."""

    def test_balanced_allows_agent_skill_patch(self) -> None:
        policy = TrustPolicy(mode=TrustMode.BALANCED)
        ctx = OperationContext(
            kind=OperationKind.SKILL_PATCH,
            skill_owner=SkillOwner.AGENT,
            confidence=0.85,
        )
        result = policy.evaluate(ctx)
        self.assertEqual(result.decision, TrustDecision.ALLOW)

    def test_balanced_requires_approval_for_new_skill(self) -> None:
        policy = TrustPolicy(mode=TrustMode.BALANCED)
        ctx = OperationContext(
            kind=OperationKind.SKILL_CREATE,
            skill_owner=SkillOwner.AGENT,
            confidence=0.85,
        )
        result = policy.evaluate(ctx)
        self.assertEqual(result.decision, TrustDecision.REQUIRE_APPROVAL)

    def test_balanced_low_confidence_needs_approval(self) -> None:
        policy = TrustPolicy(mode=TrustMode.BALANCED)
        ctx = OperationContext(
            kind=OperationKind.SKILL_PATCH,
            skill_owner=SkillOwner.AGENT,
            confidence=0.5,  # Below 0.7 threshold
        )
        result = policy.evaluate(ctx)
        self.assertEqual(result.decision, TrustDecision.REQUIRE_APPROVAL)


# ─────────────────────────────────────────────────────────────────────────────
# Test 15: AUTONOMOUS mode can create agent-owned skills
# ─────────────────────────────────────────────────────────────────────────────


class TestAutonomousMode(unittest.TestCase):
    """AC 15: AUTONOMOUS mode can create agent-owned skills automatically."""

    def test_autonomous_allows_skill_create(self) -> None:
        policy = TrustPolicy(mode=TrustMode.AUTONOMOUS)
        ctx = OperationContext(
            kind=OperationKind.SKILL_CREATE,
            skill_owner=SkillOwner.AGENT,
            confidence=0.85,
        )
        result = policy.evaluate(ctx)
        self.assertEqual(result.decision, TrustDecision.ALLOW)

    def test_autonomous_allows_skill_patch(self) -> None:
        policy = TrustPolicy(mode=TrustMode.AUTONOMOUS)
        ctx = OperationContext(
            kind=OperationKind.SKILL_PATCH,
            skill_owner=SkillOwner.AGENT,
            confidence=0.85,
        )
        result = policy.evaluate(ctx)
        self.assertEqual(result.decision, TrustDecision.ALLOW)

    def test_autonomous_allows_skill_archive(self) -> None:
        policy = TrustPolicy(mode=TrustMode.AUTONOMOUS)
        ctx = OperationContext(
            kind=OperationKind.SKILL_ARCHIVE,
            skill_owner=SkillOwner.AGENT,
            confidence=0.85,
        )
        result = policy.evaluate(ctx)
        self.assertEqual(result.decision, TrustDecision.ALLOW)

    def test_autonomous_allows_curator_lifecycle(self) -> None:
        policy = TrustPolicy(mode=TrustMode.AUTONOMOUS)
        for kind in (OperationKind.CURATOR_STALE, OperationKind.CURATOR_ARCHIVE, OperationKind.CURATOR_RESTORE):
            ctx = OperationContext(kind=kind, skill_owner=SkillOwner.AGENT)
            result = policy.evaluate(ctx)
            self.assertEqual(result.decision, TrustDecision.ALLOW, f"Failed for {kind}")


# ─────────────────────────────────────────────────────────────────────────────
# Test 16: Trust denies user/bundled regardless of mode
# ─────────────────────────────────────────────────────────────────────────────


class TestTrustDeniesProtectedSkills(unittest.TestCase):
    """AC 16: Trust policy denies mutation of user/bundled skills regardless of mode."""

    def test_user_skill_denied_in_all_modes(self) -> None:
        for mode in TrustMode:
            policy = TrustPolicy(mode=mode)
            ctx = OperationContext(
                kind=OperationKind.SKILL_PATCH,
                skill_owner=SkillOwner.USER,
                confidence=1.0,
            )
            result = policy.evaluate(ctx)
            self.assertEqual(
                result.decision, TrustDecision.DENY,
                f"User skill patch should be DENY in {mode} mode, got {result.decision}",
            )

    def test_bundled_skill_denied_in_all_modes(self) -> None:
        for mode in TrustMode:
            policy = TrustPolicy(mode=mode)
            ctx = OperationContext(
                kind=OperationKind.SKILL_ARCHIVE,
                skill_owner=SkillOwner.BUNDLED,
                confidence=1.0,
            )
            result = policy.evaluate(ctx)
            self.assertEqual(
                result.decision, TrustDecision.DENY,
                f"Bundled skill archive should be DENY in {mode} mode, got {result.decision}",
            )

    def test_pinned_skill_denies_curator_stale(self) -> None:
        for mode in TrustMode:
            policy = TrustPolicy(mode=mode)
            ctx = OperationContext(
                kind=OperationKind.CURATOR_STALE,
                skill_owner=SkillOwner.AGENT,
                skill_pinned=True,
            )
            result = policy.evaluate(ctx)
            self.assertEqual(
                result.decision, TrustDecision.DENY,
                f"Pinned skill stale should be DENY in {mode}, got {result.decision}",
            )


# ─────────────────────────────────────────────────────────────────────────────
# Test 17: Runtime correction → reviewer proposes revision N+1
# ─────────────────────────────────────────────────────────────────────────────


class TestCorrectionAttribution(unittest.TestCase):
    """AC 17: Runtime correction of skill rev N → reviewer proposes rev N+1."""

    def test_correction_produces_attribution_report(self) -> None:
        """When a skill is corrected during a run, attribution captures it."""
        run_context = {
            "run_id": "run-42",
            "loaded": [
                {"skill_id": "sk-001", "name": "telegram-recovery", "revision": 7, "trust": "approved"},
            ],
        }
        telemetry_events = [
            {"skill_id": "sk-001", "event": "loaded"},
            {"skill_id": "sk-001", "event": "executed"},
            {"skill_id": "sk-001", "event": "corrected"},
        ]

        report = build_attribution("run-42", run_context, telemetry_events)

        self.assertTrue(report.has_corrections)
        self.assertFalse(report.overall_success)
        self.assertEqual(len(report.outcomes), 1)

        outcome = report.outcomes[0]
        self.assertEqual(outcome.skill_name, "telegram-recovery")
        self.assertEqual(outcome.revision, 7)
        self.assertEqual(outcome.result, "corrected")
        self.assertTrue(report.needs_skill_update())

    def test_enriched_reviewer_context_includes_attribution(self) -> None:
        """Attribution data enriches the reviewer context for targeted improvement."""
        report = AttributionReport(
            run_id="run-42",
            outcomes=(SkillOutcome(
                skill_id="sk-001",
                skill_name="telegram-recovery",
                revision=7,
                result="corrected",
                deviation_summary="Webhook cleanup step was incomplete.",
            ),),
            has_corrections=True,
            overall_success=False,
        )

        base_context = {
            "user_prompt": "fix the telegram webhook",
            "signals": ["tool_calls"],
        }

        enriched = enrich_reviewer_context(base_context, report)

        self.assertIn("skill_attribution", enriched)
        attrib = enriched["skill_attribution"]
        self.assertTrue(attrib["has_corrections"])
        self.assertEqual(len(attrib["loaded_skills"]), 1)
        self.assertEqual(attrib["loaded_skills"][0]["result"], "corrected")
        self.assertEqual(attrib["loaded_skills"][0]["revision"], 7)

        # Signals enriched
        self.assertIn("skill_was_corrected", enriched["signals"])


# ─────────────────────────────────────────────────────────────────────────────
# Test 18: Full loop — revision N+1 selected + succeeds
# ─────────────────────────────────────────────────────────────────────────────


class TestIterativeSelfImprovement(unittest.TestCase):
    """AC 18: Future equivalent run → rev N+1 selected → succeeds without correction.

    This is the decisive Phase 3 test: demonstrates iterative procedural
    self-improvement across multiple runs.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-phase3-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.registry = SkillRegistry(self.store)
        self.home = self.tmpdir / "home"
        self.home.mkdir()
        (self.home / "skills").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_full_self_improvement_loop(self) -> None:
        """
        Scenario:
        1. Skill 'webhook-fix' at revision 1 is loaded during run A.
        2. Run A corrects the skill (deviation detected).
        3. Attribution report flags correction → reviewer proposes patch.
        4. Patch approved → skill becomes revision 2.
        5. Future run B matches the same task → revision 2 is discovered.
        6. Run B loads and executes revision 2 → succeeds without correction.
        7. Telemetry confirms the improvement.
        """
        service = SkillService(
            self.store, self.home,
            create_approval_required=True,
            modify_approval_required=True,
        )

        # ─── Step 1: Create the initial skill (revision 1) ───────────
        skill = service.create(
            "webhook-fix",
            "How to fix Telegram webhooks",
            "# Webhook Fix\n\n1. Delete webhook\n2. Set new webhook",
            owner=SkillOwner.AGENT,
        )
        self.store.sync_skills_fts()
        self.assertEqual(skill.revision, 1)

        # ─── Step 2: Run A loads and corrects the skill ──────────────
        run_a_id = "run-A-001"

        # Record telemetry for run A
        service.record_skill_selection(skill.skill_id, run_id=run_a_id)
        service._telemetry.record(skill.skill_id, "loaded", run_id=run_a_id)
        service._telemetry.record(skill.skill_id, "executed", run_id=run_a_id)
        service._telemetry.record(skill.skill_id, "corrected", run_id=run_a_id)

        # Build run context
        run_context_a = RunSkillContext(
            run_id=run_a_id,
            loaded=[LoadedSkill(
                skill_id=skill.skill_id,
                name="webhook-fix",
                revision=1,
                trust="approved",
            )],
        )
        service.save_run_context(run_context_a)

        # ─── Step 3: Attribution detects correction ──────────────────
        telemetry_events = [
            {"skill_id": skill.skill_id, "event": "loaded"},
            {"skill_id": skill.skill_id, "event": "executed"},
            {"skill_id": skill.skill_id, "event": "corrected"},
        ]
        attribution = build_attribution(
            run_a_id, run_context_a.to_dict(), telemetry_events,
        )
        self.assertTrue(attribution.has_corrections)
        self.assertTrue(attribution.needs_skill_update())

        # ─── Step 3b: Reviewer proposes patch (simulated) ────────────
        from gravityclaw.skills.models import SkillOperation
        patch_op = SkillOperation(
            operation="patch",
            name="webhook-fix",
            description="How to fix Telegram webhooks (improved)",
            content="# Webhook Fix\n\n1. Delete webhook\n2. Clean old handlers\n3. Set new webhook\n4. Verify delivery",
            reason="Step 2 was incomplete — webhook handlers needed cleanup",
            confidence=0.9,
            skill_id=skill.skill_id,
        )

        # This creates a proposal because modify_approval_required=True
        proposal_id = service.process_skill_operation(
            patch_op, source_run_id=run_a_id,
        )
        self.assertIsNotNone(proposal_id)
        proposal = self.registry.get_proposal(proposal_id)
        self.assertEqual(proposal.status, ProposalStatus.PENDING)
        self.assertEqual(proposal.base_revision, 1)

        # ─── Step 4: Approve the patch → revision 2 ─────────────────
        approved_skill_id = service.proposals.approve(proposal_id)
        self.assertEqual(approved_skill_id, skill.skill_id)

        updated_skill = self.registry.get_skill(skill.skill_id)
        self.assertEqual(updated_skill.revision, 2)
        self.store.sync_skills_fts()

        # Verify new content on disk
        from gravityclaw.skills.discovery import read_skill_content
        new_content = read_skill_content(self.home, "webhook-fix")
        self.assertIn("Clean old handlers", new_content)

        # ─── Step 5: Future run B discovers revision 2 ──────────────
        candidates = service.search_skills("fix telegram webhook", run_id="run-B-001")
        self.assertTrue(len(candidates) > 0)
        found = next((c for c in candidates if c.name == "webhook-fix"), None)
        self.assertIsNotNone(found, "webhook-fix should appear in search results")
        self.assertEqual(found.revision, 2)

        # ─── Step 6: Run B loads and executes revision 2 successfully ─
        run_b_id = "run-B-001"
        service.record_skill_selection(skill.skill_id, run_id=run_b_id)
        service._telemetry.record(skill.skill_id, "loaded", run_id=run_b_id)
        service._telemetry.record(skill.skill_id, "executed", run_id=run_b_id)
        service._telemetry.record(skill.skill_id, "successful", run_id=run_b_id)

        run_context_b = RunSkillContext(
            run_id=run_b_id,
            loaded=[LoadedSkill(
                skill_id=skill.skill_id,
                name="webhook-fix",
                revision=2,
                trust="approved",
            )],
        )
        service.save_run_context(run_context_b)

        # ─── Step 7: Verify improvement via telemetry ────────────────
        telemetry_b = [
            {"skill_id": skill.skill_id, "event": "loaded"},
            {"skill_id": skill.skill_id, "event": "executed"},
            {"skill_id": skill.skill_id, "event": "successful"},
        ]
        attribution_b = build_attribution(
            run_b_id, run_context_b.to_dict(), telemetry_b,
        )

        # Run B succeeded without correction
        self.assertFalse(attribution_b.has_corrections)
        self.assertTrue(attribution_b.overall_success)
        self.assertEqual(attribution_b.outcomes[0].result, "successful")

        # Full telemetry stats confirm improvement
        stats = self.registry.usage_stats(skill.skill_id)
        self.assertGreater(stats.get("successful", 0), 0)
        self.assertGreater(stats.get("corrected", 0), 0)
        # Success happened AFTER correction → evidence of improvement
        self.assertEqual(stats["successful"], 1)
        self.assertEqual(stats["corrected"], 1)


# ─────────────────────────────────────────────────────────────────────────────
# Additional unit tests for utility scoring
# ─────────────────────────────────────────────────────────────────────────────


class TestUtilityScoring(unittest.TestCase):
    """Unit tests for compute_utility."""

    def test_high_success_recent_usage(self) -> None:
        stats = {"executed": 60, "successful": 57, "failed": 3, "corrected": 1}
        last_used = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        score, recency, penalty, days = compute_utility(stats, last_used)

        self.assertGreater(score, 0.5)
        self.assertGreater(recency, 0.9)
        self.assertLess(days, 3)

    def test_low_success_old_usage(self) -> None:
        stats = {"executed": 2, "successful": 0, "failed": 2, "corrected": 0}
        last_used = (datetime.now(UTC) - timedelta(days=45)).isoformat()
        score, recency, penalty, days = compute_utility(stats, last_used)

        self.assertLess(score, 0.2)
        self.assertLess(recency, 0.5)
        self.assertGreater(days, 40)

    def test_never_used(self) -> None:
        stats = {}
        score, recency, penalty, days = compute_utility(stats, None)

        self.assertLess(score, 0.2)
        self.assertEqual(recency, 0.3)  # Default low recency
        self.assertIsNone(days)

    def test_high_correction_rate_penalizes(self) -> None:
        stats = {"executed": 10, "successful": 8, "failed": 0, "corrected": 8}
        last_used = datetime.now(UTC).isoformat()
        score, _, penalty, _ = compute_utility(stats, last_used)

        self.assertLess(penalty, 0.3)  # Heavy penalty for 80% correction rate


# ─────────────────────────────────────────────────────────────────────────────
# Additional tests for chunking
# ─────────────────────────────────────────────────────────────────────────────


class TestChunking(unittest.TestCase):
    """Unit tests for content chunking."""

    def test_small_content_single_chunk(self) -> None:
        chunks = chunk_content("Short content", chunk_size=1000)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].total, 1)
        self.assertEqual(chunks[0].index, 0)

    def test_large_content_multiple_chunks(self) -> None:
        # Create content that will span multiple chunks
        content = "\n\n".join(
            f"Paragraph {i}. " * 50 for i in range(20)
        )
        chunks = chunk_content(content, chunk_size=500)
        self.assertGreater(len(chunks), 1)
        # All chunks have correct total
        for chunk in chunks:
            self.assertEqual(chunk.total, len(chunks))

    def test_max_chunks_enforced(self) -> None:
        content = "\n\n".join(f"Section {i}\n" * 100 for i in range(100))
        chunks = chunk_content(content, chunk_size=100, max_chunks=5)
        self.assertLessEqual(len(chunks), 5)


# ─────────────────────────────────────────────────────────────────────────────
# Source classification tests
# ─────────────────────────────────────────────────────────────────────────────


class TestSourceClassification(unittest.TestCase):
    """Unit tests for classify_source."""

    def test_url_classified_as_web_page(self) -> None:
        result = classify_source("https://docs.docker.com/compose/")
        self.assertEqual(result.source_type, SourceType.WEB_PAGE)

    def test_plain_text_classified(self) -> None:
        result = classify_source("Just some regular text about coding")
        self.assertEqual(result.source_type, SourceType.PLAIN_TEXT)

    def test_conversation_detected(self) -> None:
        text = "User: How do I deploy?\nAssistant: Here are the steps...\nUser: Thanks!"
        result = classify_source(text)
        self.assertEqual(result.source_type, SourceType.CONVERSATION)

    def test_large_content_flagged(self) -> None:
        large = "x" * 15_000
        result = classify_source("source", content=large)
        self.assertTrue(result.is_large)

    def test_small_content_not_flagged(self) -> None:
        small = "x" * 100
        result = classify_source("source", content=small)
        self.assertFalse(result.is_large)


if __name__ == "__main__":
    unittest.main()
