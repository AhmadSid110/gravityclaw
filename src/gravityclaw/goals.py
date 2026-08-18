"""Persistent Goal Mode — evaluation and continuation logic.

After each run completes, the goal evaluator checks whether the active goal
is satisfied, needs more turns, or has failed. Deterministic acceptance
criteria (command exit codes, file existence, test results) are checked first.
Only ambiguous goals require an AGY judging turn.

The continuation flow reuses the existing RunManager.submit() path so there
is no new agent runtime — just a follow-up run in the same conversation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .store import GoalRecord, GoalEvaluationRecord, RunRecord, Store


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    verdict: str  # "continue" | "done" | "failed" | "paused"
    reason: str
    acceptance_state: list[dict[str, Any]]


class GoalEvaluator:
    """Evaluate whether a goal is satisfied after a run completes.

    Supports three acceptance criterion types:
    - command: shell command must exit 0
    - file_exists: file path must exist
    - test: run a test command and check exit 0

    If all criteria pass → done. If turn budget exhausted → failed.
    If no criteria are defined → always returns "continue" (requires AGY
    judging, handled by the continuation prompt).
    """

    def __init__(self, store: Store, workspace_path: Path | None = None) -> None:
        self.store = store
        self.workspace_path = workspace_path

    def evaluate(
        self,
        goal: GoalRecord,
        run: RunRecord,
    ) -> EvaluationResult:
        """Synchronous evaluation of deterministic acceptance criteria."""
        if not goal.acceptance:
            # No deterministic criteria — rely on AGY self-evaluation in continuation.
            if goal.turns_used >= goal.max_turns:
                return EvaluationResult(
                    verdict="failed",
                    reason=f"turn budget exhausted ({goal.turns_used}/{goal.max_turns})",
                    acceptance_state=[],
                )
            return EvaluationResult(
                verdict="continue",
                reason="no deterministic criteria; agent self-evaluates",
                acceptance_state=[],
            )

        # Run failed — don't bother checking acceptance unless turns remain
        if run.status == "failed":
            if goal.turns_used >= goal.max_turns:
                return EvaluationResult(
                    verdict="failed",
                    reason=f"run failed and turn budget exhausted ({goal.turns_used}/{goal.max_turns})",
                    acceptance_state=self._mark_all_pending(goal.acceptance),
                )
            return EvaluationResult(
                verdict="continue",
                reason=f"run failed; {goal.max_turns - goal.turns_used} turns remain",
                acceptance_state=self._mark_all_pending(goal.acceptance),
            )

        # Evaluate each criterion
        results: list[dict[str, Any]] = []
        all_passed = True
        for criterion in goal.acceptance:
            passed, detail = self._check_criterion(criterion)
            results.append({
                **criterion,
                "passed": passed,
                "detail": detail,
            })
            if not passed:
                all_passed = False

        if all_passed:
            return EvaluationResult(
                verdict="done",
                reason="all acceptance criteria passed",
                acceptance_state=results,
            )

        if goal.turns_used >= goal.max_turns:
            failed_names = [
                r.get("description", r.get("type", "unnamed"))
                for r in results if not r["passed"]
            ]
            return EvaluationResult(
                verdict="failed",
                reason=f"turn budget exhausted; unmet: {', '.join(failed_names)}",
                acceptance_state=results,
            )

        return EvaluationResult(
            verdict="continue",
            reason=f"{sum(1 for r in results if r['passed'])}/{len(results)} criteria met; continuing",
            acceptance_state=results,
        )

    def _check_criterion(self, criterion: dict[str, Any]) -> tuple[bool, str]:
        """Return (passed, detail_message) for a single criterion."""
        ctype = criterion.get("type", "")
        try:
            if ctype == "command":
                return self._check_command(criterion)
            elif ctype == "file_exists":
                return self._check_file_exists(criterion)
            elif ctype == "test":
                return self._check_command(criterion)  # Same as command
            else:
                return False, f"unknown criterion type: {ctype}"
        except Exception as exc:
            return False, f"evaluation error: {exc}"

    def _check_command(self, criterion: dict[str, Any]) -> tuple[bool, str]:
        command = criterion.get("command", "")
        if not command:
            return False, "no command specified"
        cwd = str(self.workspace_path) if self.workspace_path else None
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                return True, "exit 0"
            return False, f"exit {result.returncode}: {result.stderr[:200]}"
        except subprocess.TimeoutExpired:
            return False, "command timed out (60s)"

    def _check_file_exists(self, criterion: dict[str, Any]) -> tuple[bool, str]:
        path_str = criterion.get("path", "")
        if not path_str:
            return False, "no path specified"
        target = Path(path_str)
        if not target.is_absolute() and self.workspace_path:
            target = self.workspace_path / target
        if target.exists():
            return True, f"exists: {target}"
        return False, f"not found: {target}"

    @staticmethod
    def _mark_all_pending(acceptance: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**c, "passed": False, "detail": "not evaluated"} for c in acceptance]


def build_continuation_prompt(goal: GoalRecord, evaluation: EvaluationResult) -> str:
    """Build the prompt for a goal continuation run.

    This prompt is injected as the user message for the follow-up run,
    giving the agent context about the goal state and remaining work.
    """
    parts = [
        f"[Goal Continuation — turn {goal.turns_used}/{goal.max_turns}]",
        f"Objective: {goal.objective}",
        "",
        f"Previous evaluation: {evaluation.reason}",
    ]

    if evaluation.acceptance_state:
        parts.append("")
        parts.append("Acceptance criteria status:")
        for item in evaluation.acceptance_state:
            status = "\u2713" if item.get("passed") else "\u25cb"
            desc = item.get("description", item.get("command", item.get("path", "unnamed")))
            parts.append(f"  {status} {desc}")
            if not item.get("passed") and item.get("detail"):
                parts.append(f"    \u2192 {item['detail']}")

    if goal.current_step:
        parts.append("")
        parts.append(f"Last known step: {goal.current_step}")

    parts.append("")
    parts.append(
        "Continue working toward the objective. "
        "When you believe the goal is fully met, state that explicitly."
    )
    return "\n".join(parts)
