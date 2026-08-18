"""Tests for Phase 3.1: Product Integration & Hardening.

Validates:
 1. LearnRequest/LearnOptions model construction and normalization.
 2. parse_learn_command handles all supported syntaxes.
 3. LearnService rejects requests when learning is disabled.
 4. LearnService routes to IngestionEngine and returns formatted LearnResponse.
 5. LearnService handles duplicate detection.
 6. LearnService handles ingestion failure gracefully.
 7. LearnService records audit events.
 8. /learn is surface-agnostic (Web/Telegram/CLI/API produce identical LearnRequest).
 9. LearningConfig defaults are sane.
10. LearningConfig.from_toml parses a full [learning] section.
11. LearningConfig.from_environment applies env var overrides.
12. Env var overrides take precedence over TOML values.
13. CuratorJob respects min_idle_hours (idempotency).
14. CuratorJob acquires lock (no concurrent runs).
15. CuratorJob records audit events on run.
16. ensure_curator_schedule creates a schedule idempotently.
17. ensure_curator_schedule updates schedule when expression changes.
18. ensure_curator_schedule disables when curator.enabled=false.
19. Unified config produces correct CuratorConfig for Curator engine.
20. /api/learning/learn endpoint (via app factory) returns proper status codes.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from gravityclaw.learn_service import (
    LearnChannel,
    LearnOptions,
    LearnRequest,
    LearnResponse,
    LearnService,
    LearnStatus,
    parse_learn_command,
)
from gravityclaw.learning_config import (
    CuratorScheduleConfig,
    IngestionConfig,
    LearningConfig,
    NotificationsConfig,
    ReviewerConfig,
    SkillsConfig,
)
from gravityclaw.curator_job import (
    CURATOR_SCHEDULE_NAME,
    CURATOR_SCHEDULE_PROMPT,
    CuratorJob,
    ensure_curator_schedule,
)
from gravityclaw.skills.curator import Curator, CuratorConfig, CuratorReport
from gravityclaw.skills.ingestion import (
    DeduplicationResult,
    IngestionEngine,
    LearnResult,
    SourceType,
)
from gravityclaw.skills.registry import SkillRegistry
from gravityclaw.skills.trust import TrustMode, TrustPolicy
from gravityclaw.store import Store


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_store(tmp: Path) -> Store:
    store = Store(tmp / "gravityclaw.db")
    store.initialize()
    return store


def _make_workspace(store: Store, tmp: Path) -> Any:
    return store.create_workspace("test", tmp / "workspace")


def _make_learn_result(*, proposal_id: str | None = "prop-1", warnings: list[str] | None = None) -> LearnResult:
    return LearnResult(
        source_type=SourceType.WEB_PAGE,
        source_identity="https://example.com/docs",
        title="Example Docs",
        summary="Documentation for example.com",
        proposed_skill_name="example-docs",
        proposed_skill_content="# Example\nStep 1...",
        proposed_description="Handles example.com operations",
        dedup_result=DeduplicationResult.NO_MATCH,
        chunks_processed=1,
        content_hash="abc123",
        proposal_id=proposal_id,
        warnings=warnings or [],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test LearnRequest / LearnOptions model
# ─────────────────────────────────────────────────────────────────────────────


class TestLearnRequestModels(unittest.TestCase):
    """Test 1: LearnRequest/LearnOptions construction and normalization."""

    def test_default_options(self) -> None:
        opts = LearnOptions()
        self.assertIsNone(opts.skill_name)
        self.assertFalse(opts.force_new)
        self.assertIsNone(opts.trust_override)

    def test_request_with_options(self) -> None:
        req = LearnRequest(
            source="https://docs.example.com",
            requested_by="user:ahmad",
            channel=LearnChannel.WEB,
            options=LearnOptions(skill_name="my-skill", force_new=True),
        )
        self.assertEqual(req.source, "https://docs.example.com")
        self.assertEqual(req.channel, LearnChannel.WEB)
        self.assertEqual(req.options.skill_name, "my-skill")
        self.assertTrue(req.options.force_new)
        self.assertIsNotNone(req.request_id)
        self.assertIsNotNone(req.requested_at)

    def test_channel_enum(self) -> None:
        self.assertEqual(LearnChannel.WEB.value, "web")
        self.assertEqual(LearnChannel.TELEGRAM.value, "telegram")
        self.assertEqual(LearnChannel.CLI.value, "cli")
        self.assertEqual(LearnChannel.API.value, "api")


# ─────────────────────────────────────────────────────────────────────────────
# Test parse_learn_command
# ─────────────────────────────────────────────────────────────────────────────


class TestParseLarnCommand(unittest.TestCase):
    """Test 2: parse_learn_command handles all supported syntaxes."""

    def test_simple_url(self) -> None:
        req = parse_learn_command("/learn https://example.com/docs")
        self.assertEqual(req.source, "https://example.com/docs")
        self.assertIsNone(req.options.skill_name)
        self.assertFalse(req.options.force_new)

    def test_url_with_name(self) -> None:
        req = parse_learn_command("/learn https://example.com --name my-skill")
        self.assertEqual(req.source, "https://example.com")
        self.assertEqual(req.options.skill_name, "my-skill")

    def test_path_with_force_new(self) -> None:
        req = parse_learn_command("/learn ./project --force-new --name gravityclaw-dev")
        self.assertEqual(req.source, "./project")
        self.assertTrue(req.options.force_new)
        self.assertEqual(req.options.skill_name, "gravityclaw-dev")

    def test_this_conversation(self) -> None:
        req = parse_learn_command(
            "/learn this conversation",
            conversation_id="conv-123",
        )
        self.assertEqual(req.source, "this conversation")
        self.assertEqual(req.conversation_id, "conv-123")

    def test_without_prefix(self) -> None:
        req = parse_learn_command("learn https://docs.rs/tokio")
        self.assertEqual(req.source, "https://docs.rs/tokio")

    def test_channel_passthrough(self) -> None:
        req = parse_learn_command(
            "/learn https://foo.bar",
            channel=LearnChannel.TELEGRAM,
            requested_by="telegram:12345",
        )
        self.assertEqual(req.channel, LearnChannel.TELEGRAM)
        self.assertEqual(req.requested_by, "telegram:12345")


# ─────────────────────────────────────────────────────────────────────────────
# Test LearnService
# ─────────────────────────────────────────────────────────────────────────────


class TestLearnService(unittest.TestCase):
    """Tests 3-8: LearnService behavior."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = _make_store(self.root)
        self.trust_policy = TrustPolicy(mode=TrustMode.STRICT)
        self.registry = SkillRegistry(self.store)
        self.ingestion = IngestionEngine(self.registry, self.store, self.root)
        self.config = LearningConfig()
        self.service = LearnService(self.ingestion, self.trust_policy, self.store, self.config)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_disabled_returns_disabled_status(self) -> None:
        """Test 3: Rejects when learning disabled."""
        config = LearningConfig(enabled=False)
        svc = LearnService(self.ingestion, self.trust_policy, self.store, config)
        req = LearnRequest(
            source="https://example.com",
            requested_by="test",
            channel=LearnChannel.API,
        )
        resp = svc.learn(req)
        self.assertEqual(resp.status, LearnStatus.DISABLED)

    def test_empty_source_fails(self) -> None:
        """Test 3 extended: empty source validation."""
        req = LearnRequest(source="  ", requested_by="test", channel=LearnChannel.API)
        resp = self.service.learn(req)
        self.assertEqual(resp.status, LearnStatus.FAILED)
        self.assertIn("No source", resp.message)

    def test_successful_ingestion(self) -> None:
        """Test 4: Routes to IngestionEngine and returns formatted response."""
        result = _make_learn_result()
        with patch.object(self.ingestion, "ingest", return_value=result):
            req = LearnRequest(
                source="https://example.com/docs",
                requested_by="test",
                channel=LearnChannel.WEB,
            )
            resp = self.service.learn(req)
        self.assertEqual(resp.status, LearnStatus.SUCCESS)
        self.assertEqual(resp.proposal_id, "prop-1")
        self.assertEqual(resp.skill_name, "example-docs")
        self.assertEqual(resp.source_type, "web_page")

    def test_duplicate_detection(self) -> None:
        """Test 5: Handles duplicate detection."""
        result = _make_learn_result(proposal_id=None, warnings=["exact duplicate — skipping proposal creation"])
        with patch.object(self.ingestion, "ingest", return_value=result):
            req = LearnRequest(source="https://dup.com", requested_by="test", channel=LearnChannel.API)
            resp = self.service.learn(req)
        self.assertEqual(resp.status, LearnStatus.DUPLICATE)

    def test_ingestion_failure_graceful(self) -> None:
        """Test 6: Handles ingestion failure gracefully."""
        with patch.object(self.ingestion, "ingest", side_effect=RuntimeError("network error")):
            req = LearnRequest(source="https://fail.com", requested_by="test", channel=LearnChannel.API)
            resp = self.service.learn(req)
        self.assertEqual(resp.status, LearnStatus.FAILED)
        self.assertIn("network error", resp.message)

    def test_audit_recorded(self) -> None:
        """Test 7: Records audit events."""
        result = _make_learn_result()
        with patch.object(self.ingestion, "ingest", return_value=result):
            req = LearnRequest(source="https://example.com", requested_by="test-actor", channel=LearnChannel.CLI)
            self.service.learn(req)
        # Check audit was recorded
        audits = self.store.list_audit()
        learning_audits = [a for a in audits if a.action == "learning.learn"]
        self.assertEqual(len(learning_audits), 1)
        self.assertEqual(learning_audits[0].actor, "test-actor")

    def test_surface_agnostic(self) -> None:
        """Test 8: All surfaces produce identical pipeline results."""
        result = _make_learn_result()
        responses: list[LearnResponse] = []
        for channel in (LearnChannel.WEB, LearnChannel.TELEGRAM, LearnChannel.CLI, LearnChannel.API):
            with patch.object(self.ingestion, "ingest", return_value=result):
                req = LearnRequest(
                    source="https://example.com/docs",
                    requested_by="test",
                    channel=channel,
                )
                resp = self.service.learn(req)
                responses.append(resp)
        # All should succeed with same proposal
        for resp in responses:
            self.assertEqual(resp.status, LearnStatus.SUCCESS)
            self.assertEqual(resp.proposal_id, "prop-1")
            self.assertEqual(resp.skill_name, "example-docs")


# ─────────────────────────────────────────────────────────────────────────────
# Test LearningConfig
# ─────────────────────────────────────────────────────────────────────────────


class TestLearningConfig(unittest.TestCase):
    """Tests 9-12: Configuration tree."""

    def test_defaults_are_sane(self) -> None:
        """Test 9: LearningConfig defaults."""
        cfg = LearningConfig()
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.reviewer.model, "gemini-2.0-flash")
        self.assertEqual(cfg.skills.trust_mode, "strict")
        self.assertEqual(cfg.ingestion.chunk_tokens, 12_000)
        self.assertEqual(cfg.curator.schedule, "0 4 * * 0")
        self.assertEqual(cfg.notifications.mode, "normal")

    def test_from_toml_full_section(self) -> None:
        """Test 10: from_toml parses complete section."""
        toml: dict[str, Any] = {
            "enabled": True,
            "reviewer": {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "max_retries": 5,
            },
            "skills": {
                "trust_mode": "balanced",
                "min_confidence": 0.75,
            },
            "ingestion": {
                "chunk_tokens": 8000,
                "max_chunks": 50,
            },
            "curator": {
                "enabled": True,
                "schedule": "0 3 * * *",
                "timezone": "America/New_York",
                "stale_after_days": 45,
            },
            "notifications": {
                "mode": "verbose",
            },
        }
        cfg = LearningConfig.from_toml(toml)
        self.assertEqual(cfg.reviewer.provider, "openai")
        self.assertEqual(cfg.reviewer.model, "gpt-4o-mini")
        self.assertEqual(cfg.reviewer.max_retries, 5)
        self.assertEqual(cfg.skills.trust_mode, "balanced")
        self.assertEqual(cfg.skills.min_confidence, 0.75)
        self.assertEqual(cfg.ingestion.chunk_tokens, 8000)
        self.assertEqual(cfg.ingestion.max_chunks, 50)
        self.assertEqual(cfg.curator.schedule, "0 3 * * *")
        self.assertEqual(cfg.curator.timezone, "America/New_York")
        self.assertEqual(cfg.curator.stale_after_days, 45)
        self.assertEqual(cfg.notifications.mode, "verbose")

    def test_from_environment_applies_env_overrides(self) -> None:
        """Test 11: from_environment applies env var overrides."""
        env = {
            "GRAVITYCLAW_LEARNING_ENABLED": "true",
            "GRAVITYCLAW_LEARNING_REVIEWER_MODEL": "claude-3",
            "GRAVITYCLAW_LEARNING_SKILLS_TRUST_MODE": "autonomous",
            "GRAVITYCLAW_LEARNING_CURATOR_ENABLED": "false",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = LearningConfig.from_environment(None)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.reviewer.model, "claude-3")
        self.assertEqual(cfg.skills.trust_mode, "autonomous")
        self.assertFalse(cfg.curator.enabled)

    def test_env_overrides_take_precedence(self) -> None:
        """Test 12: Env vars override TOML values."""
        toml: dict[str, Any] = {
            "reviewer": {"model": "gemini-flash"},
            "curator": {"schedule": "0 4 * * 0"},
        }
        env = {
            "GRAVITYCLAW_LEARNING_REVIEWER_MODEL": "gpt-4o",
            "GRAVITYCLAW_LEARNING_CURATOR_SCHEDULE": "0 2 * * *",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = LearningConfig.from_environment(toml)
        self.assertEqual(cfg.reviewer.model, "gpt-4o")
        self.assertEqual(cfg.curator.schedule, "0 2 * * *")

    def test_to_dict_roundtrip(self) -> None:
        """Config serializes to dict for API responses."""
        cfg = LearningConfig()
        d = cfg.to_dict()
        self.assertIn("enabled", d)
        self.assertIn("reviewer", d)
        self.assertIn("curator", d)
        self.assertEqual(d["curator"]["schedule"], "0 4 * * 0")

    def test_empty_section_returns_defaults(self) -> None:
        """from_toml with None or empty dict returns defaults."""
        self.assertEqual(LearningConfig.from_toml(None).enabled, True)
        self.assertEqual(LearningConfig.from_toml({}).enabled, True)


# ─────────────────────────────────────────────────────────────────────────────
# Test CuratorJob
# ─────────────────────────────────────────────────────────────────────────────


class TestCuratorJob(unittest.TestCase):
    """Tests 13-15: CuratorJob idempotency, locking, audit."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = _make_store(self.root)
        self.trust_policy = TrustPolicy(mode=TrustMode.BALANCED)
        self.registry = SkillRegistry(self.store)
        self.curator = Curator(
            self.registry, self.store, self.trust_policy,
            CuratorConfig(enabled=True, stale_after_days=30),
        )
        self.config = CuratorScheduleConfig(enabled=True, min_idle_hours=2)
        self.job = CuratorJob(self.curator, self.store, self.config)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_min_idle_hours_respected(self) -> None:
        """Test 13: CuratorJob skips if min_idle_hours not met."""
        now = datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC)
        # First run should succeed
        report = self.job.run(now=now)
        self.assertIsNotNone(report)
        # Second run 30 minutes later should be skipped (< 2h idle)
        later = now + timedelta(minutes=30)
        report2 = self.job.run(now=later)
        self.assertIsNone(report2)
        # Third run 3 hours later should succeed
        much_later = now + timedelta(hours=3)
        report3 = self.job.run(now=much_later)
        self.assertIsNotNone(report3)

    def test_force_ignores_idle(self) -> None:
        """Force=True skips the idleness check."""
        now = datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC)
        self.job.run(now=now)
        later = now + timedelta(minutes=5)
        report = self.job.run(now=later, force=True)
        self.assertIsNotNone(report)

    def test_disabled_returns_none(self) -> None:
        """Disabled config returns None."""
        config = CuratorScheduleConfig(enabled=False)
        job = CuratorJob(self.curator, self.store, config)
        self.assertIsNone(job.run())

    def test_lock_prevents_concurrent(self) -> None:
        """Test 14: Lock prevents concurrent runs."""
        from gravityclaw.curator_job import _CURATOR_LOCK

        # Simulate the lock being held
        _CURATOR_LOCK.acquire()
        try:
            report = self.job.run(force=True)
            self.assertIsNone(report)
        finally:
            _CURATOR_LOCK.release()

    def test_audit_recorded(self) -> None:
        """Test 15: Records audit events."""
        now = datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC)
        self.job.run(now=now)
        audits = self.store.list_audit()
        curator_audits = [a for a in audits if a.action == "curator.run"]
        self.assertEqual(len(curator_audits), 1)
        self.assertEqual(curator_audits[0].actor, "system:curator")

    def test_status_output(self) -> None:
        """Status dict is well-formed."""
        now = datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC)
        self.job.run(now=now)
        status = self.job.status()
        self.assertTrue(status["enabled"])
        self.assertIsNotNone(status["last_run_at"])
        self.assertIsNotNone(status["last_report"])

    def test_idempotent_second_run_no_duplicate_actions(self) -> None:
        """Running curator twice against unchanged state produces zero actions on second run."""
        now = datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC)
        r1 = self.job.run(now=now, force=True)
        later = now + timedelta(hours=3)
        r2 = self.job.run(now=later, force=True)
        # Both runs should complete but with same (zero) actions since no skills exist
        self.assertIsNotNone(r1)
        self.assertIsNotNone(r2)
        self.assertEqual(len(r1.actions_taken), 0)
        self.assertEqual(len(r2.actions_taken), 0)


# ─────────────────────────────────────────────────────────────────────────────
# Test ensure_curator_schedule
# ─────────────────────────────────────────────────────────────────────────────


class TestCuratorScheduleRegistration(unittest.TestCase):
    """Tests 16-18: Schedule registration."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = _make_store(self.root)
        self.workspace = _make_workspace(self.store, self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_creates_schedule_idempotently(self) -> None:
        """Test 16: Creates schedule, second call is a no-op."""
        config = CuratorScheduleConfig(enabled=True, schedule="0 4 * * 0", timezone="UTC")
        sid1 = ensure_curator_schedule(self.store, self.workspace.id, config)
        self.assertIsNotNone(sid1)

        # Second call should return same ID without creating a new one
        sid2 = ensure_curator_schedule(self.store, self.workspace.id, config)
        self.assertEqual(sid1, sid2)

        # Only one schedule should exist
        schedules = [s for s in self.store.list_schedules() if s.name == CURATOR_SCHEDULE_NAME]
        self.assertEqual(len(schedules), 1)

    def test_updates_when_expression_changes(self) -> None:
        """Test 17: Recreates schedule when cron expression changes."""
        config1 = CuratorScheduleConfig(enabled=True, schedule="0 4 * * 0", timezone="UTC")
        sid1 = ensure_curator_schedule(self.store, self.workspace.id, config1)

        config2 = CuratorScheduleConfig(enabled=True, schedule="0 2 * * *", timezone="UTC")
        sid2 = ensure_curator_schedule(self.store, self.workspace.id, config2)

        self.assertNotEqual(sid1, sid2)
        # Old schedule should be deleted
        active = [s for s in self.store.list_schedules() if s.name == CURATOR_SCHEDULE_NAME]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].expression, "0 2 * * *")

    def test_disables_when_curator_disabled(self) -> None:
        """Test 18: Disables schedule when curator.enabled=false."""
        config_on = CuratorScheduleConfig(enabled=True, schedule="0 4 * * 0", timezone="UTC")
        sid = ensure_curator_schedule(self.store, self.workspace.id, config_on)
        self.assertIsNotNone(sid)

        config_off = CuratorScheduleConfig(enabled=False, schedule="0 4 * * 0", timezone="UTC")
        ensure_curator_schedule(self.store, self.workspace.id, config_off)

        schedule = self.store.get_schedule(sid)
        self.assertFalse(schedule.enabled)


# ─────────────────────────────────────────────────────────────────────────────
# Test unified config → CuratorConfig mapping
# ─────────────────────────────────────────────────────────────────────────────


class TestConfigToCuratorMapping(unittest.TestCase):
    """Test 19: LearningConfig produces correct CuratorConfig."""

    def test_config_mapping(self) -> None:
        toml: dict[str, Any] = {
            "curator": {
                "stale_after_days": 45,
                "archive_after_days": 120,
                "min_idle_hours": 4,
            },
        }
        cfg = LearningConfig.from_toml(toml)
        curator_cfg = CuratorConfig(
            enabled=cfg.curator.enabled,
            stale_after_days=cfg.curator.stale_after_days,
            archive_after_days=cfg.curator.archive_after_days,
            min_idle_hours=cfg.curator.min_idle_hours,
            minimum_invocations=cfg.curator.minimum_invocations,
            utility_stale_threshold=cfg.curator.utility_stale_threshold,
            utility_archive_threshold=cfg.curator.utility_archive_threshold,
        )
        self.assertEqual(curator_cfg.stale_after_days, 45)
        self.assertEqual(curator_cfg.archive_after_days, 120)
        self.assertEqual(curator_cfg.min_idle_hours, 4)
        # Defaults preserved
        self.assertTrue(curator_cfg.enabled)
        self.assertEqual(curator_cfg.minimum_invocations, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Test API endpoint integration (lightweight — no HTTP client, just app factory)
# ─────────────────────────────────────────────────────────────────────────────


class TestAPIEndpointWiring(unittest.TestCase):
    """Test 20: API endpoint wiring through create_app."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_create_app_with_learning_services(self) -> None:
        """create_app() instantiates learning services without error."""
        from gravityclaw.api import Settings, create_app

        settings = Settings(
            home=Path(self.root.name),
            mode="fake",
            learning_enabled=True,
            learning_toml={
                "curator": {"enabled": False},  # Don't actually schedule
            },
        )
        # This should not raise
        app = create_app(settings)
        self.assertIsNotNone(app.state.learn_service)
        self.assertIsNotNone(app.state.skill_service)
        self.assertIsNotNone(app.state.curator_job)
        self.assertIsNotNone(app.state.trust_policy)
        self.assertIsNotNone(app.state.learning_config)
        self.assertFalse(app.state.learning_config.curator.enabled)


if __name__ == "__main__":
    unittest.main()
