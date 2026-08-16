"""Backend-neutral events emitted by execution engines."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """A stable GravityClaw event with the original backend payload retained."""

    type: str
    run_id: str
    conversation_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] | None = None
