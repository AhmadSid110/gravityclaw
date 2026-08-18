"""Tests for Skills Phase 2.5 — Skill Runtime.

Acceptance tests:
 1. Relevant task → registry search returns matching skill.
 2. Unrelated task → irrelevant skill not injected.
 3. Candidate discovery → only metadata enters prompt.
 4. Agent calls skill_view → exact approved revision returned.
 5. skill_view(reference) → loads only requested reference file.
 6. ../ traversal → rejected.
 7. symlink escape → rejected.
 8. oversized skill → bounded safely.
 9. skill load → telemetry records revision + run_id.
10. skill execution succeeds → successful telemetry recorded.
11. skill contradicts runtime evidence → agent can deviate.
12. agent finds better procedure → skill_improve proposal created,
    original skill remains unchanged.
13. user-owned skill → agent may read it, cannot autonomously modify it.
14. bundled skill → readable, immutable.
15. future equivalent task → approved learned skill is discovered
    and successful procedure is attempted first.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from gravityclaw.skills import (
    LoadedSkill,
    PromptIntegration,
    RunSkillContext,
    SkillCandidate,
    SkillDiscovery,
    SkillManageError,
    SkillManageResult,
    SkillService,
    SkillViewError,
    SkillViewResult,
    skill_manage,
    skill_view,
)
from gravityclaw.skills.discovery import SKILL_FILENAME, ensure_skill_directory
from gravityclaw.skills.models import (
    ProposalStatus,
    SkillOwner,
    SkillState,
    SkillTrust,
)
from gravityclaw.skills.registry import SkillRegistry
from gravityclaw.skills.runtime import (
    MAX_FILE_BYTES,
    MAX_SKILL_BYTES,
    SKILL_PREAMBLE,
    _validate_no_escape,
    _validate_relative_path,
)
from gravityclaw.skills.telemetry import TelemetryService, VALID_EVENTS
from gravityclaw.store import Store, utc_now


def _make_store(tmp: Path) -> Store:
    """Create an initialized Store in a temp directory."""
    store = Store(tmp / "state.db")
    store.initialize()
    return store


def _make_service(tmp: Path, store: Store, **kwargs) -> SkillService:
    """Create a SkillService with the given temp home and store."""
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    (home / "skills").mkdir(exist_ok=True)
    return SkillService(store, home, **kwargs)


def _write_skill(home: Path, name: str, content: str, meta: dict | None = None) -> Path:
    """Write a skill directory with SKILL.md to the filesystem."""
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
) -> str:
    """Register a skill and return its ID."""
    record = registry.register_skill(
        name=name, description=description,
        path=f"skills/{name}", owner=owner, trust=trust,
    )
    if revision > 0:
        registry.update_skill(record.skill_id, revision=revision)
    return record.skill_id


class Test01_RelevantTaskSearch(unittest.TestCase):
    """1. Relevant task → registry search returns matching skill."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-rt-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.svc = _make_service(self.tmpdir, self.store)
        self.home = self.tmpdir / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def test_fts_search_finds_relevant_skill(self):
        # Create and register a skill
        _write_skill(self.home, "telegram-recovery",
                     "# Telegram Recovery\nRecover Telegram bot connectivity.")
        _register_skill(
            self.svc.registry, "telegram-recovery",
            "Diagnose and recover Telegram bot connectivity issues",
        )
        self.store.sync_skills_fts()

        # Search for a related task
        candidates = self.svc.search_skills("telegram bot not responding")
        self.assertGreater(len(candidates), 0)
        names = [c.name for c in candidates]
        self.assertIn("telegram-recovery", names)

    def test_like_fallback_finds_relevant_skill(self):
        # Register without FTS sync — tests LIKE fallback
        _write_skill(self.home, "docker-deploy",
                     "# Docker Deploy\nDeploy containers to production.")
        _register_skill(
            self.svc.registry, "docker-deploy",
            "Deploy Docker containers to production environments",
        )
        # Don't sync FTS — force LIKE fallback
        # Discovery should still work via LIKE search
        discovery = SkillDiscovery(self.svc.registry, self.store)
        candidates = discovery._like_search(["docker", "deploy"], 8, False)
        self.assertGreater(len(candidates), 0)


class Test02_UnrelatedTaskNotInjected(unittest.TestCase):
    """2. Unrelated task → irrelevant skill not injected."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-rt-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.svc = _make_service(self.tmpdir, self.store)
        self.home = self.tmpdir / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def test_unrelated_query_returns_no_match(self):
        _write_skill(self.home, "telegram-recovery",
                     "# Telegram Recovery\nRecover Telegram bot.")
        _register_skill(
            self.svc.registry, "telegram-recovery",
            "Diagnose Telegram connectivity issues",
        )
        self.store.sync_skills_fts()

        # Search for something completely unrelated
        candidates = self.svc.search_skills("bake chocolate cake recipe")
        # Either empty or doesn't contain telegram-recovery
        telegram_found = any(c.name == "telegram-recovery" for c in candidates)
        # FTS on unrelated terms should not match
        self.assertFalse(telegram_found)


class Test03_CandidateMetadataOnly(unittest.TestCase):
    """3. Candidate discovery → only metadata enters prompt."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-rt-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.svc = _make_service(self.tmpdir, self.store)
        self.home = self.tmpdir / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def test_prompt_contains_metadata_not_content(self):
        skill_content = "# Modal Deploy\nStep 1: Do X\nStep 2: Do Y\nLong procedure..."
        _write_skill(self.home, "modal-deployment", skill_content)
        _register_skill(
            self.svc.registry, "modal-deployment",
            "Deploy workloads on Modal platform",
        )
        self.store.sync_skills_fts()

        prompt, candidates = self.svc.build_skill_prompt("deploy to modal")
        # Prompt should contain the name and description
        self.assertIn("modal-deployment", prompt)
        self.assertIn("Deploy workloads on Modal platform", prompt)
        # But NOT the full procedure content
        self.assertNotIn("Step 1: Do X", prompt)
        self.assertNotIn("Step 2: Do Y", prompt)
        # It should instruct the agent to call skill_view
        self.assertIn("skill_view", prompt)


class Test04_SkillViewReturnsExactRevision(unittest.TestCase):
    """4. Agent calls skill_view → exact approved revision returned."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-rt-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.svc = _make_service(self.tmpdir, self.store)
        self.home = self.tmpdir / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def test_skill_view_returns_content_and_metadata(self):
        content = "# Modal Deploy\n\nProcedure for deploying to Modal."
        _write_skill(self.home, "modal-deployment", content)
        skill_id = _register_skill(
            self.svc.registry, "modal-deployment",
            "Deploy workloads on Modal", revision=4, trust=SkillTrust.APPROVED,
        )

        result = self.svc.skill_view("modal-deployment", run_id="run-100")
        self.assertEqual(result.skill_id, skill_id)
        self.assertEqual(result.name, "modal-deployment")
        self.assertEqual(result.revision, 4)
        self.assertEqual(result.trust, SkillTrust.APPROVED)
        self.assertEqual(result.content, content)
        self.assertIsNone(result.path)
        self.assertFalse(result.truncated)


class Test05_SkillViewReference(unittest.TestCase):
    """5. skill_view(reference) → loads only requested reference file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-rt-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.svc = _make_service(self.tmpdir, self.store)
        self.home = self.tmpdir / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def test_loads_reference_file(self):
        skill_dir = _write_skill(self.home, "modal-deployment",
                                 "# Modal Deploy\nMain procedure.")
        ref_dir = skill_dir / "references"
        ref_dir.mkdir(exist_ok=True)
        ref_content = "# Persistence\nHow to configure persistence."
        (ref_dir / "persistence.md").write_text(ref_content, encoding="utf-8")
        _register_skill(
            self.svc.registry, "modal-deployment", "Deploy to Modal",
        )

        result = self.svc.skill_view(
            "modal-deployment", path="references/persistence.md",
        )
        self.assertEqual(result.content, ref_content)
        self.assertEqual(result.path, "references/persistence.md")


class Test06_PathTraversalRejected(unittest.TestCase):
    """6. ../ traversal → rejected."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-rt-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.svc = _make_service(self.tmpdir, self.store)
        self.home = self.tmpdir / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def test_dotdot_rejected(self):
        _write_skill(self.home, "test-skill", "# Test\nContent.")
        _register_skill(self.svc.registry, "test-skill", "Test skill")

        with self.assertRaises(SkillViewError) as ctx:
            self.svc.skill_view("test-skill", path="../../../etc/passwd")
        self.assertIn("traversal", str(ctx.exception).lower())

    def test_embedded_dotdot_rejected(self):
        _write_skill(self.home, "test-skill", "# Test\nContent.")
        _register_skill(self.svc.registry, "test-skill", "Test skill")

        with self.assertRaises(SkillViewError) as ctx:
            self.svc.skill_view("test-skill", path="references/../../secret.txt")
        self.assertIn("traversal", str(ctx.exception).lower())

    def test_absolute_path_rejected(self):
        _write_skill(self.home, "test-skill", "# Test\nContent.")
        _register_skill(self.svc.registry, "test-skill", "Test skill")

        with self.assertRaises(SkillViewError) as ctx:
            self.svc.skill_view("test-skill", path="/etc/passwd")
        self.assertIn("absolute", str(ctx.exception).lower())

    def test_hidden_path_rejected(self):
        _write_skill(self.home, "test-skill", "# Test\nContent.")
        _register_skill(self.svc.registry, "test-skill", "Test skill")

        with self.assertRaises(SkillViewError) as ctx:
            self.svc.skill_view("test-skill", path=".history/000001.SKILL.md")
        self.assertIn("hidden", str(ctx.exception).lower())


class Test07_SymlinkEscapeRejected(unittest.TestCase):
    """7. symlink escape → rejected."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-rt-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.svc = _make_service(self.tmpdir, self.store)
        self.home = self.tmpdir / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def test_symlink_outside_skill_dir_rejected(self):
        skill_dir = _write_skill(self.home, "test-skill", "# Test\nContent.")
        _register_skill(self.svc.registry, "test-skill", "Test skill")

        # Create a secret file outside the skill directory
        secret = self.home / "secret.txt"
        secret.write_text("TOP SECRET", encoding="utf-8")

        # Create a symlink inside the skill directory pointing outside
        link_path = skill_dir / "escape_link.txt"
        try:
            link_path.symlink_to(secret)
        except OSError:
            self.skipTest("Cannot create symlinks on this platform")

        # Attempt to read via the symlink should be rejected
        with self.assertRaises(SkillViewError) as ctx:
            self.svc.skill_view("test-skill", path="escape_link.txt")
        self.assertIn("escapes", str(ctx.exception).lower())


class Test08_OversizedSkillBounded(unittest.TestCase):
    """8. oversized skill → bounded safely."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-rt-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.svc = _make_service(self.tmpdir, self.store)
        self.home = self.tmpdir / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def test_oversized_content_truncated(self):
        # Create a skill larger than the max limit
        large_content = "# Big Skill\n" + ("x" * (MAX_SKILL_BYTES + 1000))
        _write_skill(self.home, "big-skill", large_content)
        _register_skill(self.svc.registry, "big-skill", "A very large skill")

        result = self.svc.skill_view("big-skill", max_bytes=1000)
        self.assertLessEqual(len(result.content.encode("utf-8")), 1000)
        self.assertTrue(result.truncated)

    def test_custom_max_bytes_respected(self):
        content = "# Small\n" + ("y" * 5000)
        _write_skill(self.home, "medium-skill", content)
        _register_skill(self.svc.registry, "medium-skill", "Medium skill")

        result = self.svc.skill_view("medium-skill", max_bytes=100)
        self.assertLessEqual(len(result.content.encode("utf-8")), 100)
        self.assertTrue(result.truncated)


class Test09_SkillLoadTelemetry(unittest.TestCase):
    """9. skill load → telemetry records revision + run_id."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-rt-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.svc = _make_service(self.tmpdir, self.store)
        self.home = self.tmpdir / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def test_loading_records_telemetry(self):
        _write_skill(self.home, "deploy-skill", "# Deploy\nProcedure.")
        skill_id = _register_skill(
            self.svc.registry, "deploy-skill", "Deploy procedure", revision=3,
        )

        result = self.svc.skill_view("deploy-skill", run_id="run-42")
        self.assertEqual(result.revision, 3)

        # Check telemetry was recorded
        stats = self.svc.telemetry.stats(skill_id)
        self.assertEqual(stats.get("loaded", 0), 1)


class Test10_ExecutionSuccessTelemetry(unittest.TestCase):
    """10. skill execution succeeds → successful telemetry recorded."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-rt-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.svc = _make_service(self.tmpdir, self.store)
        self.home = self.tmpdir / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def test_successful_execution_records_telemetry(self):
        _write_skill(self.home, "restart-skill", "# Restart\nRestart the gateway.")
        skill_id = _register_skill(
            self.svc.registry, "restart-skill", "Restart gateway procedure",
        )

        # Simulate runtime: selected → executed → successful
        self.svc.record_skill_selection(skill_id, run_id="run-50")
        self.svc.record_skill_execution(skill_id, run_id="run-50", success=True)

        stats = self.svc.telemetry.stats(skill_id)
        self.assertEqual(stats.get("selected"), 1)
        self.assertEqual(stats.get("executed"), 1)
        self.assertEqual(stats.get("successful"), 1)
        self.assertIsNone(stats.get("failed"))

    def test_failed_execution_records_telemetry(self):
        _write_skill(self.home, "flaky-skill", "# Flaky\nMight fail.")
        skill_id = _register_skill(
            self.svc.registry, "flaky-skill", "Unreliable procedure",
        )

        self.svc.record_skill_execution(skill_id, run_id="run-51", success=False)

        stats = self.svc.telemetry.stats(skill_id)
        self.assertEqual(stats.get("executed"), 1)
        self.assertEqual(stats.get("failed"), 1)


class Test11_SkillContradictsEvidence(unittest.TestCase):
    """11. skill contradicts runtime evidence → agent can deviate.

    This is a design test: the preamble instructs the agent to prefer
    current evidence over skill procedures. We verify the preamble text.
    """

    def test_preamble_instructs_preference_for_evidence(self):
        self.assertIn("prefer current evidence", SKILL_PREAMBLE)
        self.assertIn("conflicts with current evidence", SKILL_PREAMBLE)

    def test_preamble_instructs_propose_improvement(self):
        self.assertIn("improve_proposal", SKILL_PREAMBLE)


class Test12_AgentProposesImprovement(unittest.TestCase):
    """12. agent finds better procedure → skill_improve proposal created,
        original skill remains unchanged."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-rt-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.svc = _make_service(self.tmpdir, self.store)
        self.home = self.tmpdir / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def test_improve_proposal_leaves_original_unchanged(self):
        original_content = "# Recovery\nStep 1: Old procedure."
        _write_skill(self.home, "telegram-recovery", original_content)
        skill_id = _register_skill(
            self.svc.registry, "telegram-recovery",
            "Telegram recovery procedure", revision=3,
        )

        # Agent proposes an improvement
        result = self.svc.skill_manage(
            "improve_proposal",
            skill_id="telegram-recovery",
            content="# Recovery\nStep 1: New improved procedure.\nStep 2: Clear webhook.",
            reason="The recovery sequence missed clearing the stale webhook.",
            base_revision=3,
            confidence=0.85,
            source_run_id="run-77",
        )

        self.assertEqual(result.action, "improve_proposal")
        self.assertIsNotNone(result.proposal_id)

        # Original SKILL.md unchanged
        current = (self.home / "skills" / "telegram-recovery" / SKILL_FILENAME).read_text()
        self.assertEqual(current, original_content)

        # Skill revision unchanged
        record = self.svc.registry.get_skill_by_name("telegram-recovery")
        self.assertEqual(record.revision, 3)

    def test_improve_stale_revision_rejected(self):
        _write_skill(self.home, "test-skill", "# Test\nContent.")
        _register_skill(
            self.svc.registry, "test-skill", "Test", revision=5,
        )

        with self.assertRaises(SkillManageError) as ctx:
            self.svc.skill_manage(
                "improve_proposal",
                skill_id="test-skill",
                content="new content",
                reason="fix",
                base_revision=3,  # Stale!
            )
        self.assertIn("stale", str(ctx.exception).lower())


class Test13_UserOwnedSkillReadOnly(unittest.TestCase):
    """13. user-owned skill → agent may read it, cannot autonomously modify it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-rt-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.svc = _make_service(self.tmpdir, self.store)
        self.home = self.tmpdir / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def test_user_skill_readable(self):
        content = "# My Skill\nUser-created procedure."
        _write_skill(self.home, "my-workflow",
                     content, meta={"owner": "user"})
        _register_skill(
            self.svc.registry, "my-workflow", "User workflow",
            owner=SkillOwner.USER,
        )

        # Agent can read it
        result = self.svc.skill_view("my-workflow")
        self.assertEqual(result.content, content)
        self.assertEqual(result.owner, SkillOwner.USER)

    def test_user_skill_cannot_be_archived_by_agent(self):
        _write_skill(self.home, "my-workflow", "# My Skill\nContent.")
        _register_skill(
            self.svc.registry, "my-workflow", "User workflow",
            owner=SkillOwner.USER,
        )

        with self.assertRaises(SkillManageError) as ctx:
            self.svc.skill_manage("archive_proposal", skill_id="my-workflow")
        self.assertIn("user-owned", str(ctx.exception).lower())


class Test14_BundledSkillImmutable(unittest.TestCase):
    """14. bundled skill → readable, immutable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-rt-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.svc = _make_service(self.tmpdir, self.store)
        self.home = self.tmpdir / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def test_bundled_skill_readable(self):
        content = "# Bundled\nShipped with GravityClaw."
        _write_skill(self.home, "bundled-diag",
                     content, meta={"owner": "bundled"})
        _register_skill(
            self.svc.registry, "bundled-diag", "Built-in diagnostics",
            owner=SkillOwner.BUNDLED,
        )

        result = self.svc.skill_view("bundled-diag")
        self.assertEqual(result.content, content)
        self.assertEqual(result.owner, SkillOwner.BUNDLED)

    def test_bundled_skill_cannot_be_archived(self):
        _write_skill(self.home, "bundled-diag", "# Bundled\nContent.")
        _register_skill(
            self.svc.registry, "bundled-diag", "Built-in",
            owner=SkillOwner.BUNDLED,
        )

        with self.assertRaises(SkillManageError) as ctx:
            self.svc.skill_manage("archive_proposal", skill_id="bundled-diag")
        self.assertIn("bundled", str(ctx.exception).lower())


class Test15_FutureEquivalentTaskUsesLearnedSkill(unittest.TestCase):
    """15. future equivalent task → approved learned skill is discovered
        and successful procedure is attempted first.

    This is the decisive test: the system should surface relevant skills
    for equivalent future tasks, completing the self-improvement loop.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-rt-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.svc = _make_service(self.tmpdir, self.store)
        self.home = self.tmpdir / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def test_learned_skill_discovered_for_equivalent_task(self):
        """Simulate the full loop: learn → store → future task → discover → use."""
        # Step 1: A skill was learned and approved (from a previous run)
        content = """# Systemd Gateway Recovery

## When to use
Gateway service is down or unresponsive.

## Procedure
1. Check status: `systemctl status gravityclaw-gateway`
2. Check logs: `journalctl -u gravityclaw-gateway --since "5 min ago"`
3. If OOM: increase memory limit in unit file
4. Restart: `systemctl restart gravityclaw-gateway`
5. Verify: curl health endpoint
"""
        _write_skill(self.home, "systemd-gateway-recovery", content)
        skill_id = _register_skill(
            self.svc.registry, "systemd-gateway-recovery",
            "Recover and diagnose a systemd-managed GravityClaw gateway",
            trust=SkillTrust.APPROVED, revision=2,
        )
        self.store.sync_skills_fts()

        # Step 2: A future equivalent task arrives
        future_task = "the gateway is down and not responding to health checks"
        prompt, candidates = self.svc.build_skill_prompt(future_task)

        # Step 3: The skill should be discovered
        candidate_names = [c.name for c in candidates]
        self.assertIn("systemd-gateway-recovery", candidate_names)

        # Step 4: The prompt should contain the skill metadata
        self.assertIn("systemd-gateway-recovery", prompt)
        self.assertIn("Recover and diagnose", prompt)

        # Step 5: Agent selects and loads it
        candidate = next(c for c in candidates if c.name == "systemd-gateway-recovery")
        self.svc.record_skill_selection(skill_id, run_id="run-200")
        view_result = self.svc.skill_view("systemd-gateway-recovery", run_id="run-200")

        # Step 6: Full procedure is available
        self.assertIn("systemctl status", view_result.content)
        self.assertIn("journalctl", view_result.content)
        self.assertEqual(view_result.revision, 2)
        self.assertEqual(view_result.trust, SkillTrust.APPROVED)

        # Step 7: After successful execution
        self.svc.record_skill_execution(skill_id, run_id="run-200", success=True)

        # Step 8: Verify telemetry chain
        stats = self.svc.telemetry.stats(skill_id)
        self.assertGreaterEqual(stats.get("matched", 0), 1)
        self.assertGreaterEqual(stats.get("presented", 0), 1)
        self.assertEqual(stats.get("selected"), 1)
        self.assertGreaterEqual(stats.get("loaded", 0), 1)
        self.assertEqual(stats.get("executed"), 1)
        self.assertEqual(stats.get("successful"), 1)


class TestRunSkillContext(unittest.TestCase):
    """RunSkillContext persistence and serialization."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-rt-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.svc = _make_service(self.tmpdir, self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_and_load_context(self):
        ctx = RunSkillContext(
            run_id="run-100",
            candidates=["skill-a", "skill-b"],
            presented=["skill-a"],
            selected=["skill-a"],
            loaded=[LoadedSkill(
                skill_id="skill-a", name="deploy-skill",
                revision=3, trust="approved",
            )],
        )

        self.svc.save_run_context(ctx)
        loaded = self.svc.get_run_context("run-100")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.run_id, "run-100")
        self.assertEqual(loaded.candidates, ["skill-a", "skill-b"])
        self.assertEqual(loaded.presented, ["skill-a"])
        self.assertEqual(loaded.selected, ["skill-a"])
        self.assertEqual(len(loaded.loaded), 1)
        self.assertEqual(loaded.loaded[0].name, "deploy-skill")
        self.assertEqual(loaded.loaded[0].revision, 3)

    def test_missing_context_returns_none(self):
        result = self.svc.get_run_context("nonexistent-run")
        self.assertIsNone(result)

    def test_serialization_roundtrip(self):
        ctx = RunSkillContext(
            run_id="run-200",
            candidates=["a", "b", "c"],
            presented=["a", "b"],
            selected=["a"],
            loaded=[
                LoadedSkill(skill_id="a", name="alpha", revision=1, trust="approved"),
                LoadedSkill(skill_id="b", name="beta", revision=2, trust="unreviewed"),
            ],
        )
        data = ctx.to_dict()
        restored = RunSkillContext.from_dict(data)
        self.assertEqual(restored.run_id, ctx.run_id)
        self.assertEqual(len(restored.loaded), 2)


class TestSkillManageCreateProposal(unittest.TestCase):
    """skill_manage create_proposal validation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-rt-")
        self.tmpdir = Path(self.tmp.name)
        self.store = _make_store(self.tmpdir)
        self.svc = _make_service(self.tmpdir, self.store)
        self.home = self.tmpdir / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_create_proposal(self):
        result = self.svc.skill_manage(
            "create_proposal",
            name="new-procedure",
            description="A new reusable procedure",
            content="# New Procedure\nStep 1...",
            reason="Learned from run",
            confidence=0.9,
        )
        self.assertEqual(result.action, "create_proposal")
        self.assertIsNotNone(result.proposal_id)

    def test_invalid_name_rejected(self):
        with self.assertRaises(SkillManageError):
            self.svc.skill_manage(
                "create_proposal",
                name="Invalid Name!",
                description="Bad",
                content="# Bad",
                reason="test",
            )

    def test_duplicate_name_rejected(self):
        _write_skill(self.home, "existing-skill", "# Existing")
        _register_skill(self.svc.registry, "existing-skill", "Exists")

        with self.assertRaises(SkillManageError) as ctx:
            self.svc.skill_manage(
                "create_proposal",
                name="existing-skill",
                description="Duplicate",
                content="# Dup",
                reason="test",
            )
        self.assertIn("already exists", str(ctx.exception).lower())

    def test_invalid_action_rejected(self):
        with self.assertRaises(SkillManageError) as ctx:
            self.svc.skill_manage("approve")
        self.assertIn("invalid action", str(ctx.exception).lower())


class TestPromptIntegrationPreamble(unittest.TestCase):
    """Prompt integration formatting and structure."""

    def test_preamble_content(self):
        self.assertIn("Skills are reusable procedures", SKILL_PREAMBLE)
        self.assertIn("procedural guidance", SKILL_PREAMBLE)
        self.assertIn("prefer current evidence", SKILL_PREAMBLE)

    def test_empty_prompt_when_no_skills(self):
        tmp = tempfile.TemporaryDirectory(prefix="gc-rt-")
        tmpdir = Path(tmp.name)
        store = _make_store(tmpdir)
        svc = _make_service(tmpdir, store)

        prompt, candidates = svc.build_skill_prompt("do something")
        self.assertEqual(prompt, "")
        self.assertEqual(candidates, [])
        tmp.cleanup()


class TestTelemetryExtended(unittest.TestCase):
    """Extended telemetry event types (Phase 2.5)."""

    def test_all_runtime_events_valid(self):
        """All Phase 2.5 telemetry events should be in VALID_EVENTS."""
        runtime_events = {"matched", "presented", "selected", "loaded",
                         "executed", "successful", "failed", "corrected"}
        for event in runtime_events:
            self.assertIn(event, VALID_EVENTS)

    def test_telemetry_precision_metrics(self):
        """Calculate selection precision and success rate."""
        tmp = tempfile.TemporaryDirectory(prefix="gc-rt-")
        tmpdir = Path(tmp.name)
        store = _make_store(tmpdir)
        svc = _make_service(tmpdir, store)
        home = tmpdir / "home"

        _write_skill(home, "metric-skill", "# Metric")
        skill_id = _register_skill(svc.registry, "metric-skill", "Metrics")

        # Simulate usage funnel
        for _ in range(10):
            svc.telemetry.record(skill_id, "matched")
        for _ in range(7):
            svc.telemetry.record(skill_id, "presented")
        for _ in range(4):
            svc.telemetry.record(skill_id, "selected")
        for _ in range(4):
            svc.telemetry.record(skill_id, "loaded")
        for _ in range(3):
            svc.telemetry.record(skill_id, "executed")
        for _ in range(2):
            svc.telemetry.record(skill_id, "successful")
        svc.telemetry.record(skill_id, "failed")

        stats = svc.telemetry.stats(skill_id)
        self.assertEqual(stats["matched"], 10)
        self.assertEqual(stats["presented"], 7)
        self.assertEqual(stats["selected"], 4)
        self.assertEqual(stats["loaded"], 4)
        self.assertEqual(stats["executed"], 3)
        self.assertEqual(stats["successful"], 2)
        self.assertEqual(stats["failed"], 1)

        # Success rate
        rate = svc.telemetry.success_rate(skill_id)
        self.assertAlmostEqual(rate, 2 / 3)

        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
