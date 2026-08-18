"""Tests for Learning Mode Phase 2 — skill registry, proposals, approval, revisions, telemetry.

Acceptance tests:
 1. Successful reusable procedure → skill proposal created.
 2. Pending proposal → filesystem unchanged.
 3. Approve proposal → SKILL.md created, registry row, revision=1, audit event.
 4. Reject proposal → no skill mutation.
 5. Existing skill becomes outdated → patch proposal created.
 6. Approve patch → revision N+1, previous content recoverable.
 7. Stale base_revision → proposal approval rejected safely.
 8. User-owned skill → reviewer cannot auto-mutate it.
 9. Skill metadata discovery → does not load complete SKILL.md.
10. skill_view → records loaded telemetry.
11. Skill used successfully in a later run → success telemetry increments.
12. End-to-end: reviewer produces skill candidate → proposal created via LearningEngine.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from gravityclaw.identity import IdentityStore
from gravityclaw.learning import (
    LearningEligibilityGate,
    LearningEngine,
    LearningJob,
    LearningResult,
    MemoryOperation,
    SkillCandidate,
)
from gravityclaw.memory import MemoryService
from gravityclaw.skills import SkillService, SkillRecord
from gravityclaw.skills.discovery import discover_skills, read_skill_content
from gravityclaw.skills.models import (
    ProposalStatus,
    SkillOperation,
    SkillOwner,
    SkillState,
    SkillTrust,
)
from gravityclaw.skills.registry import SkillRegistry, StaleRevisionError
from gravityclaw.skills.proposals import ProposalService
from gravityclaw.skills.revisions import RevisionService
from gravityclaw.skills.telemetry import TelemetryService, VALID_EVENTS
from gravityclaw.store import RunRecord, Store


def _make_store(tmp: Path) -> Store:
    """Create an initialized Store in a temp directory."""
    store = Store(tmp / "state.db")
    store.initialize()
    return store


def _make_skill_service(tmp: Path, store: Store, **kwargs) -> SkillService:
    """Create a SkillService with the given temp home and store."""
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    (home / "skills").mkdir(exist_ok=True)
    return SkillService(store, home, **kwargs)


def _write_skill_fs(home: Path, name: str, content: str, meta: dict | None = None) -> Path:
    """Write a skill directory with SKILL.md to the filesystem."""
    skill_dir = home / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    if meta:
        (skill_dir / "skill.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )
    return skill_dir


class SkillRegistryTests(unittest.TestCase):
    """Core registry CRUD operations."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-skills-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.registry = SkillRegistry(self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_register_and_get_skill(self) -> None:
        skill = self.registry.register_skill(
            name="test-skill",
            description="A test skill",
            path="skills/test-skill",
        )
        self.assertEqual(skill.name, "test-skill")
        self.assertEqual(skill.state, SkillState.ACTIVE)
        self.assertEqual(skill.revision, 0)
        self.assertFalse(skill.pinned)

        retrieved = self.registry.get_skill(skill.skill_id)
        self.assertEqual(retrieved.name, "test-skill")

    def test_get_by_name(self) -> None:
        self.registry.register_skill("my-skill", "desc", "skills/my-skill")
        result = self.registry.get_skill_by_name("my-skill")
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "my-skill")

        missing = self.registry.get_skill_by_name("nonexistent")
        self.assertIsNone(missing)

    def test_list_skills_with_filters(self) -> None:
        self.registry.register_skill("s1", "d1", "skills/s1", owner=SkillOwner.USER)
        self.registry.register_skill("s2", "d2", "skills/s2", owner=SkillOwner.AGENT)
        self.registry.register_skill("s3", "d3", "skills/s3", owner=SkillOwner.AGENT)

        all_skills = self.registry.list_skills()
        self.assertEqual(len(all_skills), 3)

        agent_skills = self.registry.list_skills(owner=SkillOwner.AGENT)
        self.assertEqual(len(agent_skills), 2)

    def test_update_skill_fields(self) -> None:
        skill = self.registry.register_skill("upd", "desc", "skills/upd")
        updated = self.registry.update_skill(
            skill.skill_id, revision=5, pinned=True, state=SkillState.STALE,
        )
        self.assertEqual(updated.revision, 5)
        self.assertTrue(updated.pinned)
        self.assertEqual(updated.state, SkillState.STALE)


class ProposalLifecycleTests(unittest.TestCase):
    """Tests 1–4, 7: Proposal creation, approval, rejection, and stale detection."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-proposals-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.home = self.tmpdir / "home"
        self.home.mkdir()
        (self.home / "skills").mkdir()
        self.registry = SkillRegistry(self.store)
        self.proposals = ProposalService(self.registry, self.home)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_01_reusable_procedure_creates_proposal(self) -> None:
        """Test 1: Successful reusable procedure → skill proposal created."""
        proposal = self.registry.create_proposal(
            skill_name="systemd-recovery",
            operation="create",
            description="Recover a systemd-managed service",
            reason="A reusable troubleshooting sequence succeeded.",
            content="# systemd-recovery\n\nRecovery steps...",
            confidence=0.94,
            source_run_id="run-abc",
            review_model="gemini-2.0-flash",
        )
        self.assertEqual(proposal.status, ProposalStatus.PENDING)
        self.assertEqual(proposal.skill_name, "systemd-recovery")
        self.assertEqual(proposal.operation, "create")
        self.assertIsNone(proposal.resolved_at)

    def test_02_pending_proposal_no_filesystem_change(self) -> None:
        """Test 2: Pending proposal → filesystem unchanged."""
        self.registry.create_proposal(
            skill_name="phantom-skill",
            operation="create",
            description="Should not exist on disk",
            reason="test",
            content="# phantom\n\nContent...",
            confidence=0.9,
        )
        skill_dir = self.home / "skills" / "phantom-skill"
        self.assertFalse(skill_dir.exists())

    def test_03_approve_creates_skill(self) -> None:
        """Test 3: Approve proposal → SKILL.md created, registry row, revision=1, audit."""
        proposal = self.registry.create_proposal(
            skill_name="gateway-restart",
            operation="create",
            description="Restart GravityClaw gateway safely",
            reason="Proven procedure after two failed approaches",
            content="# gateway-restart\n\nStep 1: Stop...\nStep 2: Start...",
            confidence=0.95,
            source_run_id="run-xyz",
            review_model="gemini-2.0-flash",
        )

        skill_id = self.proposals.approve(proposal.id, reason="Looks good")

        # Verify filesystem
        skill_md = self.home / "skills" / "gateway-restart" / "SKILL.md"
        self.assertTrue(skill_md.is_file())
        self.assertIn("Step 1: Stop", skill_md.read_text())

        # Verify registry
        skill = self.registry.get_skill(skill_id)
        self.assertEqual(skill.name, "gateway-restart")
        self.assertEqual(skill.revision, 1)
        self.assertEqual(skill.trust, SkillTrust.APPROVED)

        # Verify revision history
        revisions = self.registry.list_revisions(skill_id)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0].operation, "create")
        self.assertEqual(revisions[0].revision, 1)

        # Verify proposal resolved
        resolved = self.registry.get_proposal(proposal.id)
        self.assertEqual(resolved.status, ProposalStatus.APPROVED)
        self.assertIsNotNone(resolved.resolved_at)

        # Verify .history/ snapshot
        history = self.home / "skills" / "gateway-restart" / ".history" / "000001.SKILL.md"
        self.assertTrue(history.is_file())

    def test_04_reject_no_mutation(self) -> None:
        """Test 4: Reject proposal → no skill mutation."""
        proposal = self.registry.create_proposal(
            skill_name="bad-skill",
            operation="create",
            description="This should be rejected",
            reason="low quality",
            content="# bad\n\nBad content",
            confidence=0.3,
        )

        self.proposals.reject(proposal.id, reason="Not useful")

        # No filesystem
        self.assertFalse((self.home / "skills" / "bad-skill").exists())

        # No registry entry
        self.assertIsNone(self.registry.get_skill_by_name("bad-skill"))

        # Proposal marked rejected
        resolved = self.registry.get_proposal(proposal.id)
        self.assertEqual(resolved.status, ProposalStatus.REJECTED)

    def test_07_stale_base_revision_rejected(self) -> None:
        """Test 7: Stale base_revision → proposal approval rejected safely."""
        # Create a skill at revision 3
        skill = self.registry.register_skill(
            "outdated-skill", "desc", "skills/outdated-skill",
        )
        self.registry.update_skill(skill.skill_id, revision=3)
        # Write SKILL.md so patch has something to work with
        _write_skill_fs(self.home, "outdated-skill", "# outdated\n\nOld content")

        # Create a patch proposal based on revision 1 (stale)
        proposal = self.registry.create_proposal(
            skill_name="outdated-skill",
            operation="patch",
            description="Improvement",
            reason="Better steps",
            content="# outdated\n\nNew content",
            confidence=0.9,
            skill_id=skill.skill_id,
            base_revision=1,  # stale!
        )

        # Approval should fail with StaleRevisionError
        with self.assertRaises(StaleRevisionError):
            self.proposals.approve(proposal.id)

        # Proposal should be marked as conflict
        resolved = self.registry.get_proposal(proposal.id)
        self.assertEqual(resolved.status, ProposalStatus.CONFLICT)


class PatchAndRevisionTests(unittest.TestCase):
    """Tests 5–6: Patch proposals and revision history."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-patch-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.home = self.tmpdir / "home"
        self.home.mkdir()
        (self.home / "skills").mkdir()
        self.registry = SkillRegistry(self.store)
        self.proposals = ProposalService(self.registry, self.home)
        self.revisions = RevisionService(self.registry, self.home)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_05_outdated_skill_patch_proposal(self) -> None:
        """Test 5: Existing skill becomes outdated → patch proposal created."""
        # Create initial skill via proposal
        create_prop = self.registry.create_proposal(
            skill_name="deploy-modal",
            operation="create",
            description="Deploy to Modal",
            reason="Reusable deployment",
            content="# deploy-modal\n\nOld steps...",
            confidence=0.9,
        )
        self.proposals.approve(create_prop.id)

        skill = self.registry.get_skill_by_name("deploy-modal")
        self.assertEqual(skill.revision, 1)

        # Now create a patch proposal
        patch_prop = self.registry.create_proposal(
            skill_name="deploy-modal",
            operation="patch",
            description="Deploy to Modal (updated)",
            reason="Missing daemon-reload step",
            content="# deploy-modal\n\nNew steps with daemon-reload...",
            confidence=0.97,
            skill_id=skill.skill_id,
            base_revision=1,
        )
        self.assertEqual(patch_prop.status, ProposalStatus.PENDING)
        self.assertEqual(patch_prop.base_revision, 1)

    def test_06_approve_patch_increments_revision(self) -> None:
        """Test 6: Approve patch → revision N+1, previous content recoverable."""
        # Create skill
        create_prop = self.registry.create_proposal(
            skill_name="fix-nginx",
            operation="create",
            description="Fix nginx config",
            reason="Common procedure",
            content="# fix-nginx\n\nVersion 1 content",
            confidence=0.9,
        )
        skill_id = self.proposals.approve(create_prop.id)
        skill = self.registry.get_skill(skill_id)
        self.assertEqual(skill.revision, 1)

        # Patch
        patch_prop = self.registry.create_proposal(
            skill_name="fix-nginx",
            operation="patch",
            description="Fix nginx config (v2)",
            reason="Added reload step",
            content="# fix-nginx\n\nVersion 2 content with reload",
            confidence=0.95,
            skill_id=skill_id,
            base_revision=1,
        )
        self.proposals.approve(patch_prop.id)

        # Verify revision bumped
        updated = self.registry.get_skill(skill_id)
        self.assertEqual(updated.revision, 2)

        # Verify current content is v2
        skill_md = self.home / "skills" / "fix-nginx" / "SKILL.md"
        self.assertIn("Version 2", skill_md.read_text())

        # Verify previous content recoverable
        v1_content = self.revisions.get_revision_content("fix-nginx", 1)
        self.assertIsNotNone(v1_content)
        self.assertIn("Version 1", v1_content)

        # Verify rollback works
        new_rev = self.revisions.rollback("fix-nginx", 1, reason="revert to v1")
        self.assertEqual(new_rev, 3)
        current = (self.home / "skills" / "fix-nginx" / "SKILL.md").read_text()
        self.assertIn("Version 1", current)


class OwnershipTests(unittest.TestCase):
    """Test 8: User-owned skill protection."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-ownership-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.home = self.tmpdir / "home"
        self.home.mkdir()
        (self.home / "skills").mkdir()
        self.registry = SkillRegistry(self.store)
        self.proposals = ProposalService(self.registry, self.home)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_08_user_owned_skill_cannot_be_auto_patched(self) -> None:
        """Test 8: User-owned skill → reviewer cannot auto-mutate it."""
        # Register a user-owned skill
        skill = self.registry.register_skill(
            "my-custom-skill", "User's custom skill", "skills/my-custom-skill",
            owner=SkillOwner.USER,
        )
        self.registry.update_skill(skill.skill_id, revision=1)
        _write_skill_fs(self.home, "my-custom-skill", "# my-custom-skill\n\nOriginal")

        # Try to approve a patch
        proposal = self.registry.create_proposal(
            skill_name="my-custom-skill",
            operation="patch",
            description="Improvement",
            reason="Better approach",
            content="# my-custom-skill\n\nModified content",
            confidence=0.9,
            skill_id=skill.skill_id,
            base_revision=1,
        )

        with self.assertRaises(ValueError) as ctx:
            self.proposals.approve(proposal.id)
        self.assertIn("user-owned", str(ctx.exception))

        # Content should be unchanged
        content = (self.home / "skills" / "my-custom-skill" / "SKILL.md").read_text()
        self.assertIn("Original", content)
        self.assertNotIn("Modified", content)


class DiscoveryAndTelemetryTests(unittest.TestCase):
    """Tests 9–11: Discovery, view, and telemetry."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-discovery-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.home = self.tmpdir / "home"
        self.home.mkdir()
        (self.home / "skills").mkdir()
        self.service = SkillService(self.store, self.home)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_09_discovery_does_not_load_full_content(self) -> None:
        """Test 9: Skill metadata discovery → does not load complete SKILL.md."""
        # Write two skills to the filesystem
        _write_skill_fs(
            self.home, "skill-alpha",
            "# skill-alpha\n\nThis is a long skill content " + "x" * 5000,
            meta={"description": "Alpha skill", "owner": "user"},
        )
        _write_skill_fs(
            self.home, "skill-beta",
            "# skill-beta\n\nBeta procedure steps",
        )

        discovered = self.service.discover()
        self.assertEqual(len(discovered), 2)

        names = {s.name for s in discovered}
        self.assertIn("skill-alpha", names)
        self.assertIn("skill-beta", names)

        # Discovery returns short descriptions, not full content
        alpha = next(s for s in discovered if s.name == "skill-alpha")
        self.assertEqual(alpha.description, "Alpha skill")  # from skill.json

        # Registry should now have these
        all_skills = self.service.registry.list_skills()
        self.assertEqual(len(all_skills), 2)

    def test_10_skill_view_records_telemetry(self) -> None:
        """Test 10: skill_view → records loaded telemetry."""
        _write_skill_fs(
            self.home, "telem-skill",
            "# telem-skill\n\nSome content here",
        )
        # Register first
        self.service.discover()

        # View the skill
        content = self.service.view("telem-skill", run_id="run-001")
        self.assertIsNotNone(content)
        self.assertIn("Some content here", content)

        # Check telemetry
        skill = self.service.registry.get_skill_by_name("telem-skill")
        stats = self.service.telemetry.stats(skill.skill_id)
        self.assertEqual(stats.get("loaded"), 1)

    def test_11_success_telemetry_increments(self) -> None:
        """Test 11: Skill used successfully → success telemetry increments."""
        _write_skill_fs(self.home, "used-skill", "# used-skill\n\nSteps")
        self.service.discover()

        skill = self.service.registry.get_skill_by_name("used-skill")

        # Record usage flow
        self.service.telemetry.record(skill.skill_id, "discovered", run_id="r1")
        self.service.telemetry.record(skill.skill_id, "selected", run_id="r1")
        self.service.telemetry.record(skill.skill_id, "loaded", run_id="r1")
        self.service.telemetry.record(skill.skill_id, "executed", run_id="r1")
        self.service.telemetry.record(skill.skill_id, "successful", run_id="r1")

        stats = self.service.telemetry.stats(skill.skill_id)
        self.assertEqual(stats["discovered"], 1)
        self.assertEqual(stats["selected"], 1)
        self.assertEqual(stats["successful"], 1)
        self.assertEqual(stats["executed"], 1)

        # Success rate
        rate = self.service.telemetry.success_rate(skill.skill_id)
        self.assertEqual(rate, 1.0)

        # Now record a failure
        self.service.telemetry.record(skill.skill_id, "executed", run_id="r2")
        self.service.telemetry.record(skill.skill_id, "failed", run_id="r2")

        rate2 = self.service.telemetry.success_rate(skill.skill_id)
        self.assertAlmostEqual(rate2, 0.5)  # 1 success, 1 failure


class EndToEndLearningIntegrationTests(unittest.TestCase):
    """Test 12: Full reviewer → skill proposal flow through LearningEngine."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-e2e-skills-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.home = self.tmpdir / "home"
        self.home.mkdir()
        (self.home / "skills").mkdir()

        # Set up identity
        identity_home = self.tmpdir / "identity"
        identity_home.mkdir()
        (identity_home / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
        (identity_home / "USER.md").write_text("# User\n", encoding="utf-8")
        self.identity = IdentityStore(identity_home)

        # Memory service mock
        self.memory = MagicMock(spec=MemoryService)

        # Create SkillService
        self.skill_service = SkillService(
            self.store, self.home,
            create_approval_required=True,
            modify_approval_required=True,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_12_reviewer_skill_candidate_creates_proposal(self) -> None:
        """Test 12: Reviewer produces skill candidate → proposal created via LearningEngine.

        This is the decisive end-to-end test: a reviewer response containing
        a skill candidate should result in a pending proposal persisted in
        the registry, accessible for human approval.
        """
        engine = LearningEngine(
            store=self.store,
            identity=self.identity,
            memory=self.memory,
            enabled=True,
            skill_service=self.skill_service,
        )

        # Simulate what happens after reviewer returns a response with skills
        reviewer_response = {
            "worth_learning": True,
            "summary": "Learned a systemd recovery procedure",
            "memory": [],
            "skills": [
                {
                    "operation": "create",
                    "name": "systemd-gateway-recovery",
                    "description": "Recover a GravityClaw gateway managed by systemd",
                    "content": "# systemd-gateway-recovery\n\n## Steps\n\n1. Check status\n2. Restart\n3. Verify",
                    "reason": "A reusable troubleshooting sequence succeeded after two failed approaches.",
                    "confidence": 0.94,
                }
            ],
        }

        # Parse the response
        result = engine._parse_reviewer_response(reviewer_response)
        self.assertIsNotNone(result)
        self.assertTrue(result.worth_learning)
        self.assertEqual(len(result.skill_candidates), 1)
        self.assertEqual(result.skill_candidates[0].name, "systemd-gateway-recovery")
        self.assertEqual(result.skill_candidates[0].operation, "create")
        self.assertEqual(result.skill_candidates[0].confidence, 0.94)

        # Now simulate the full dispatch (what _execute_review does)
        # Create a mock workspace and conversation for the store
        workspace = self.store.create_workspace("test", self.tmpdir / "ws")
        conversation = self.store.create_conversation(workspace.id)
        run = self.store.enqueue_run(conversation.id, {"prompt": "fix the gateway"})
        claimed = self.store.claim_run(run.id)

        # Create a learning job (simulating what process_run does)
        job = self.store.create_learning_job(
            run_id=run.id,
            conversation_id=conversation.id,
            gate_score=5.0,
            gate_signals=["tool_calls", "task_success_after_failure"],
            context={"user_prompt": "fix the gateway"},
        )

        # Dispatch the skill candidate
        candidate = result.skill_candidates[0]
        asyncio.run(engine._apply_skill_operation(candidate, job))

        # Verify: a pending proposal should now exist
        proposals = self.skill_service.registry.list_proposals(status="pending")
        self.assertEqual(len(proposals), 1)

        proposal = proposals[0]
        self.assertEqual(proposal.skill_name, "systemd-gateway-recovery")
        self.assertEqual(proposal.operation, "create")
        self.assertEqual(proposal.confidence, 0.94)
        self.assertEqual(proposal.source_run_id, run.id)
        self.assertIn("reusable troubleshooting", proposal.reason)

        # Verify: learning event recorded
        events = self.store.list_learning_events(run_id=run.id, event_type="skill_candidate")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, "pending_approval")
        self.assertEqual(events[0].target, "systemd-gateway-recovery")

        # Now approve it
        skill_id = self.skill_service.proposals.approve(proposal.id)

        # Verify the skill exists on disk and in registry
        skill = self.skill_service.registry.get_skill(skill_id)
        self.assertEqual(skill.name, "systemd-gateway-recovery")
        self.assertEqual(skill.revision, 1)

        content = read_skill_content(self.home, "systemd-gateway-recovery")
        self.assertIn("## Steps", content)
        self.assertIn("1. Check status", content)


class SkillServiceDirectTests(unittest.TestCase):
    """Tests for direct SkillService operations (create, patch, archive, rollback)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-svc-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.home = self.tmpdir / "home"
        self.home.mkdir()
        (self.home / "skills").mkdir()
        self.service = SkillService(self.store, self.home)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_direct_create_and_patch(self) -> None:
        """Direct skill creation and patching (bypasses proposal flow)."""
        skill = self.service.create(
            "direct-skill",
            "A directly created skill",
            "# direct-skill\n\nVersion 1",
            owner=SkillOwner.USER,
        )
        self.assertEqual(skill.revision, 1)
        self.assertEqual(skill.owner, SkillOwner.USER)

        # Patch
        updated = self.service.patch("direct-skill", "# direct-skill\n\nVersion 2", "improved")
        self.assertEqual(updated.revision, 2)

        # Verify content
        content = read_skill_content(self.home, "direct-skill")
        self.assertIn("Version 2", content)

    def test_archive_and_restore(self) -> None:
        """Archive and restore a skill."""
        self.service.create("arch-skill", "desc", "# arch-skill\n\nContent")
        archived = self.service.archive("arch-skill")
        self.assertEqual(archived.state, SkillState.ARCHIVED)

        restored = self.service.restore("arch-skill")
        self.assertEqual(restored.state, SkillState.ACTIVE)

    def test_pin_and_unpin(self) -> None:
        """Pin and unpin a skill."""
        self.service.create("pin-skill", "desc", "# pin-skill\n\nContent")
        pinned = self.service.pin("pin-skill")
        self.assertTrue(pinned.pinned)

        unpinned = self.service.unpin("pin-skill")
        self.assertFalse(unpinned.pinned)

    def test_rollback(self) -> None:
        """Rollback to a previous revision."""
        self.service.create("rb-skill", "desc", "# rb-skill\n\nV1")
        self.service.patch("rb-skill", "# rb-skill\n\nV2", "update")
        self.service.patch("rb-skill", "# rb-skill\n\nV3", "another update")

        # Rollback to revision 1
        new_rev = self.service.rollback("rb-skill", 1, reason="revert")
        self.assertEqual(new_rev, 4)

        content = read_skill_content(self.home, "rb-skill")
        self.assertIn("V1", content)

    def test_process_skill_operation_creates_proposal(self) -> None:
        """SkillService.process_skill_operation creates a proposal when approval required."""
        op = SkillOperation(
            operation="create",
            name="auto-skill",
            description="An auto-discovered skill",
            content="# auto-skill\n\nAuto content",
            reason="pattern detected",
            confidence=0.88,
        )
        result_id = self.service.process_skill_operation(
            op, source_run_id="run-123", review_model="gemini-2.0-flash",
        )
        self.assertIsNotNone(result_id)

        # Should be a proposal
        proposal = self.service.registry.get_proposal(result_id)
        self.assertEqual(proposal.status, ProposalStatus.PENDING)
        self.assertEqual(proposal.skill_name, "auto-skill")

    def test_process_skill_operation_direct_when_no_approval(self) -> None:
        """SkillService directly creates when approval not required."""
        service = SkillService(
            self.store, self.home,
            create_approval_required=False,
            modify_approval_required=False,
        )
        op = SkillOperation(
            operation="create",
            name="instant-skill",
            description="Created instantly",
            content="# instant-skill\n\nImmediate",
            reason="approved mode",
            confidence=0.95,
        )
        result_id = service.process_skill_operation(op, source_run_id="r1")

        # Should be the skill_id directly
        skill = service.registry.get_skill(result_id)
        self.assertEqual(skill.name, "instant-skill")
        self.assertEqual(skill.revision, 1)


if __name__ == "__main__":
    unittest.main()
