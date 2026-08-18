"""Skill Curator — deterministic lifecycle management for learned skills.

Uses behavioral telemetry evidence (not just age) to manage the skill
lifecycle state machine:

    ACTIVE → STALE → ARCHIVED
    ARCHIVED → ACTIVE (on reuse or explicit restore)

Respects ownership rules:
    - user-owned → never mutated
    - bundled → immutable
    - agent-owned → curator eligible
    - pinned → never stale/archive

The curator is deterministic (no LLM calls) and runs as a scheduled job.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..store import Store, utc_now
from .models import SkillOwner, SkillState
from .registry import SkillRegistry
from .trust import (
    OperationContext,
    OperationKind,
    PolicyResult,
    TrustDecision,
    TrustPolicy,
)

LOGGER = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CuratorConfig:
    """Deterministic lifecycle thresholds.

    Loaded from [learning.curator] in gravityclaw.toml.
    """
    enabled: bool = True
    stale_after_days: int = 30
    archive_after_days: int = 90
    minimum_invocations: int = 2     # Below this, utility is unknown → apply age rules
    min_idle_hours: int = 2          # Skip curator if last activity was recent
    utility_stale_threshold: float = 0.3   # Utility score below which → STALE
    utility_archive_threshold: float = 0.1  # Below which → ARCHIVED

    @classmethod
    def from_toml(cls, section: dict[str, Any]) -> "CuratorConfig":
        """Parse from [learning.curator] TOML section."""
        return cls(
            enabled=bool(section.get("enabled", True)),
            stale_after_days=int(section.get("stale_after_days", 30)),
            archive_after_days=int(section.get("archive_after_days", 90)),
            minimum_invocations=int(section.get("minimum_invocations", 2)),
            min_idle_hours=int(section.get("min_idle_hours", 2)),
            utility_stale_threshold=float(section.get("utility_stale_threshold", 0.3)),
            utility_archive_threshold=float(section.get("utility_archive_threshold", 0.1)),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Utility scoring
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SkillUtility:
    """Computed utility score and supporting metrics for a skill."""
    skill_id: str
    name: str
    score: float                # 0.0–1.0 derived utility
    success_rate: float         # 0.0–1.0
    execution_count: int
    last_used: str | None       # ISO timestamp or None
    recency_factor: float       # 0.0–1.0 exponential decay
    correction_penalty: float   # 0.0–1.0 (1.0 = no corrections)
    days_since_last_use: float | None


def compute_utility(
    stats: dict[str, int],
    last_used_at: str | None,
    *,
    now: datetime | None = None,
) -> tuple[float, float, float, float | None]:
    """Compute the utility score from telemetry stats.

    Returns (score, recency_factor, correction_penalty, days_since_last_use).

    Formula:
        utility = success_rate × log(executions + 1) × recency_factor × correction_penalty
        normalized to [0, 1] via sigmoid-like clamp.
    """
    _now = now or datetime.now(UTC)

    executed = stats.get("executed", 0)
    successful = stats.get("successful", 0)
    failed = stats.get("failed", 0)
    corrected = stats.get("corrected", 0)

    # Success rate
    total_outcomes = successful + failed
    if total_outcomes == 0:
        success_rate = 0.5  # Neutral when no evidence
    else:
        success_rate = successful / total_outcomes

    # Volume factor: log(executions + 1) normalized to [0, 1]
    # At 50 executions → ~0.85, at 100 → ~0.92
    volume_factor = min(1.0, math.log(executed + 1) / math.log(100 + 1))

    # Recency factor: exponential decay based on days since last use
    days_since_last_use: float | None = None
    if last_used_at:
        try:
            last_dt = datetime.fromisoformat(last_used_at)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=UTC)
            delta = _now - last_dt
            days_since_last_use = max(0.0, delta.total_seconds() / 86400)
        except (ValueError, TypeError):
            days_since_last_use = None

    if days_since_last_use is not None:
        # Half-life of 30 days: after 30 days, recency = 0.5
        recency_factor = math.exp(-0.693 * days_since_last_use / 30.0)
    else:
        recency_factor = 0.3  # Low recency when never used

    # Correction penalty: each correction reduces the penalty
    if executed > 0:
        correction_ratio = corrected / executed
        correction_penalty = max(0.1, 1.0 - correction_ratio)
    else:
        correction_penalty = 1.0

    # Final score
    raw_score = success_rate * volume_factor * recency_factor * correction_penalty
    # Clamp to [0, 1]
    score = max(0.0, min(1.0, raw_score))

    return score, recency_factor, correction_penalty, days_since_last_use


# ─────────────────────────────────────────────────────────────────────────────
# Curator actions
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class CuratorAction:
    """A single lifecycle action taken by the curator."""
    skill_id: str
    skill_name: str
    action: str       # "mark_stale" | "archive" | "restore" | "flag_duplicate"
    reason: str
    utility: SkillUtility | None = None


@dataclass(slots=True)
class CuratorReport:
    """Summary of a curator run."""
    skills_evaluated: int = 0
    actions_taken: list[CuratorAction] = field(default_factory=list)
    actions_blocked: list[CuratorAction] = field(default_factory=list)
    skipped_protected: int = 0  # user/bundled/pinned
    errors: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Curator engine
# ─────────────────────────────────────────────────────────────────────────────


class Curator:
    """Deterministic skill lifecycle manager.

    Evaluates each eligible skill's utility and transitions it through
    the ACTIVE → STALE → ARCHIVED state machine based on behavioral evidence.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        store: Store,
        trust_policy: TrustPolicy,
        config: CuratorConfig | None = None,
    ) -> None:
        self._registry = registry
        self._store = store
        self._policy = trust_policy
        self._config = config or CuratorConfig()

    @property
    def config(self) -> CuratorConfig:
        return self._config

    def run(self, *, now: datetime | None = None) -> CuratorReport:
        """Execute a full curator cycle.

        Evaluates all registered skills and applies lifecycle transitions
        based on utility scores and deterministic rules.

        Does not use an LLM. Safe to run frequently.
        """
        if not self._config.enabled:
            return CuratorReport()

        _now = now or datetime.now(UTC)
        report = CuratorReport()

        # Get all skills (including stale — they might need archiving or restoration)
        skills = self._registry.list_skills(limit=1000)

        for skill in skills:
            report.skills_evaluated += 1

            # Skip protected skills entirely
            if skill.owner in (SkillOwner.USER, SkillOwner.BUNDLED):
                report.skipped_protected += 1
                continue
            if skill.pinned:
                report.skipped_protected += 1
                continue

            # Compute utility
            utility = self._compute_skill_utility(skill, now=_now)

            # Determine action based on current state + utility
            action = self._determine_action(skill, utility, now=_now)
            if action is None:
                continue

            # Check trust policy
            op_kind = _action_to_operation_kind(action.action)
            if op_kind is None:
                continue

            ctx = OperationContext(
                kind=op_kind,
                skill_owner=skill.owner,
                skill_state=skill.state,
                skill_pinned=skill.pinned,
            )
            policy_result = self._policy.evaluate(ctx)

            if policy_result.decision == TrustDecision.DENY:
                report.actions_blocked.append(action)
                LOGGER.debug(
                    "curator: action %s on '%s' denied: %s",
                    action.action, skill.name, policy_result.reason,
                )
                continue

            if policy_result.decision == TrustDecision.REQUIRE_APPROVAL:
                # In a full implementation, this would create a proposal.
                # For Phase 3, we just block and report.
                report.actions_blocked.append(action)
                LOGGER.info(
                    "curator: action %s on '%s' requires approval",
                    action.action, skill.name,
                )
                continue

            # ALLOW — apply the action
            try:
                self._apply_action(action)
                report.actions_taken.append(action)
                LOGGER.info(
                    "curator: %s skill '%s' (utility=%.3f, reason=%s)",
                    action.action, skill.name,
                    utility.score if utility else 0,
                    action.reason,
                )
            except Exception as exc:
                report.errors.append(f"{action.action} on {skill.name}: {exc}")
                LOGGER.warning(
                    "curator: failed to %s '%s': %s",
                    action.action, skill.name, exc,
                )

        return report

    def evaluate_skill(self, skill_name: str, *, now: datetime | None = None) -> SkillUtility | None:
        """Compute utility for a single skill. Useful for diagnostics."""
        skill = self._registry.get_skill_by_name(skill_name)
        if skill is None:
            return None
        return self._compute_skill_utility(skill, now=now or datetime.now(UTC))

    def restore_on_reuse(self, skill_name: str) -> bool:
        """Automatically restore an archived skill when it's reused.

        Called by the runtime when a previously-archived skill is selected
        during a run. Returns True if restored, False if not needed/allowed.
        """
        skill = self._registry.get_skill_by_name(skill_name)
        if skill is None:
            return False
        if skill.state != SkillState.ARCHIVED:
            return False  # Not archived, nothing to do

        # Check trust policy
        ctx = OperationContext(
            kind=OperationKind.CURATOR_RESTORE,
            skill_owner=skill.owner,
            skill_state=skill.state,
            skill_pinned=skill.pinned,
        )
        result = self._policy.evaluate(ctx)
        if result.decision != TrustDecision.ALLOW:
            return False

        # Restore
        self._registry.update_skill(skill.skill_id, state=SkillState.ACTIVE)
        LOGGER.info("curator: auto-restored '%s' on reuse", skill.name)
        return True

    # ──────────────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────────────

    def _compute_skill_utility(
        self, skill: Any, *, now: datetime,
    ) -> SkillUtility:
        """Compute the utility score for a skill from telemetry."""
        stats = self._registry.usage_stats(skill.skill_id)

        # Find last usage timestamp
        last_used_at = self._get_last_usage_time(skill.skill_id)

        score, recency_factor, correction_penalty, days = compute_utility(
            stats, last_used_at, now=now,
        )

        return SkillUtility(
            skill_id=skill.skill_id,
            name=skill.name,
            score=score,
            success_rate=_success_rate(stats),
            execution_count=stats.get("executed", 0),
            last_used=last_used_at,
            recency_factor=recency_factor,
            correction_penalty=correction_penalty,
            days_since_last_use=days,
        )

    def _get_last_usage_time(self, skill_id: str) -> str | None:
        """Get the most recent usage timestamp for a skill."""
        with self._store._connect() as conn:
            row = conn.execute(
                """SELECT created_at FROM skill_usage
                   WHERE skill_id=? AND event IN ('executed','successful','loaded','selected')
                   ORDER BY created_at DESC LIMIT 1""",
                (skill_id,),
            ).fetchone()
        return row["created_at"] if row else None

    def _determine_action(
        self, skill: Any, utility: SkillUtility, *, now: datetime,
    ) -> CuratorAction | None:
        """Determine what lifecycle action (if any) should be taken."""
        config = self._config

        # ARCHIVED skills: check if they should be restored (only on reuse — handled elsewhere)
        if skill.state == SkillState.ARCHIVED:
            return None

        # Check if enough data to judge
        has_sufficient_data = utility.execution_count >= config.minimum_invocations

        if skill.state == SkillState.ACTIVE:
            # Active → Stale?
            if has_sufficient_data:
                if utility.score < config.utility_stale_threshold:
                    return CuratorAction(
                        skill_id=skill.skill_id,
                        skill_name=skill.name,
                        action="mark_stale",
                        reason=f"utility {utility.score:.3f} < stale threshold {config.utility_stale_threshold}",
                        utility=utility,
                    )
            else:
                # Low execution count: fall back to age-based rule
                if utility.days_since_last_use is not None:
                    if utility.days_since_last_use > config.stale_after_days:
                        return CuratorAction(
                            skill_id=skill.skill_id,
                            skill_name=skill.name,
                            action="mark_stale",
                            reason=f"unused for {utility.days_since_last_use:.0f} days "
                                   f"(threshold: {config.stale_after_days}d) with <{config.minimum_invocations} invocations",
                            utility=utility,
                        )

        elif skill.state == SkillState.STALE:
            # Stale → Active (recovered)?
            if has_sufficient_data and utility.score >= config.utility_stale_threshold:
                return CuratorAction(
                    skill_id=skill.skill_id,
                    skill_name=skill.name,
                    action="restore",
                    reason=f"utility {utility.score:.3f} recovered above stale threshold",
                    utility=utility,
                )

            # Stale → Archived?
            if has_sufficient_data:
                if utility.score < config.utility_archive_threshold:
                    return CuratorAction(
                        skill_id=skill.skill_id,
                        skill_name=skill.name,
                        action="archive",
                        reason=f"utility {utility.score:.3f} < archive threshold {config.utility_archive_threshold}",
                        utility=utility,
                    )
            else:
                # Age-based archive
                if utility.days_since_last_use is not None:
                    if utility.days_since_last_use > config.archive_after_days:
                        return CuratorAction(
                            skill_id=skill.skill_id,
                            skill_name=skill.name,
                            action="archive",
                            reason=f"stale and unused for {utility.days_since_last_use:.0f} days "
                                   f"(threshold: {config.archive_after_days}d)",
                            utility=utility,
                        )

        return None

    def _apply_action(self, action: CuratorAction) -> None:
        """Apply a curator lifecycle action to the registry."""
        if action.action == "mark_stale":
            self._registry.update_skill(action.skill_id, state=SkillState.STALE)
        elif action.action == "archive":
            self._registry.update_skill(action.skill_id, state=SkillState.ARCHIVED)
        elif action.action == "restore":
            self._registry.update_skill(action.skill_id, state=SkillState.ACTIVE)
        elif action.action == "flag_duplicate":
            # Future: mark as duplicate candidate
            LOGGER.info("curator: flagged '%s' as potential duplicate", action.skill_name)
        else:
            raise ValueError(f"unknown curator action: {action.action}")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _action_to_operation_kind(action: str) -> OperationKind | None:
    """Map curator action names to OperationKind for trust evaluation."""
    return {
        "mark_stale": OperationKind.CURATOR_STALE,
        "archive": OperationKind.CURATOR_ARCHIVE,
        "restore": OperationKind.CURATOR_RESTORE,
        "flag_duplicate": OperationKind.CURATOR_FLAG_DUPLICATE,
    }.get(action)


def _success_rate(stats: dict[str, int]) -> float:
    """Compute success rate from stats dict."""
    successful = stats.get("successful", 0)
    failed = stats.get("failed", 0)
    total = successful + failed
    if total == 0:
        return 0.5
    return successful / total
