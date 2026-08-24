"""Tests for Learning Mode Phase 1 — gate, store, engine, and end-to-end flow."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from gravityclaw.learning import (
    DEFAULT_GATE_THRESHOLD,
    DEFAULT_SIGNAL_WEIGHTS,
    GateResult,
    LearningEligibilityGate,
    LearningEngine,
    LearningEvent,
    LearningJob,
    LearningResult,
    MemoryOperation,
)
from gravityclaw.identity import IdentityStore
from gravityclaw.memory import MemoryService
from gravityclaw.store import RunRecord, Store


def _make_run(
    run_id: str = "run-001",
    conversation_id: str = "conv-001",
    prompt: str = "do something",
    **kwargs,
) -> RunRecord:
    """Create a minimal RunRecord for testing."""
    defaults = {
        "id": run_id,
        "conversation_id": conversation_id,
        "status": "completed",
        "backend": "agy",
        "backend_conversation_id": None,
        "worker_id": None,
        "request": {"prompt": prompt},
        "error": None,
        "created_at": "2026-08-17T10:00:00+00:00",
        "started_at": "2026-08-17T10:00:01+00:00",
        "finished_at": "2026-08-17T10:00:05+00:00",
        "version": 1,
    }
    defaults.update(kwargs)
    return RunRecord(**defaults)


def _make_events(tool_calls: int = 0, tool_names: list[str] | None = None,
                 include_failure: bool = False, include_completion: bool = True,
                 total_events: int | None = None) -> list[dict]:
    """Build a synthetic event list for gate testing."""
    events = []
    names = tool_names or ["read_file"] * tool_calls
    for i, name in enumerate(names):
        events.append({
            "event_type": "tool.executed",
            "payload": {"tool_name": name, "name": name},
        })
    if include_failure:
        events.insert(0, {"event_type": "tool.failed", "payload": {"error": "oops"}})
    if include_completion:
        events.append({
            "event_type": "agent.completed",
            "payload": {"response": "Done."},
        })
    # Pad with diagnostics if total_events is specified
    if total_events and len(events) < total_events:
        for _ in range(total_events - len(events)):
            events.insert(-1, {"event_type": "backend.diagnostic", "payload": {"text": "..."}})
    return events


class EligibilityGateTests(unittest.TestCase):
    """Tests for LearningEligibilityGate deterministic scoring."""

    def setUp(self) -> None:
        self.gate = LearningEligibilityGate()

    def test_trivial_turn_not_eligible(self) -> None:
        """Short turns with no tool calls, no signals → not eligible."""
        run = _make_run(prompt="thanks")
        events = _make_events(tool_calls=0)
        result = self.gate.evaluate(run, events)
        self.assertFalse(result.eligible)
        self.assertEqual(result.score, 0.0)
        self.assertEqual(result.fired_signals, ())

    def test_tool_calls_fire_signal(self) -> None:
        """Any tool call should fire the tool_calls signal."""
        run = _make_run(prompt="read that file")
        events = _make_events(tool_calls=1)
        result = self.gate.evaluate(run, events)
        self.assertIn("tool_calls", result.fired_signals)
        self.assertGreater(result.score, 0)

    def test_many_tool_calls_fires_long_turn(self) -> None:
        """5+ tool calls fires both tool_calls and long_or_complex_turn."""
        run = _make_run(prompt="refactor everything")
        events = _make_events(tool_calls=6)
        result = self.gate.evaluate(run, events)
        self.assertIn("tool_calls", result.fired_signals)
        self.assertIn("long_or_complex_turn", result.fired_signals)
        # Both signals together should cross the default threshold
        self.assertTrue(result.eligible)

    def test_many_events_fires_long_turn(self) -> None:
        """15+ events fires long_or_complex_turn even with few tool calls."""
        run = _make_run(prompt="complex task")
        events = _make_events(tool_calls=2, total_events=16)
        result = self.gate.evaluate(run, events)
        self.assertIn("long_or_complex_turn", result.fired_signals)

    def test_explicit_learn_request_always_eligible(self) -> None:
        """'remember this' in prompt should always fire (weight=10)."""
        run = _make_run(prompt="remember this: production runs on port 8787")
        events = _make_events(tool_calls=0)
        result = self.gate.evaluate(run, events)
        self.assertTrue(result.eligible)
        self.assertIn("explicit_learn_request", result.fired_signals)
        self.assertGreaterEqual(result.score, 10.0)

    def test_learn_command_fires(self) -> None:
        """/learn in prompt fires explicit_learn_request."""
        run = _make_run(prompt="/learn this deployment process")
        events = _make_events(tool_calls=0)
        result = self.gate.evaluate(run, events)
        self.assertIn("explicit_learn_request", result.fired_signals)
        self.assertTrue(result.eligible)

    def test_explicit_preference_fires(self) -> None:
        """Preference language fires explicit_preference signal."""
        run = _make_run(prompt="I prefer tabs over spaces")
        events = _make_events(tool_calls=0)
        result = self.gate.evaluate(run, events)
        self.assertIn("explicit_preference", result.fired_signals)

    def test_user_correction_fires(self) -> None:
        """Correction language fires user_correction signal."""
        run = _make_run(prompt="no, that's not right. Use port 9090 instead.")
        events = _make_events(tool_calls=0)
        result = self.gate.evaluate(run, events)
        self.assertIn("user_correction", result.fired_signals)

    def test_failure_then_success_fires(self) -> None:
        """A tool failure followed by completion fires task_success_after_failure."""
        run = _make_run(prompt="try again")
        events = _make_events(tool_calls=2, include_failure=True)
        result = self.gate.evaluate(run, events)
        self.assertIn("task_success_after_failure", result.fired_signals)

    def test_config_change_fires(self) -> None:
        """File writes to config-like paths fire configuration_change."""
        run = _make_run(prompt="update the config")
        events = [
            {"event_type": "tool.executed", "payload": {"tool_name": "fs_write", "path": "/etc/gravityclaw.toml"}},
            {"event_type": "agent.completed", "payload": {"response": "Done."}},
        ]
        result = self.gate.evaluate(run, events)
        self.assertIn("configuration_change", result.fired_signals)

    def test_new_tool_discovered_fires(self) -> None:
        """Any tool usage fires new_tool_discovered."""
        run = _make_run(prompt="use the new tool")
        events = _make_events(tool_calls=1, tool_names=["custom_tool"])
        result = self.gate.evaluate(run, events)
        self.assertIn("new_tool_discovered", result.fired_signals)

    def test_goal_continuation_dampened(self) -> None:
        """Goal continuation runs have their score halved."""
        run = _make_run(prompt="continue working",
                        request={"prompt": "continue working", "goal_continuation": True})
        events = _make_events(tool_calls=3)
        result = self.gate.evaluate(run, events)
        # Same events without goal_continuation should score higher
        run_normal = _make_run(prompt="continue working")
        result_normal = self.gate.evaluate(run_normal, events)
        self.assertLess(result.score, result_normal.score)

    def test_custom_threshold(self) -> None:
        """Custom threshold changes eligibility boundary."""
        high_gate = LearningEligibilityGate(threshold=50.0)
        run = _make_run(prompt="do something with tools")
        events = _make_events(tool_calls=3)
        result = high_gate.evaluate(run, events)
        self.assertFalse(result.eligible)

    def test_custom_weights(self) -> None:
        """Custom weights change scoring."""
        custom_gate = LearningEligibilityGate(
            threshold=1.0,
            signal_weights={"tool_calls": 100.0},
        )
        run = _make_run(prompt="use a tool")
        events = _make_events(tool_calls=1)
        result = custom_gate.evaluate(run, events)
        self.assertTrue(result.eligible)
        self.assertGreaterEqual(result.score, 100.0)


class LearningStoreTests(unittest.TestCase):
    """Tests for learning-related Store CRUD methods."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="gravityclaw-learning-")
        self.store = Store(Path(self.tmpdir.name) / "test.db")
        self.store.initialize()
        self.workspace = self.store.create_workspace("test", Path(self.tmpdir.name))
        self.conversation = self.store.create_conversation(
            self.workspace.id, channel="test", channel_key="learn-test"
        )
        self.run = self.store.submit_run(self.conversation.id, {"prompt": "hello"})

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_create_and_get_learning_job(self) -> None:
        job = self.store.create_learning_job(
            run_id=self.run.id,
            conversation_id=self.conversation.id,
            gate_score=5.5,
            gate_signals=["tool_calls", "explicit_preference"],
            context={"user_prompt": "test", "signals": ["tool_calls"]},
        )
        self.assertEqual(job.state, "pending")
        self.assertEqual(job.gate_score, 5.5)
        self.assertEqual(job.gate_signals, ("tool_calls", "explicit_preference"))
        self.assertEqual(job.attempts, 0)
        self.assertIsNone(job.result_json)

        fetched = self.store.get_learning_job(job.id)
        self.assertEqual(fetched.id, job.id)
        self.assertEqual(fetched.state, "pending")

    def test_update_learning_job_state(self) -> None:
        job = self.store.create_learning_job(
            run_id=self.run.id,
            conversation_id=self.conversation.id,
            gate_score=3.0,
            gate_signals=["tool_calls"],
            context={"user_prompt": "x"},
        )
        self.store.update_learning_job(job.id, state="running")
        updated = self.store.get_learning_job(job.id)
        self.assertEqual(updated.state, "running")
        self.assertIsNotNone(updated.started_at)

        self.store.update_learning_job(
            job.id, state="completed",
            result={"worth_learning": True, "summary": "learned something"},
        )
        completed = self.store.get_learning_job(job.id)
        self.assertEqual(completed.state, "completed")
        self.assertIsNotNone(completed.completed_at)
        result = json.loads(completed.result_json)
        self.assertTrue(result["worth_learning"])

    def test_update_learning_job_attempts(self) -> None:
        job = self.store.create_learning_job(
            run_id=self.run.id,
            conversation_id=self.conversation.id,
            gate_score=4.0,
            gate_signals=[],
            context={},
        )
        self.store.update_learning_job(job.id, attempts=3)
        fetched = self.store.get_learning_job(job.id)
        self.assertEqual(fetched.attempts, 3)

    def test_list_learning_jobs_filters(self) -> None:
        self.store.create_learning_job(
            run_id=self.run.id,
            conversation_id=self.conversation.id,
            gate_score=3.0, gate_signals=[], context={},
        )
        run2 = self.store.submit_run(self.conversation.id, {"prompt": "second"})
        self.store.create_learning_job(
            run_id=run2.id,
            conversation_id=self.conversation.id,
            gate_score=6.0, gate_signals=["explicit_learn_request"], context={},
        )
        all_jobs = self.store.list_learning_jobs()
        self.assertEqual(len(all_jobs), 2)

        by_conv = self.store.list_learning_jobs(conversation_id=self.conversation.id)
        self.assertEqual(len(by_conv), 2)

        pending = self.store.list_learning_jobs(states=("pending",))
        self.assertEqual(len(pending), 2)

        self.store.update_learning_job(all_jobs[0].id, state="completed", result={})
        completed = self.store.list_learning_jobs(states=("completed",))
        self.assertEqual(len(completed), 1)

    def test_record_and_list_learning_events(self) -> None:
        event = self.store.record_learning_event(
            run_id=self.run.id,
            conversation_id=self.conversation.id,
            event_type="memory_upsert",
            target="agent:gateway-port",
            status="applied",
            summary="Production gateway runs on port 8787",
            reviewer_model="gemini-2.0-flash",
        )
        self.assertEqual(event.event_type, "memory_upsert")
        self.assertEqual(event.status, "applied")

        events = self.store.list_learning_events(conversation_id=self.conversation.id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].target, "agent:gateway-port")

        events_by_type = self.store.list_learning_events(event_type="memory_upsert")
        self.assertEqual(len(events_by_type), 1)

        events_by_run = self.store.list_learning_events(run_id=self.run.id)
        self.assertEqual(len(events_by_run), 1)

    def test_invalid_learning_job_state_rejected(self) -> None:
        job = self.store.create_learning_job(
            run_id=self.run.id,
            conversation_id=self.conversation.id,
            gate_score=3.0, gate_signals=[], context={},
        )
        with self.assertRaises(ValueError) as ctx:
            self.store.update_learning_job(job.id, state="invalid_state")
        self.assertIn("invalid learning job state", str(ctx.exception))

    def test_get_nonexistent_learning_job(self) -> None:
        with self.assertRaises(KeyError):
            self.store.get_learning_job("nonexistent-id")


class LearningEngineMemoryTests(unittest.TestCase):
    """Tests for the memory merge/write logic in LearningEngine."""

    def test_merge_knowledge_creates_learned_section(self) -> None:
        content = "# Long-term Memory\n\nSome existing content.\n"
        op = MemoryOperation(
            namespace="agent", operation="upsert",
            key="gateway-port", content="Gateway always runs on port 8787",
            confidence=0.95, reason="user stated it",
        )
        result = LearningEngine._merge_knowledge(content, op)
        self.assertIn("## Learned", result)
        self.assertIn("<!-- learn:gateway-port -->", result)
        self.assertIn("Gateway always runs on port 8787", result)
        # Original content preserved
        self.assertIn("Some existing content.", result)

    def test_merge_knowledge_appends_to_existing_section(self) -> None:
        content = "# Memory\n\n## Learned\n- <!-- learn:old-fact --> old fact\n"
        op = MemoryOperation(
            namespace="agent", operation="upsert",
            key="new-fact", content="new knowledge",
        )
        result = LearningEngine._merge_knowledge(content, op)
        self.assertIn("<!-- learn:old-fact -->", result)
        self.assertIn("<!-- learn:new-fact -->", result)
        self.assertEqual(result.count("## Learned"), 1)

    def test_merge_knowledge_replaces_existing_key(self) -> None:
        content = "# Memory\n\n## Learned\n- <!-- learn:port --> old port 80\n"
        op = MemoryOperation(
            namespace="agent", operation="upsert",
            key="port", content="port is 8787 not 80",
        )
        result = LearningEngine._merge_knowledge(content, op)
        self.assertNotIn("old port 80", result)
        self.assertIn("port is 8787 not 80", result)
        self.assertEqual(result.count("<!-- learn:port -->"), 1)

    def test_remove_knowledge_removes_line(self) -> None:
        content = "# Memory\n\n## Learned\n- <!-- learn:stale --> outdated fact\n- <!-- learn:keep --> good fact\n"
        result = LearningEngine._remove_knowledge(content, "stale")
        self.assertNotIn("outdated fact", result)
        self.assertIn("good fact", result)

    def test_remove_knowledge_no_op_if_missing(self) -> None:
        content = "# Memory\n\nNo learned section.\n"
        result = LearningEngine._remove_knowledge(content, "nonexistent")
        self.assertEqual(result, content)


class LearningEngineReviewerParsingTests(unittest.TestCase):
    """Tests for reviewer response parsing."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="gravityclaw-learn-parse-")
        self.store = Store(Path(self.tmpdir.name) / "test.db")
        self.store.initialize()
        self.identity = IdentityStore(Path(self.tmpdir.name) / "identity")
        self.identity.bootstrap()
        self.memory = MemoryService(Path(self.tmpdir.name) / "identity", self.store)
        self.engine = LearningEngine(self.store, self.identity, self.memory, enabled=False)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_parse_valid_response(self) -> None:
        raw = {
            "worth_learning": True,
            "summary": "User prefers tabs",
            "memory": [
                {
                    "namespace": "user",
                    "operation": "upsert",
                    "key": "indent-style",
                    "content": "Prefers tabs over spaces",
                    "confidence": 0.92,
                    "reason": "explicit statement",
                }
            ],
            "skills": [],
        }
        result = self.engine._parse_reviewer_response(raw)
        self.assertIsNotNone(result)
        self.assertTrue(result.worth_learning)
        self.assertEqual(len(result.memory_operations), 1)
        self.assertEqual(result.memory_operations[0].namespace, "user")
        self.assertEqual(result.memory_operations[0].key, "indent-style")
        self.assertEqual(result.summary, "User prefers tabs")

    def test_parse_not_worth_learning(self) -> None:
        raw = {"worth_learning": False, "summary": "", "memory": [], "skills": []}
        result = self.engine._parse_reviewer_response(raw)
        self.assertIsNotNone(result)
        self.assertFalse(result.worth_learning)
        self.assertEqual(len(result.memory_operations), 0)

    def test_parse_openai_wrapped_response(self) -> None:
        raw = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "worth_learning": True,
                        "summary": "learned a fact",
                        "memory": [{"namespace": "agent", "operation": "upsert",
                                     "key": "fact-1", "content": "some fact",
                                     "confidence": 0.8, "reason": "observed"}],
                        "skills": [],
                    })
                }
            }]
        }
        result = self.engine._parse_reviewer_response(raw)
        self.assertIsNotNone(result)
        self.assertTrue(result.worth_learning)
        self.assertEqual(len(result.memory_operations), 1)

    def test_parse_invalid_json_returns_none(self) -> None:
        result = self.engine._parse_reviewer_response({"garbage": True})
        # Should not crash; returns None or a not-worth-learning result
        # Because there's no "worth_learning" key in the data but it's a dict
        # the parser treats it as data with worth_learning=False
        self.assertIsNotNone(result)
        self.assertFalse(result.worth_learning)

    def test_parse_invalid_namespace_defaults_to_agent(self) -> None:
        raw = {
            "worth_learning": True,
            "summary": "test",
            "memory": [{"namespace": "invalid", "operation": "upsert",
                         "key": "k", "content": "c", "confidence": 0.5, "reason": ""}],
            "skills": [],
        }
        result = self.engine._parse_reviewer_response(raw)
        self.assertEqual(result.memory_operations[0].namespace, "agent")


class LearningEngineEndToEndTests(unittest.TestCase):
    """Integration tests: gate → job → review → apply memory."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="gravityclaw-learn-e2e-")
        self.root = Path(self.tmpdir.name)
        self.store = Store(self.root / "test.db")
        self.store.initialize()
        self.workspace = self.store.create_workspace("test", self.root)
        self.conversation = self.store.create_conversation(
            self.workspace.id, channel="test", channel_key="e2e-test"
        )
        self.identity = IdentityStore(self.root / "identity")
        self.identity.bootstrap()
        self.memory = MemoryService(self.root / "identity", self.store)
        self.engine = LearningEngine(
            self.store, self.identity, self.memory,
            enabled=True,
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_ineligible_run_creates_no_job(self) -> None:
        """A trivial turn should not create a learning job."""
        run = self.store.submit_run(self.conversation.id, {"prompt": "thanks"})
        # Simulate transition to completed (normally done by RunManager)
        self.store.transition_run(run.id, "running", expected=("queued",))
        self.store.transition_run(run.id, "completed", expected=("running",))
        completed_run = self.store.get_run(run.id)
        asyncio.run(self.engine.process_run(completed_run))
        jobs = self.store.list_learning_jobs(conversation_id=self.conversation.id)
        self.assertEqual(len(jobs), 0)

    def test_eligible_run_creates_job(self) -> None:
        """A run with enough signals should create a pending learning job."""
        run = self.store.submit_run(
            self.conversation.id, {"prompt": "remember this: always deploy on Fridays"}
        )
        self.store.transition_run(run.id, "running", expected=("queued",))
        self.store.transition_run(run.id, "completed", expected=("running",))
        completed_run = self.store.get_run(run.id)
        # process_run creates a job and fires a background task
        asyncio.run(self.engine.process_run(completed_run))
        jobs = self.store.list_learning_jobs(conversation_id=self.conversation.id)
        self.assertEqual(len(jobs), 1)
        self.assertIn(jobs[0].state, ("pending", "running"))  # background task may start immediately

    def test_full_pipeline_applies_memory(self) -> None:
        """End-to-end: eligible run → reviewer → MEMORY.md updated."""
        run = self.store.submit_run(
            self.conversation.id,
            {"prompt": "remember this: the production gateway always runs behind systemd"},
        )
        self.store.transition_run(run.id, "running", expected=("queued",))
        self.store.transition_run(run.id, "completed", expected=("running",))
        completed_run = self.store.get_run(run.id)

        # Mock the reviewer to return a structured result
        reviewer_response = {
            "worth_learning": True,
            "summary": "Production gateway runs behind systemd",
            "memory": [
                {
                    "namespace": "agent",
                    "operation": "upsert",
                    "key": "gateway-systemd",
                    "content": "The production gateway always runs behind systemd",
                    "confidence": 0.95,
                    "reason": "User explicitly stated this",
                }
            ],
            "skills": [],
        }
        parsed_result = self.engine._parse_reviewer_response(reviewer_response)

        async def run_pipeline():
            with patch.object(
                self.engine, "_call_reviewer",
                new=AsyncMock(return_value=parsed_result),
            ):
                await self.engine.process_run(completed_run)
                # Wait for background task to complete
                if self.engine._background_tasks:
                    await asyncio.gather(*self.engine._background_tasks, return_exceptions=True)

        asyncio.run(run_pipeline())

        # Verify MEMORY.md was updated
        memory_doc = self.identity.load(("MEMORY.md",))[0]
        self.assertIn("gateway-systemd", memory_doc.content)
        self.assertIn("runs behind systemd", memory_doc.content)

        # Verify learning event was recorded
        events = self.store.list_learning_events(conversation_id=self.conversation.id)
        applied_events = [e for e in events if e.status == "applied" and e.event_type == "memory_upsert"]
        self.assertGreaterEqual(len(applied_events), 1)

        # Verify learning job completed
        jobs = self.store.list_learning_jobs(conversation_id=self.conversation.id)
        completed_jobs = [j for j in jobs if j.state == "completed"]
        self.assertGreaterEqual(len(completed_jobs), 1)

    def test_reviewer_returns_not_worth_learning(self) -> None:
        """If reviewer says nothing to learn, a review_skipped event is recorded."""
        run = self.store.submit_run(
            self.conversation.id,
            {"prompt": "I prefer dark mode for the editor"},
        )
        self.store.transition_run(run.id, "running", expected=("queued",))
        self.store.transition_run(run.id, "completed", expected=("running",))
        completed_run = self.store.get_run(run.id)

        async def run_pipeline():
            with patch.object(
                self.engine, "_call_reviewer",
                new=AsyncMock(
                    return_value=LearningResult(worth_learning=False, reviewer_model="gemini-2.0-flash")
                ),
            ):
                await self.engine.process_run(completed_run)
                if self.engine._background_tasks:
                    await asyncio.gather(*self.engine._background_tasks, return_exceptions=True)

        asyncio.run(run_pipeline())

        events = self.store.list_learning_events(conversation_id=self.conversation.id)
        skipped = [e for e in events if e.event_type == "review_skipped"]
        self.assertGreaterEqual(len(skipped), 1)

    def test_disabled_engine_does_nothing(self) -> None:
        """When disabled, process_run is a no-op."""
        self.engine.enabled = False
        run = self.store.submit_run(
            self.conversation.id,
            {"prompt": "remember this: important fact"},
        )
        self.store.transition_run(run.id, "running", expected=("queued",))
        self.store.transition_run(run.id, "completed", expected=("running",))
        completed_run = self.store.get_run(run.id)
        asyncio.run(self.engine.process_run(completed_run))
        jobs = self.store.list_learning_jobs()
        self.assertEqual(len(jobs), 0)

    def test_approval_required_creates_pending_event(self) -> None:
        """With memory_approval_required=True, operations become pending_approval."""
        self.engine.memory_approval_required = True
        run = self.store.submit_run(
            self.conversation.id,
            {"prompt": "remember this: need approval for this fact"},
        )
        self.store.transition_run(run.id, "running", expected=("queued",))
        self.store.transition_run(run.id, "completed", expected=("running",))
        completed_run = self.store.get_run(run.id)

        reviewer_response = {
            "worth_learning": True,
            "summary": "fact requiring approval",
            "memory": [{
                "namespace": "agent", "operation": "upsert",
                "key": "approved-fact", "content": "needs approval",
                "confidence": 0.9, "reason": "test",
            }],
            "skills": [],
        }
        parsed_result = self.engine._parse_reviewer_response(reviewer_response)

        async def run_pipeline():
            with patch.object(
                self.engine, "_call_reviewer",
                new=AsyncMock(return_value=parsed_result),
            ):
                await self.engine.process_run(completed_run)
                if self.engine._background_tasks:
                    await asyncio.gather(*self.engine._background_tasks, return_exceptions=True)

        asyncio.run(run_pipeline())

        events = self.store.list_learning_events(conversation_id=self.conversation.id)
        pending = [e for e in events if e.status == "pending_approval"]
        self.assertGreaterEqual(len(pending), 1)

        # MEMORY.md should NOT have been modified
        memory_doc = self.identity.load(("MEMORY.md",))[0]
        self.assertNotIn("approved-fact", memory_doc.content)

    def test_schema_migration_from_v12_creates_learning_tables(self) -> None:
        """Simulates a v12→v13 migration and verifies learning tables exist."""
        fresh_db = self.root / "migrate.db"
        store = Store(fresh_db)
        store.initialize()
        # Simulate downgrade to v12 and re-migrate
        with store._connect() as connection:
            connection.execute("UPDATE metadata SET value='12' WHERE key='schema_version'")
            connection.execute("DROP TABLE IF EXISTS learning_jobs")
            connection.execute("DROP TABLE IF EXISTS learning_events")
        store.initialize()
        with store._connect() as connection:
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertEqual(version, "18")
        self.assertIn("learning_jobs", tables)
        self.assertIn("learning_events", tables)


if __name__ == "__main__":
    unittest.main()
