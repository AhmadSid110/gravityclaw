"""Skill system data models — typed records for registry, proposals, and telemetry."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SkillOwner(StrEnum):
    USER = "user"        # manually created — never autonomously mutated
    AGENT = "agent"      # learned by GravityClaw — eligible for autonomous management
    BUNDLED = "bundled"  # shipped with GravityClaw — immutable from learning engine


class SkillState(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    ARCHIVED = "archived"


class SkillTrust(StrEnum):
    UNREVIEWED = "unreviewed"
    APPROVED = "approved"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CONFLICT = "conflict"  # base revision mismatch


@dataclass(frozen=True, slots=True)
class SkillRecord:
    """Authoritative metadata for a registered skill.

    The filesystem (skills/<name>/SKILL.md) remains the authoritative content layer.
    SQLite indexes metadata, not content.
    """
    skill_id: str
    name: str
    description: str
    path: str  # Relative path from GRAVITYCLAW_HOME (e.g., "skills/modal-deployment")

    owner: str   # SkillOwner value
    state: str   # SkillState value
    trust: str   # SkillTrust value

    revision: int
    pinned: bool

    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class SkillRevisionRecord:
    """A single revision in a skill's history."""
    id: str
    skill_id: str
    revision: int
    parent_revision: int | None
    operation: str  # "create" | "patch" | "rollback"
    source_run_id: str | None
    proposal_id: str | None
    model: str | None
    reason: str
    created_at: str


@dataclass(frozen=True, slots=True)
class SkillProposal:
    """A proposed skill mutation awaiting approval.

    Contains enough information to detect stale base revisions and
    produce a meaningful diff for human review.
    """
    id: str
    skill_id: str | None        # None for create proposals
    skill_name: str
    operation: str               # "create" | "patch" | "archive"
    description: str
    reason: str
    confidence: float

    # Content payload
    content: str                 # New SKILL.md content (full for create, new for patch)
    before: str | None           # Previous SKILL.md content (None for create)
    base_revision: int | None    # Revision the patch is based on (None for create)

    # Provenance
    source_run_id: str | None
    review_model: str | None

    status: str  # ProposalStatus value
    status_reason: str | None

    created_at: str
    resolved_at: str | None


@dataclass(frozen=True, slots=True)
class SkillUsageEvent:
    """A single telemetry event for skill usage tracking."""
    id: str
    skill_id: str
    run_id: str | None
    event: str  # "discovered" | "selected" | "loaded" | "executed" | "successful" | "failed" | "corrected"
    created_at: str


# ─────────────────────────────────────────────────────────────────────────────
# Typed operations from the reviewer (extends Phase 1 schema)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SkillOperation:
    """A skill operation proposed by the auxiliary reviewer.

    This extends the Phase-1 reviewer contract with a structured skill
    mutation proposal.
    """
    operation: str       # "create" | "patch" | "archive"
    name: str            # Skill name (kebab-case identifier)
    description: str     # One-line description of the skill
    content: str         # Full SKILL.md content for create; new section/patch for patch
    reason: str          # Why this operation is proposed
    confidence: float    # 0.0–1.0
    skill_id: str | None = None  # For patch/archive: existing skill id
