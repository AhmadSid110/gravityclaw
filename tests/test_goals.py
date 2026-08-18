"""Tests for Persistent Goal Mode — store CRUD and evaluator logic."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gravityclaw.goals import GoalEvaluator, EvaluationResult, build_continuation_prompt
from gravityclaw.store import GoalRecord, GoalEvaluationRecord, RunRecord, Store


class GoalStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="gravityclaw-goal-")
        self.store = Store(Path(self.tmpdir.name) / "test.db")
        self.store.initialize()
        self.workspace = self.store.create_workspace("test", Path(self.tmpdir.name))
        self.conversation = self.store.create_conversation(
            self.workspace.id, channel="test", channel_key="goal-test"
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_create_and_get_goal(self) -> None:
        goal = self.store.create_goal(
            self.conversation.id,
            "Get all tests passing",
            acceptance=[{"type": "command", "command": "true", "description": "All tests pass"}],
            max_turns=10,
        )
        self.assertEqual(goal.status, "active")
        self.assertEqual(goal.objective, "Get all tests passing")
        self.assertEqual(goal.max_turns, 10)
        self.assertEqual(goal.turns_used, 0)
        self.assertEqual(len(goal.acceptance), 1)

        fetched = self.store.get_goal(goal.id)
        self.assertEqual(fetched.id, goal.id)

    def test_only_one_active_goal_per_conversation(self) -> None:
        self.store.create_goal(self.conversation.id, "First goal")
        with self.assertRaises(ValueError) as ctx:
            self.store.create_goal(self.conversation.id, "Second goal")
        self.assertIn("already has an active goal", str(ctx.exception))

    def test_get_active_goal(self) -> None:
        self.assertIsNone(self.store.get_active_goal(self.conversation.id))
        goal = self.store.create_goal(self.conversation.id, "My goal")
        active = self.store.get_active_goal(self.conversation.id)
        self.assertIsNotNone(active)
        self.assertEqual(active.id, goal.id)

    def test_update_goal_status(self) -> None:
        goal = self.store.create_goal(self.conversation.id, "Work")
        updated = self.store.update_goal(goal.id, status="paused")
        self.assertEqual(updated.status, "paused")
        # Paused still blocks creating a new goal
        with self.assertRaises(ValueError):
            self.store.create_goal(self.conversation.id, "Another")
        # Complete it
        completed = self.store.update_goal(goal.id, status="completed")
        self.assertEqual(completed.status, "completed")
        # Now we can create a new one
        new_goal = self.store.create_goal(self.conversation.id, "New goal")
        self.assertEqual(new_goal.status, "active")

    def test_increment_goal_turn(self) -> None:
        goal = self.store.create_goal(self.conversation.id, "Work")
        run = self.store.submit_run(self.conversation.id, {"prompt": "test"})
        updated = self.store.increment_goal_turn(goal.id, run.id)
        self.assertEqual(updated.turns_used, 1)
        self.assertEqual(updated.last_run_id, run.id)

    def test_record_and_list_evaluations(self) -> None:
        goal = self.store.create_goal(self.conversation.id, "Work")
        ev = self.store.record_goal_evaluation(
            goal.id, run_id=None, turn_number=1,
            verdict="continue", reason="still working",
        )
        self.assertEqual(ev.verdict, "continue")
        evaluations = self.store.list_goal_evaluations(goal.id)
        self.assertEqual(len(evaluations), 1)
        self.assertEqual(evaluations[0].reason, "still working")

    def test_list_goals_with_filter(self) -> None:
        goal1 = self.store.create_goal(self.conversation.id, "Active goal")
        self.store.update_goal(goal1.id, status="completed")
        # Now create another
        goal2 = self.store.create_goal(self.conversation.id, "New active")
        all_goals = self.store.list_goals(conversation_id=self.conversation.id)
        self.assertEqual(len(all_goals), 2)
        active_only = self.store.list_goals(
            conversation_id=self.conversation.id, statuses=("active",)
        )
        self.assertEqual(len(active_only), 1)
        self.assertEqual(active_only[0].id, goal2.id)


class GoalEvaluatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="gravityclaw-eval-")
        self.store = Store(Path(self.tmpdir.name) / "test.db")
        self.store.initialize()
        self.workspace = self.store.create_workspace("test", Path(self.tmpdir.name))
        self.evaluator = GoalEvaluator(self.store, workspace_path=Path(self.tmpdir.name))

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _make_run(self, status: str = "completed") -> RunRecord:
        conversation = self.store.create_conversation(
            self.workspace.id, channel="test", channel_key=f"eval-{id(self)}"
        )
        run = self.store.submit_run(conversation.id, {"prompt": "test"})
        self.store.transition_run(run.id, "running", expected=("queued",))
        self.store.transition_run(run.id, status, expected=("running",))
        return self.store.get_run(run.id)

    def test_no_criteria_continues(self) -> None:
        goal = GoalRecord(
            id="g1", conversation_id="c1", objective="Do stuff",
            acceptance=[], status="active", max_turns=5, turns_used=1,
            current_step=None, last_run_id=None, created_at="", updated_at="",
        )
        run = self._make_run("completed")
        result = self.evaluator.evaluate(goal, run)
        self.assertEqual(result.verdict, "continue")

    def test_no_criteria_fails_at_budget(self) -> None:
        goal = GoalRecord(
            id="g1", conversation_id="c1", objective="Do stuff",
            acceptance=[], status="active", max_turns=5, turns_used=5,
            current_step=None, last_run_id=None, created_at="", updated_at="",
        )
        run = self._make_run("completed")
        result = self.evaluator.evaluate(goal, run)
        self.assertEqual(result.verdict, "failed")
        self.assertIn("budget exhausted", result.reason)

    def test_command_criterion_passes(self) -> None:
        goal = GoalRecord(
            id="g1", conversation_id="c1", objective="Do stuff",
            acceptance=[{"type": "command", "command": "true", "description": "Always passes"}],
            status="active", max_turns=5, turns_used=1,
            current_step=None, last_run_id=None, created_at="", updated_at="",
        )
        run = self._make_run("completed")
        result = self.evaluator.evaluate(goal, run)
        self.assertEqual(result.verdict, "done")
        self.assertTrue(result.acceptance_state[0]["passed"])

    def test_command_criterion_fails_continues(self) -> None:
        goal = GoalRecord(
            id="g1", conversation_id="c1", objective="Do stuff",
            acceptance=[{"type": "command", "command": "false", "description": "Always fails"}],
            status="active", max_turns=5, turns_used=1,
            current_step=None, last_run_id=None, created_at="", updated_at="",
        )
        run = self._make_run("completed")
        result = self.evaluator.evaluate(goal, run)
        self.assertEqual(result.verdict, "continue")
        self.assertFalse(result.acceptance_state[0]["passed"])

    def test_file_exists_criterion(self) -> None:
        # Create a file in the temp workspace
        (Path(self.tmpdir.name) / "marker.txt").write_text("ok")
        goal = GoalRecord(
            id="g1", conversation_id="c1", objective="Create marker",
            acceptance=[{"type": "file_exists", "path": "marker.txt", "description": "marker exists"}],
            status="active", max_turns=5, turns_used=1,
            current_step=None, last_run_id=None, created_at="", updated_at="",
        )
        run = self._make_run("completed")
        result = self.evaluator.evaluate(goal, run)
        self.assertEqual(result.verdict, "done")

    def test_continuation_prompt_format(self) -> None:
        goal = GoalRecord(
            id="g1", conversation_id="c1", objective="Fix all bugs",
            acceptance=[{"type": "command", "command": "pytest", "description": "Tests pass"}],
            status="active", max_turns=10, turns_used=3,
            current_step="Running pytest", last_run_id=None, created_at="", updated_at="",
        )
        evaluation = EvaluationResult(
            verdict="continue",
            reason="1/1 criteria met; continuing",
            acceptance_state=[{"type": "command", "passed": False, "description": "Tests pass", "detail": "exit 1"}],
        )
        prompt = build_continuation_prompt(goal, evaluation)
        self.assertIn("Goal Continuation", prompt)
        self.assertIn("turn 3/10", prompt)
        self.assertIn("Fix all bugs", prompt)
        self.assertIn("Tests pass", prompt)
        self.assertIn("Running pytest", prompt)


if __name__ == "__main__":
    unittest.main()
