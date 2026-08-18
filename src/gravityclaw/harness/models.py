"""Agent Harness data models — Phase 6.

Minimal dataclasses that describe the runtime context for a single model call.
No database required; these are ephemeral per-dispatch structures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .tool_policy import VisibleToolSet


@dataclass(frozen=True, slots=True)
class Capability:
    """A single GravityClaw capability available (or unavailable) for a run."""

    name: str
    description: str
    category: str = "general"
    available: bool = True
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SkillSummary:
    """Metadata-only skill reference injected into the harness prompt.

    Full content stays out of the initial prompt — the model requests it
    via skill_view when needed (progressive disclosure).
    """

    skill_id: str
    name: str
    description: str
    trust: str = "learned"
    relevance_score: float = 0.0


@dataclass(frozen=True, slots=True)
class HarnessContext:
    """Complete context snapshot for one harness compilation.

    Built at dispatch time from live runtime state; never persisted.

    The capabilities tuple is derived from visible_tools (when provided)
    via CapabilityResolver.from_visible_tools(). This enforces the invariant
    that tool capabilities match actual tool availability.
    """

    # Runtime identity
    model: str
    provider: str
    channel: str
    workspace: str
    host_user: str = "ubuntu"
    host_name: str = "GravityClaw VPS"
    sandbox_enabled: bool = False
    execution_target: str = "host"

    # Feature flags
    learning_enabled: bool = True
    trust_mode: str = "strict"

    # Resolved capabilities and skills
    capabilities: tuple[Capability, ...] = ()
    skills: tuple[SkillSummary, ...] = ()

    # The canonical visible tool set (when available).
    # This is the single source of truth — capabilities above are derived from it.
    # Optional for backward compat; new code should always provide it.
    visible_tools: "VisibleToolSet | None" = None

    # Pre-existing context (already compiled by ContextBuilder)
    memory_available: bool = True
    scheduler_available: bool = True
    delegation_available: bool = True

    # Optional adapter hint
    adapter: str = "generic"

    @property
    def available_capabilities(self) -> tuple[Capability, ...]:
        return tuple(c for c in self.capabilities if c.available)

    @property
    def unavailable_capabilities(self) -> tuple[Capability, ...]:
        return tuple(c for c in self.capabilities if not c.available)

    @property
    def capability_categories(self) -> dict[str, list[Capability]]:
        """Group available capabilities by category."""
        groups: dict[str, list[Capability]] = {}
        for cap in self.available_capabilities:
            groups.setdefault(cap.category, []).append(cap)
        return groups
