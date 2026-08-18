"""Unified Learning Mode configuration tree — Phase 3.1.

All learning subsystem configuration lives under a single [learning] section
in gravityclaw.toml, with consistent GRAVITYCLAW_LEARNING_* environment
variable overrides.

Configuration hierarchy:
    [learning]                          → top-level enable/mode
    [learning.reviewer]                 → LLM reviewer settings
    [learning.skills]                   → trust/confidence policy
    [learning.ingestion]                → source processing limits
    [learning.curator]                  → lifecycle automation
    [learning.notifications]            → notification preferences

Environment overrides follow GRAVITYCLAW_LEARNING_{SECTION}_{KEY} naming.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Sub-section configs
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReviewerConfig:
    """[learning.reviewer] — auxiliary reviewer LLM settings."""
    enabled: bool = True
    provider: str = "google"
    model: str = "gemini-2.0-flash"
    fallback_to_primary: bool = False
    max_input_tokens: int = 12_000
    max_output_tokens: int = 1_200
    max_retries: int = 2


@dataclass(frozen=True, slots=True)
class SkillsConfig:
    """[learning.skills] — trust/confidence policy settings."""
    trust_mode: str = "strict"  # "strict" | "balanced" | "autonomous"
    min_confidence: float = 0.80
    create_approval_required: bool = True
    modify_approval_required: bool = True


@dataclass(frozen=True, slots=True)
class IngestionConfig:
    """[learning.ingestion] — source processing limits."""
    small_source_token_limit: int = 20_000
    chunk_tokens: int = 12_000
    max_chunks: int = 100


@dataclass(frozen=True, slots=True)
class CuratorScheduleConfig:
    """[learning.curator] — lifecycle automation settings."""
    enabled: bool = True
    schedule: str = "0 4 * * 0"       # cron expression (default: weekly Sun 04:00)
    timezone: str = "UTC"
    min_idle_hours: int = 2
    stale_after_days: int = 30
    archive_after_days: int = 90
    minimum_invocations: int = 2
    utility_stale_threshold: float = 0.3
    utility_archive_threshold: float = 0.1


@dataclass(frozen=True, slots=True)
class NotificationsConfig:
    """[learning.notifications] — notification preferences."""
    mode: str = "normal"  # "silent" | "normal" | "verbose"


# ─────────────────────────────────────────────────────────────────────────────
# Top-level unified config
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LearningConfig:
    """Complete learning mode configuration, parsed from [learning] in gravityclaw.toml.

    Usage:
        config = LearningConfig.from_toml(toml_dict.get("learning", {}))
        # or with environment overrides:
        config = LearningConfig.from_environment(toml_dict.get("learning", {}))
    """
    enabled: bool = True
    memory_approval_required: bool = False

    # Sub-sections
    reviewer: ReviewerConfig = field(default_factory=ReviewerConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    ingestion: IngestionConfig = field(default_factory=IngestionConfig)
    curator: CuratorScheduleConfig = field(default_factory=CuratorScheduleConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)

    @classmethod
    def from_toml(cls, section: dict[str, Any] | None) -> "LearningConfig":
        """Parse from the [learning] TOML section (no env overrides)."""
        if not section or not isinstance(section, dict):
            return cls()

        reviewer_raw = section.get("reviewer", {})
        skills_raw = section.get("skills", {})
        ingestion_raw = section.get("ingestion", {})
        curator_raw = section.get("curator", {})
        notifications_raw = section.get("notifications", {})

        return cls(
            enabled=bool(section.get("enabled", True)),
            memory_approval_required=bool(section.get("memory_approval_required", False)),
            reviewer=_parse_reviewer(reviewer_raw),
            skills=_parse_skills(skills_raw),
            ingestion=_parse_ingestion(ingestion_raw),
            curator=_parse_curator(curator_raw),
            notifications=_parse_notifications(notifications_raw),
        )

    @classmethod
    def from_environment(cls, toml_section: dict[str, Any] | None = None) -> "LearningConfig":
        """Parse from TOML section + environment variable overrides.

        Environment variables follow the pattern:
            GRAVITYCLAW_LEARNING_ENABLED
            GRAVITYCLAW_LEARNING_REVIEWER_MODEL
            GRAVITYCLAW_LEARNING_SKILLS_TRUST_MODE
            GRAVITYCLAW_LEARNING_INGESTION_CHUNK_TOKENS
            GRAVITYCLAW_LEARNING_CURATOR_ENABLED
            GRAVITYCLAW_LEARNING_CURATOR_SCHEDULE
            GRAVITYCLAW_LEARNING_NOTIFICATIONS_MODE
        """
        base = cls.from_toml(toml_section)

        # Top-level overrides
        enabled = _env_bool("GRAVITYCLAW_LEARNING_ENABLED", base.enabled)
        memory_approval = _env_bool(
            "GRAVITYCLAW_LEARNING_MEMORY_APPROVAL_REQUIRED",
            base.memory_approval_required,
        )

        # Reviewer overrides
        reviewer = ReviewerConfig(
            enabled=_env_bool("GRAVITYCLAW_LEARNING_REVIEWER_ENABLED", base.reviewer.enabled),
            provider=_env_str("GRAVITYCLAW_LEARNING_REVIEWER_PROVIDER", base.reviewer.provider),
            model=_env_str("GRAVITYCLAW_LEARNING_REVIEWER_MODEL", base.reviewer.model),
            fallback_to_primary=_env_bool(
                "GRAVITYCLAW_LEARNING_REVIEWER_FALLBACK_TO_PRIMARY",
                base.reviewer.fallback_to_primary,
            ),
            max_input_tokens=_env_int(
                "GRAVITYCLAW_LEARNING_REVIEWER_MAX_INPUT_TOKENS",
                base.reviewer.max_input_tokens,
            ),
            max_output_tokens=_env_int(
                "GRAVITYCLAW_LEARNING_REVIEWER_MAX_OUTPUT_TOKENS",
                base.reviewer.max_output_tokens,
            ),
            max_retries=_env_int(
                "GRAVITYCLAW_LEARNING_REVIEWER_MAX_RETRIES",
                base.reviewer.max_retries,
            ),
        )

        # Skills overrides
        skills = SkillsConfig(
            trust_mode=_env_str("GRAVITYCLAW_LEARNING_SKILLS_TRUST_MODE", base.skills.trust_mode),
            min_confidence=_env_float(
                "GRAVITYCLAW_LEARNING_SKILLS_MIN_CONFIDENCE",
                base.skills.min_confidence,
            ),
            create_approval_required=_env_bool(
                "GRAVITYCLAW_LEARNING_SKILLS_CREATE_APPROVAL_REQUIRED",
                base.skills.create_approval_required,
            ),
            modify_approval_required=_env_bool(
                "GRAVITYCLAW_LEARNING_SKILLS_MODIFY_APPROVAL_REQUIRED",
                base.skills.modify_approval_required,
            ),
        )

        # Ingestion overrides
        ingestion = IngestionConfig(
            small_source_token_limit=_env_int(
                "GRAVITYCLAW_LEARNING_INGESTION_SMALL_SOURCE_TOKEN_LIMIT",
                base.ingestion.small_source_token_limit,
            ),
            chunk_tokens=_env_int(
                "GRAVITYCLAW_LEARNING_INGESTION_CHUNK_TOKENS",
                base.ingestion.chunk_tokens,
            ),
            max_chunks=_env_int(
                "GRAVITYCLAW_LEARNING_INGESTION_MAX_CHUNKS",
                base.ingestion.max_chunks,
            ),
        )

        # Curator overrides
        curator = CuratorScheduleConfig(
            enabled=_env_bool("GRAVITYCLAW_LEARNING_CURATOR_ENABLED", base.curator.enabled),
            schedule=_env_str("GRAVITYCLAW_LEARNING_CURATOR_SCHEDULE", base.curator.schedule),
            timezone=_env_str("GRAVITYCLAW_LEARNING_CURATOR_TIMEZONE", base.curator.timezone),
            min_idle_hours=_env_int(
                "GRAVITYCLAW_LEARNING_CURATOR_MIN_IDLE_HOURS",
                base.curator.min_idle_hours,
            ),
            stale_after_days=_env_int(
                "GRAVITYCLAW_LEARNING_CURATOR_STALE_AFTER_DAYS",
                base.curator.stale_after_days,
            ),
            archive_after_days=_env_int(
                "GRAVITYCLAW_LEARNING_CURATOR_ARCHIVE_AFTER_DAYS",
                base.curator.archive_after_days,
            ),
            minimum_invocations=_env_int(
                "GRAVITYCLAW_LEARNING_CURATOR_MINIMUM_INVOCATIONS",
                base.curator.minimum_invocations,
            ),
            utility_stale_threshold=_env_float(
                "GRAVITYCLAW_LEARNING_CURATOR_UTILITY_STALE_THRESHOLD",
                base.curator.utility_stale_threshold,
            ),
            utility_archive_threshold=_env_float(
                "GRAVITYCLAW_LEARNING_CURATOR_UTILITY_ARCHIVE_THRESHOLD",
                base.curator.utility_archive_threshold,
            ),
        )

        # Notifications overrides
        notifications = NotificationsConfig(
            mode=_env_str("GRAVITYCLAW_LEARNING_NOTIFICATIONS_MODE", base.notifications.mode),
        )

        return cls(
            enabled=enabled,
            memory_approval_required=memory_approval,
            reviewer=reviewer,
            skills=skills,
            ingestion=ingestion,
            curator=curator,
            notifications=notifications,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dictionary suitable for API responses or TOML output."""
        return {
            "enabled": self.enabled,
            "memory_approval_required": self.memory_approval_required,
            "reviewer": {
                "enabled": self.reviewer.enabled,
                "provider": self.reviewer.provider,
                "model": self.reviewer.model,
                "fallback_to_primary": self.reviewer.fallback_to_primary,
                "max_input_tokens": self.reviewer.max_input_tokens,
                "max_output_tokens": self.reviewer.max_output_tokens,
                "max_retries": self.reviewer.max_retries,
            },
            "skills": {
                "trust_mode": self.skills.trust_mode,
                "min_confidence": self.skills.min_confidence,
                "create_approval_required": self.skills.create_approval_required,
                "modify_approval_required": self.skills.modify_approval_required,
            },
            "ingestion": {
                "small_source_token_limit": self.ingestion.small_source_token_limit,
                "chunk_tokens": self.ingestion.chunk_tokens,
                "max_chunks": self.ingestion.max_chunks,
            },
            "curator": {
                "enabled": self.curator.enabled,
                "schedule": self.curator.schedule,
                "timezone": self.curator.timezone,
                "min_idle_hours": self.curator.min_idle_hours,
                "stale_after_days": self.curator.stale_after_days,
                "archive_after_days": self.curator.archive_after_days,
                "minimum_invocations": self.curator.minimum_invocations,
                "utility_stale_threshold": self.curator.utility_stale_threshold,
                "utility_archive_threshold": self.curator.utility_archive_threshold,
            },
            "notifications": {
                "mode": self.notifications.mode,
            },
        }


# ─────────────────────────────────────────────────────────────────────────────
# Internal parsers
# ─────────────────────────────────────────────────────────────────────────────


def _parse_reviewer(raw: Any) -> ReviewerConfig:
    if not isinstance(raw, dict):
        return ReviewerConfig()
    return ReviewerConfig(
        enabled=bool(raw.get("enabled", True)),
        provider=str(raw.get("provider", "google")),
        model=str(raw.get("model", "gemini-2.0-flash")),
        fallback_to_primary=bool(raw.get("fallback_to_primary", False)),
        max_input_tokens=int(raw.get("max_input_tokens", 12_000)),
        max_output_tokens=int(raw.get("max_output_tokens", 1_200)),
        max_retries=int(raw.get("max_retries", 2)),
    )


def _parse_skills(raw: Any) -> SkillsConfig:
    if not isinstance(raw, dict):
        return SkillsConfig()
    return SkillsConfig(
        trust_mode=str(raw.get("trust_mode", "strict")),
        min_confidence=float(raw.get("min_confidence", 0.80)),
        create_approval_required=bool(raw.get("create_approval_required", True)),
        modify_approval_required=bool(raw.get("modify_approval_required", True)),
    )


def _parse_ingestion(raw: Any) -> IngestionConfig:
    if not isinstance(raw, dict):
        return IngestionConfig()
    return IngestionConfig(
        small_source_token_limit=int(raw.get("small_source_token_limit", 20_000)),
        chunk_tokens=int(raw.get("chunk_tokens", 12_000)),
        max_chunks=int(raw.get("max_chunks", 100)),
    )


def _parse_curator(raw: Any) -> CuratorScheduleConfig:
    if not isinstance(raw, dict):
        return CuratorScheduleConfig()
    return CuratorScheduleConfig(
        enabled=bool(raw.get("enabled", True)),
        schedule=str(raw.get("schedule", "0 4 * * 0")),
        timezone=str(raw.get("timezone", "UTC")),
        min_idle_hours=int(raw.get("min_idle_hours", 2)),
        stale_after_days=int(raw.get("stale_after_days", 30)),
        archive_after_days=int(raw.get("archive_after_days", 90)),
        minimum_invocations=int(raw.get("minimum_invocations", 2)),
        utility_stale_threshold=float(raw.get("utility_stale_threshold", 0.3)),
        utility_archive_threshold=float(raw.get("utility_archive_threshold", 0.1)),
    )


def _parse_notifications(raw: Any) -> NotificationsConfig:
    if not isinstance(raw, dict):
        return NotificationsConfig()
    return NotificationsConfig(
        mode=str(raw.get("mode", "normal")),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Environment helpers
# ─────────────────────────────────────────────────────────────────────────────


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.lower() in {"1", "true", "yes"}


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        return default
