"""Causal Attribution — feeds skill execution outcomes back into the reviewer.

When a skill-influenced run completes, this module constructs structured
feedback that tells the reviewer exactly which skills were loaded, what
happened, and whether the agent deviated.

This transforms the reviewer from "infer from entire conversation" to
"here are the specific skills and their measured outcomes."
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

LOGGER = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Attribution data structures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SkillOutcome:
    """Measured outcome of a single skill's influence on a run."""
    skill_id: str
    skill_name: str
    revision: int
    result: str           # "successful" | "failed" | "corrected" | "partial"
    deviation_summary: str = ""  # Brief description of how the agent deviated
    steps_attempted: int = 0
    steps_succeeded: int = 0


@dataclass(frozen=True, slots=True)
class AttributionReport:
    """Complete causal attribution for a run's skill usage.

    This is the structured signal passed to the reviewer to enable
    targeted self-improvement proposals.
    """
    run_id: str
    outcomes: tuple[SkillOutcome, ...] = ()
    has_corrections: bool = False
    overall_success: bool = True

    def to_reviewer_context(self) -> dict[str, Any]:
        """Format as the JSON structure the reviewer receives.

        Produces the `loaded_skills` signal documented in the spec.
        """
        if not self.outcomes:
            return {}

        return {
            "loaded_skills": [
                {
                    "skill_id": o.skill_id,
                    "skill_name": o.skill_name,
                    "revision": o.revision,
                    "result": o.result,
                    "deviation_summary": o.deviation_summary,
                    "steps_attempted": o.steps_attempted,
                    "steps_succeeded": o.steps_succeeded,
                }
                for o in self.outcomes
            ],
            "has_corrections": self.has_corrections,
            "overall_success": self.overall_success,
        }

    def needs_skill_update(self) -> bool:
        """Whether any skill outcome suggests a revision is needed."""
        return any(
            o.result in ("corrected", "failed") for o in self.outcomes
        )


# ─────────────────────────────────────────────────────────────────────────────
# Attribution builder
# ─────────────────────────────────────────────────────────────────────────────


def build_attribution(
    run_id: str,
    run_skill_context: dict[str, Any] | None,
    telemetry_events: list[dict[str, Any]],
) -> AttributionReport:
    """Build an attribution report from a completed run's context and telemetry.

    Args:
        run_id: The completed run's ID.
        run_skill_context: The RunSkillContext dict (from JSON) or None.
        telemetry_events: Skill usage telemetry events for this run.

    Returns:
        AttributionReport with per-skill outcomes.
    """
    if not run_skill_context:
        return AttributionReport(run_id=run_id)

    loaded_skills = run_skill_context.get("loaded", [])
    if not loaded_skills:
        return AttributionReport(run_id=run_id)

    # Build a map of skill_id → telemetry events for this run
    skill_events: dict[str, list[str]] = {}
    for event in telemetry_events:
        sid = event.get("skill_id", "")
        etype = event.get("event", "")
        if sid and etype:
            skill_events.setdefault(sid, []).append(etype)

    outcomes: list[SkillOutcome] = []
    has_corrections = False

    for loaded in loaded_skills:
        skill_id = loaded.get("skill_id", "")
        skill_name = loaded.get("name", "")
        revision = loaded.get("revision", 0)

        events_for_skill = skill_events.get(skill_id, [])

        # Determine outcome from telemetry
        result = _determine_outcome(events_for_skill)
        if result == "corrected":
            has_corrections = True

        outcomes.append(SkillOutcome(
            skill_id=skill_id,
            skill_name=skill_name,
            revision=revision,
            result=result,
            deviation_summary=_build_deviation_summary(result, events_for_skill),
        ))

    overall_success = all(o.result in ("successful",) for o in outcomes) if outcomes else True

    return AttributionReport(
        run_id=run_id,
        outcomes=tuple(outcomes),
        has_corrections=has_corrections,
        overall_success=overall_success,
    )


def _determine_outcome(events: list[str]) -> str:
    """Determine the outcome from a list of telemetry events for a skill."""
    if "corrected" in events:
        return "corrected"
    if "failed" in events:
        return "failed"
    if "successful" in events:
        return "successful"
    if "executed" in events:
        return "partial"
    return "successful"  # Loaded but no explicit outcome → assumed OK


def _build_deviation_summary(result: str, events: list[str]) -> str:
    """Build a brief summary of how the agent deviated (if at all)."""
    if result == "corrected":
        return "Agent deviated from skill procedure and found an alternative approach."
    elif result == "failed":
        return "Skill procedure did not achieve the desired outcome."
    elif result == "partial":
        return "Skill was executed but outcome was not explicitly recorded."
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Integration with Learning Engine
# ─────────────────────────────────────────────────────────────────────────────


def enrich_reviewer_context(
    context: dict[str, Any],
    attribution: AttributionReport,
) -> dict[str, Any]:
    """Enrich the reviewer's input context with causal attribution.

    Merges the attribution data into the context dict that gets sent
    to the auxiliary reviewer model.
    """
    if not attribution.outcomes:
        return context

    enriched = dict(context)
    enriched["skill_attribution"] = attribution.to_reviewer_context()

    # Add a signal that skill correction happened
    if attribution.has_corrections:
        signals = enriched.get("signals", [])
        if "skill_was_corrected" not in signals:
            signals.append("skill_was_corrected")
        enriched["signals"] = signals

    return enriched
