"""Skill usage telemetry — tracks discovery, selection, loading, and execution events.

Events:
  discovered - skill appeared in context discovery
  selected   - skill was chosen for potential use
  loaded     - full SKILL.md was loaded into context
  executed   - skill procedure was followed
  successful - skill led to successful outcome
  failed     - skill procedure failed
  corrected  - skill content was corrected/overridden
  proposal_generated - a new proposal was generated for this skill
"""

from __future__ import annotations

from .registry import SkillRegistry


VALID_EVENTS = frozenset({
    "discovered",
    "matched",
    "presented",
    "selected",
    "loaded",
    "executed",
    "successful",
    "failed",
    "corrected",
    "proposal_generated",
})


class TelemetryService:
    """Records and queries skill usage telemetry."""

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    def record(
        self,
        skill_id: str,
        event: str,
        *,
        run_id: str | None = None,
    ) -> None:
        """Record a usage event for a skill.

        Validates the event type before recording.
        """
        if event not in VALID_EVENTS:
            raise ValueError(f"invalid telemetry event: {event}")
        self._registry.record_usage(skill_id, event, run_id=run_id)

    def stats(self, skill_id: str) -> dict[str, int]:
        """Get aggregated usage statistics for a skill."""
        return self._registry.usage_stats(skill_id)

    def success_rate(self, skill_id: str) -> float | None:
        """Calculate the success rate for a skill.

        Returns None if the skill has never been executed.
        Returns a float 0.0–1.0 representing success / (success + failed).
        """
        stats = self.stats(skill_id)
        executed = stats.get("executed", 0)
        if executed == 0:
            return None
        successful = stats.get("successful", 0)
        failed = stats.get("failed", 0)
        total_outcomes = successful + failed
        if total_outcomes == 0:
            return None
        return successful / total_outcomes
