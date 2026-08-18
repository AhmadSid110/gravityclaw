"""Surface-agnostic /learn command — Phase 3.1.

Provides a single entry point for learning from any surface:
    Web:       POST /api/learning/learn
    Telegram:  /learn https://...
    CLI:       gravityclaw learn https://...
    Internal:  LearnService.learn(request)

All surfaces resolve to:
    Channel adapter → LearnRequest → LearnService.learn() → IngestionEngine → SkillProposal → TrustPolicy

The LearnService is the single coordination point. Ingestion logic stays
in skills/ingestion.py; this module handles request normalization, progress
tracking, and result formatting.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .learning_config import LearningConfig
from .skills.ingestion import IngestionEngine, LearnResult
from .skills.trust import TrustDecision, TrustPolicy, OperationContext, OperationKind
from .store import Store

LOGGER = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Request / options models
# ─────────────────────────────────────────────────────────────────────────────


class LearnChannel(StrEnum):
    """Channel from which a /learn request originated."""
    WEB = "web"
    TELEGRAM = "telegram"
    CLI = "cli"
    API = "api"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class LearnOptions:
    """Options that modify /learn behavior."""
    skill_name: str | None = None       # User-specified skill name
    force_new: bool = False             # Force create even if similar exists
    trust_override: str | None = None   # Temporary trust mode override for this request
    title_hint: str | None = None       # Suggested title for the skill


@dataclass(frozen=True, slots=True)
class LearnRequest:
    """Normalized /learn request from any surface.

    All channel adapters produce this same structure.
    """
    source: str                          # URL, file path, "this conversation", or text
    requested_by: str                    # Actor identifier
    channel: LearnChannel                # Originating surface
    conversation_id: str | None = None   # Active conversation (for "this conversation")
    run_id: str | None = None            # Source run if triggered from a run
    options: LearnOptions = field(default_factory=LearnOptions)
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    requested_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


# ─────────────────────────────────────────────────────────────────────────────
# Response model
# ─────────────────────────────────────────────────────────────────────────────


class LearnStatus(StrEnum):
    """Outcome status for a /learn request."""
    SUCCESS = "success"
    DUPLICATE = "duplicate"
    FAILED = "failed"
    PENDING_APPROVAL = "pending_approval"
    DISABLED = "disabled"


@dataclass(slots=True)
class LearnResponse:
    """Unified response from LearnService.learn()."""
    request_id: str
    status: LearnStatus
    message: str

    # Detail fields (populated on success)
    proposal_id: str | None = None
    skill_name: str | None = None
    source_type: str | None = None
    chunks_processed: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "request_id": self.request_id,
            "status": self.status.value,
            "message": self.message,
        }
        if self.proposal_id:
            result["proposal_id"] = self.proposal_id
        if self.skill_name:
            result["skill_name"] = self.skill_name
        if self.source_type:
            result["source_type"] = self.source_type
        if self.chunks_processed:
            result["chunks_processed"] = self.chunks_processed
        if self.warnings:
            result["warnings"] = self.warnings
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Service
# ─────────────────────────────────────────────────────────────────────────────


class LearnService:
    """Surface-agnostic /learn command coordinator.

    Wraps IngestionEngine with:
    - Request validation and normalization
    - Configuration checks (learning enabled? trust mode?)
    - Result formatting
    - Audit trail recording
    """

    def __init__(
        self,
        ingestion_engine: IngestionEngine,
        trust_policy: TrustPolicy,
        store: Store,
        config: LearningConfig,
    ) -> None:
        self._ingestion = ingestion_engine
        self._trust_policy = trust_policy
        self._store = store
        self._config = config

    @property
    def config(self) -> LearningConfig:
        return self._config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def learn(self, request: LearnRequest) -> LearnResponse:
        """Process a /learn request from any surface.

        This is THE entry point. All surfaces call this same method.

        Pipeline:
            1. Check learning is enabled
            2. Validate request
            3. Resolve source content
            4. Delegate to IngestionEngine.ingest()
            5. Check trust policy for the resulting proposal
            6. Record audit event
            7. Return formatted response
        """
        # 1. Check enabled
        if not self._config.enabled:
            return LearnResponse(
                request_id=request.request_id,
                status=LearnStatus.DISABLED,
                message="Learning mode is disabled.",
            )

        # 2. Validate
        if not request.source or not request.source.strip():
            return LearnResponse(
                request_id=request.request_id,
                status=LearnStatus.FAILED,
                message="No source provided.",
            )

        # 3-4. Delegate to ingestion engine
        try:
            result = self._ingestion.ingest(
                request.source.strip(),
                source_run_id=request.run_id,
                title_hint=request.options.title_hint or request.options.skill_name,
            )
        except Exception as exc:
            LOGGER.error("learn ingestion failed: %s", exc, exc_info=True)
            return LearnResponse(
                request_id=request.request_id,
                status=LearnStatus.FAILED,
                message=f"Ingestion failed: {exc}",
            )

        # 5. Check result
        if result.warnings and not result.proposal_id:
            # Ingestion produced warnings but no proposal
            if any("duplicate" in w.lower() for w in result.warnings):
                return LearnResponse(
                    request_id=request.request_id,
                    status=LearnStatus.DUPLICATE,
                    message="Source has already been learned.",
                    skill_name=result.proposed_skill_name or None,
                    source_type=result.source_type.value if result.source_type else None,
                    warnings=result.warnings,
                )
            return LearnResponse(
                request_id=request.request_id,
                status=LearnStatus.FAILED,
                message=result.warnings[0] if result.warnings else "Ingestion produced no result.",
                warnings=result.warnings,
            )

        # 6. Record audit event
        self._record_audit(request, result)

        # 7. Format response
        return LearnResponse(
            request_id=request.request_id,
            status=LearnStatus.SUCCESS,
            message=f"Learned '{result.proposed_skill_name}' — proposal created.",
            proposal_id=result.proposal_id,
            skill_name=result.proposed_skill_name,
            source_type=result.source_type.value if result.source_type else None,
            chunks_processed=result.chunks_processed,
            warnings=result.warnings,
        )

    def _record_audit(self, request: LearnRequest, result: LearnResult) -> None:
        """Record the /learn event for audit trail."""
        try:
            self._store.record_audit(
                actor=request.requested_by,
                action="learning.learn",
                resource_type="skill_proposal",
                resource_id=result.proposal_id or "",
                payload={
                    "source": request.source[:200],
                    "channel": request.channel.value,
                    "skill_name": result.proposed_skill_name,
                    "source_type": result.source_type.value if result.source_type else None,
                    "dedup_result": result.dedup_result.value if result.dedup_result else None,
                    "request_id": request.request_id,
                },
            )
        except Exception as exc:
            LOGGER.warning("failed to record /learn audit: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Parsing helper — extract LearnRequest from command text
# ─────────────────────────────────────────────────────────────────────────────


def parse_learn_command(
    text: str,
    *,
    channel: LearnChannel = LearnChannel.INTERNAL,
    requested_by: str = "user",
    conversation_id: str | None = None,
    run_id: str | None = None,
) -> LearnRequest:
    """Parse a /learn command string into a LearnRequest.

    Supports:
        /learn https://example.com
        /learn ./path/to/file --name my-skill
        /learn this conversation
        /learn "some raw text to learn from"

    Args:
        text: The full command text (with or without /learn prefix).
        channel: Originating channel.
        requested_by: Actor identifier.
        conversation_id: Active conversation ID.
        run_id: Source run ID if applicable.

    Returns:
        Normalized LearnRequest.
    """
    # Strip /learn prefix if present
    stripped = text.strip()
    if stripped.lower().startswith("/learn"):
        stripped = stripped[6:].strip()
    elif stripped.lower().startswith("learn"):
        stripped = stripped[5:].strip()

    # Parse options
    parts = stripped.split()
    source_parts: list[str] = []
    options_dict: dict[str, Any] = {}
    i = 0
    while i < len(parts):
        part = parts[i]
        if part == "--name" and i + 1 < len(parts):
            options_dict["skill_name"] = parts[i + 1]
            i += 2
        elif part == "--force-new":
            options_dict["force_new"] = True
            i += 1
        elif part == "--trust" and i + 1 < len(parts):
            options_dict["trust_override"] = parts[i + 1]
            i += 1
        else:
            source_parts.append(part)
            i += 1

    source = " ".join(source_parts)

    return LearnRequest(
        source=source,
        requested_by=requested_by,
        channel=channel,
        conversation_id=conversation_id,
        run_id=run_id,
        options=LearnOptions(
            skill_name=options_dict.get("skill_name"),
            force_new=options_dict.get("force_new", False),
            trust_override=options_dict.get("trust_override"),
            title_hint=options_dict.get("skill_name"),
        ),
    )
