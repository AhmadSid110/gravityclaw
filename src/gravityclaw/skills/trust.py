"""Trust / Autonomy Engine — centralized policy for all autonomous operations.

Defines three operating modes (STRICT, BALANCED, AUTONOMOUS) and a
central TrustPolicy that returns ALLOW / REQUIRE_APPROVAL / DENY for
any proposed operation.

Designed to govern not just skill mutations but any autonomous decision:
scheduler creation, external integrations, tool installation, config changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .models import SkillOwner, SkillState

LOGGER = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Core types
# ─────────────────────────────────────────────────────────────────────────────


class TrustMode(StrEnum):
    """Operating mode for the autonomy engine."""
    STRICT = "strict"       # Everything requires approval except memory writes
    BALANCED = "balanced"   # Approved agent-skill patches auto; new skills need approval
    AUTONOMOUS = "autonomous"  # Agent-owned skills fully autonomous; user/bundled protected


class TrustDecision(StrEnum):
    """Outcome of a trust policy evaluation."""
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class OperationKind(StrEnum):
    """Kinds of operations subject to trust evaluation."""
    # Memory
    MEMORY_WRITE = "memory_write"

    # Skills
    SKILL_PROPOSAL = "skill_proposal"
    SKILL_CREATE = "skill_create"
    SKILL_PATCH = "skill_patch"
    SKILL_ARCHIVE = "skill_archive"
    SKILL_RESTORE = "skill_restore"

    # Curator lifecycle
    CURATOR_STALE = "curator_stale"
    CURATOR_ARCHIVE = "curator_archive"
    CURATOR_RESTORE = "curator_restore"
    CURATOR_FLAG_DUPLICATE = "curator_flag_duplicate"

    # Future: extensible
    SCHEDULER_CREATE = "scheduler_create"
    TOOL_INSTALL = "tool_install"
    CONFIG_CHANGE = "config_change"
    EXTERNAL_INTEGRATION = "external_integration"


@dataclass(frozen=True, slots=True)
class OperationContext:
    """Context passed to the trust policy for evaluation.

    Carries enough information for the policy to make a decision without
    needing to query external state.
    """
    kind: OperationKind | str
    skill_owner: str | None = None       # SkillOwner value for skill operations
    skill_state: str | None = None       # SkillState value
    skill_pinned: bool = False           # Whether the skill is pinned
    confidence: float = 0.0             # Reviewer confidence (0.0–1.0)
    is_correction: bool = False          # Whether this is a post-correction improvement
    provenance: str | None = None        # Source of the operation
    extra: dict[str, Any] | None = None  # Additional context for extensibility


@dataclass(frozen=True, slots=True)
class PolicyResult:
    """Full result from a trust policy evaluation."""
    decision: TrustDecision
    reason: str
    mode: TrustMode


# ─────────────────────────────────────────────────────────────────────────────
# Mode-specific rule tables
# ─────────────────────────────────────────────────────────────────────────────

# The rule tables define what each mode allows for each operation kind.
# Structure: (OperationKind, optional_condition) -> TrustDecision

# STRICT: approval required for all skill mutations
_STRICT_RULES: dict[str, TrustDecision] = {
    OperationKind.MEMORY_WRITE: TrustDecision.ALLOW,
    OperationKind.SKILL_PROPOSAL: TrustDecision.ALLOW,
    OperationKind.SKILL_CREATE: TrustDecision.REQUIRE_APPROVAL,
    OperationKind.SKILL_PATCH: TrustDecision.REQUIRE_APPROVAL,
    OperationKind.SKILL_ARCHIVE: TrustDecision.REQUIRE_APPROVAL,
    OperationKind.SKILL_RESTORE: TrustDecision.REQUIRE_APPROVAL,
    OperationKind.CURATOR_STALE: TrustDecision.REQUIRE_APPROVAL,
    OperationKind.CURATOR_ARCHIVE: TrustDecision.REQUIRE_APPROVAL,
    OperationKind.CURATOR_RESTORE: TrustDecision.REQUIRE_APPROVAL,
    OperationKind.CURATOR_FLAG_DUPLICATE: TrustDecision.REQUIRE_APPROVAL,
    OperationKind.SCHEDULER_CREATE: TrustDecision.REQUIRE_APPROVAL,
    OperationKind.TOOL_INSTALL: TrustDecision.REQUIRE_APPROVAL,
    OperationKind.CONFIG_CHANGE: TrustDecision.REQUIRE_APPROVAL,
    OperationKind.EXTERNAL_INTEGRATION: TrustDecision.REQUIRE_APPROVAL,
}

# BALANCED: agent-owned skill patches and curator lifecycle auto-managed
_BALANCED_RULES: dict[str, TrustDecision] = {
    OperationKind.MEMORY_WRITE: TrustDecision.ALLOW,
    OperationKind.SKILL_PROPOSAL: TrustDecision.ALLOW,
    OperationKind.SKILL_CREATE: TrustDecision.REQUIRE_APPROVAL,
    OperationKind.SKILL_PATCH: TrustDecision.ALLOW,  # Agent-owned only; user/bundled overridden below
    OperationKind.SKILL_ARCHIVE: TrustDecision.REQUIRE_APPROVAL,
    OperationKind.SKILL_RESTORE: TrustDecision.ALLOW,
    OperationKind.CURATOR_STALE: TrustDecision.ALLOW,
    OperationKind.CURATOR_ARCHIVE: TrustDecision.ALLOW,
    OperationKind.CURATOR_RESTORE: TrustDecision.ALLOW,
    OperationKind.CURATOR_FLAG_DUPLICATE: TrustDecision.ALLOW,
    OperationKind.SCHEDULER_CREATE: TrustDecision.REQUIRE_APPROVAL,
    OperationKind.TOOL_INSTALL: TrustDecision.REQUIRE_APPROVAL,
    OperationKind.CONFIG_CHANGE: TrustDecision.REQUIRE_APPROVAL,
    OperationKind.EXTERNAL_INTEGRATION: TrustDecision.REQUIRE_APPROVAL,
}

# AUTONOMOUS: agent-owned skills fully managed; user/bundled still protected
_AUTONOMOUS_RULES: dict[str, TrustDecision] = {
    OperationKind.MEMORY_WRITE: TrustDecision.ALLOW,
    OperationKind.SKILL_PROPOSAL: TrustDecision.ALLOW,
    OperationKind.SKILL_CREATE: TrustDecision.ALLOW,
    OperationKind.SKILL_PATCH: TrustDecision.ALLOW,
    OperationKind.SKILL_ARCHIVE: TrustDecision.ALLOW,
    OperationKind.SKILL_RESTORE: TrustDecision.ALLOW,
    OperationKind.CURATOR_STALE: TrustDecision.ALLOW,
    OperationKind.CURATOR_ARCHIVE: TrustDecision.ALLOW,
    OperationKind.CURATOR_RESTORE: TrustDecision.ALLOW,
    OperationKind.CURATOR_FLAG_DUPLICATE: TrustDecision.ALLOW,
    OperationKind.SCHEDULER_CREATE: TrustDecision.ALLOW,
    OperationKind.TOOL_INSTALL: TrustDecision.REQUIRE_APPROVAL,
    OperationKind.CONFIG_CHANGE: TrustDecision.REQUIRE_APPROVAL,
    OperationKind.EXTERNAL_INTEGRATION: TrustDecision.REQUIRE_APPROVAL,
}

_MODE_RULES: dict[TrustMode, dict[str, TrustDecision]] = {
    TrustMode.STRICT: _STRICT_RULES,
    TrustMode.BALANCED: _BALANCED_RULES,
    TrustMode.AUTONOMOUS: _AUTONOMOUS_RULES,
}


# ─────────────────────────────────────────────────────────────────────────────
# TrustPolicy — the single evaluation entry point
# ─────────────────────────────────────────────────────────────────────────────


class TrustPolicy:
    """Central policy engine for all autonomous operations.

    Usage:
        policy = TrustPolicy(mode=TrustMode.BALANCED)
        result = policy.evaluate(OperationContext(
            kind=OperationKind.SKILL_PATCH,
            skill_owner=SkillOwner.AGENT,
            confidence=0.85,
        ))
        if result.decision == TrustDecision.ALLOW:
            # proceed
        elif result.decision == TrustDecision.REQUIRE_APPROVAL:
            # queue for user approval
        elif result.decision == TrustDecision.DENY:
            # reject
    """

    def __init__(self, mode: TrustMode = TrustMode.STRICT) -> None:
        self._mode = mode

    @property
    def mode(self) -> TrustMode:
        return self._mode

    @mode.setter
    def mode(self, value: TrustMode) -> None:
        self._mode = value

    def evaluate(self, ctx: OperationContext) -> PolicyResult:
        """Evaluate a proposed operation against the current trust policy.

        Hard rules (cannot be overridden by mode):
        - User-owned skills: DENY all autonomous mutations (patch/archive/restore).
        - Bundled skills: DENY all mutations.
        - Pinned skills: DENY curator archive/stale.

        Soft rules (mode-dependent):
        - Everything else follows the mode's rule table.
        """
        # ─── Hard rules (ownership-based, mode-independent) ───────────
        decision = self._check_hard_rules(ctx)
        if decision is not None:
            return decision

        # ─── Mode-based rules ─────────────────────────────────────────
        rules = _MODE_RULES.get(self._mode, _STRICT_RULES)
        kind_key = ctx.kind if isinstance(ctx.kind, str) else ctx.kind.value

        base_decision = rules.get(kind_key, TrustDecision.REQUIRE_APPROVAL)

        # ─── Confidence gate: low-confidence proposals need approval ──
        if base_decision == TrustDecision.ALLOW and self._needs_confidence_gate(ctx):
            if ctx.confidence < 0.7:
                return PolicyResult(
                    decision=TrustDecision.REQUIRE_APPROVAL,
                    reason=f"confidence {ctx.confidence:.2f} below threshold 0.7",
                    mode=self._mode,
                )

        return PolicyResult(
            decision=base_decision,
            reason=f"mode={self._mode.value} rule for {kind_key}",
            mode=self._mode,
        )

    def _check_hard_rules(self, ctx: OperationContext) -> PolicyResult | None:
        """Check ownership-based hard rules that override mode.

        Returns PolicyResult if a hard rule applies, None otherwise.
        """
        # Skill mutation operations
        skill_mutations = {
            OperationKind.SKILL_PATCH,
            OperationKind.SKILL_ARCHIVE,
            OperationKind.SKILL_RESTORE,
            OperationKind.SKILL_CREATE,
            OperationKind.CURATOR_STALE,
            OperationKind.CURATOR_ARCHIVE,
            OperationKind.CURATOR_RESTORE,
        }
        kind_str = ctx.kind if isinstance(ctx.kind, str) else ctx.kind.value

        if kind_str in {k.value for k in skill_mutations}:
            # User-owned: DENY all mutations regardless of mode
            if ctx.skill_owner == SkillOwner.USER:
                # Exception: SKILL_PROPOSAL is always allowed (proposing is safe)
                if kind_str == OperationKind.SKILL_CREATE:
                    # Creating is never about user-owned (it's a new skill)
                    pass
                else:
                    return PolicyResult(
                        decision=TrustDecision.DENY,
                        reason="user-owned skills cannot be autonomously mutated",
                        mode=self._mode,
                    )

            # Bundled: DENY all mutations regardless of mode
            if ctx.skill_owner == SkillOwner.BUNDLED:
                return PolicyResult(
                    decision=TrustDecision.DENY,
                    reason="bundled skills are immutable",
                    mode=self._mode,
                )

            # Pinned: DENY curator stale/archive
            if ctx.skill_pinned and kind_str in (
                OperationKind.CURATOR_STALE.value,
                OperationKind.CURATOR_ARCHIVE.value,
            ):
                return PolicyResult(
                    decision=TrustDecision.DENY,
                    reason="pinned skills cannot be staled or archived",
                    mode=self._mode,
                )

        return None

    def _needs_confidence_gate(self, ctx: OperationContext) -> bool:
        """Determine whether the operation should be subject to confidence gating."""
        gated_kinds = {
            OperationKind.SKILL_CREATE.value,
            OperationKind.SKILL_PATCH.value,
            OperationKind.SKILL_ARCHIVE.value,
        }
        kind_str = ctx.kind if isinstance(ctx.kind, str) else ctx.kind.value
        return kind_str in gated_kinds

    def can_auto_apply(self, ctx: OperationContext) -> bool:
        """Convenience: returns True if evaluate() would return ALLOW."""
        return self.evaluate(ctx).decision == TrustDecision.ALLOW

    def requires_approval(self, ctx: OperationContext) -> bool:
        """Convenience: returns True if evaluate() would return REQUIRE_APPROVAL."""
        return self.evaluate(ctx).decision == TrustDecision.REQUIRE_APPROVAL
