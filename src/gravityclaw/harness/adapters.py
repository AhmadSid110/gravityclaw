"""Model adapters — tiny provider-specific prompt tweaks.

Architecture:

    GravityClaw Core (95% identical)
        │
        ┌──────────┼───────────┐
        ▼          ▼           ▼
    OpenAI    Anthropic    Gemini
    + small   + small      + small
    adapter   adapter      adapter

Adapters are intentionally minimal. Do NOT maintain three entirely different
system prompts. Only add adapter instructions when real evaluations show a
model needs specific guidance.
"""

from __future__ import annotations

from typing import Protocol

from .models import HarnessContext


class Adapter(Protocol):
    """Provider-specific prompt adapter."""

    @property
    def name(self) -> str: ...

    def instructions(self, ctx: HarnessContext) -> str:
        """Return additional instructions for this provider, or empty string."""
        ...


class GenericAdapter:
    """Default adapter — no additional instructions needed."""

    @property
    def name(self) -> str:
        return "generic"

    def instructions(self, ctx: HarnessContext) -> str:
        return ""


class OpenAIAdapter:
    """Tweaks for OpenAI models (GPT-4o, GPT-5, o-series)."""

    @property
    def name(self) -> str:
        return "openai"

    def instructions(self, ctx: HarnessContext) -> str:
        return ""


class AnthropicAdapter:
    """Tweaks for Anthropic models (Claude)."""

    @property
    def name(self) -> str:
        return "anthropic"

    def instructions(self, ctx: HarnessContext) -> str:
        # Claude sometimes speculates about runtime state rather than checking.
        return (
            "## Provider notes\n\n"
            "Prefer available tools over speculating about runtime state. "
            "When uncertain whether a capability is available, check the "
            "feature list above rather than assuming."
        )


class GeminiAdapter:
    """Tweaks for Google Gemini models."""

    @property
    def name(self) -> str:
        return "gemini"

    def instructions(self, ctx: HarnessContext) -> str:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────


class AdapterRegistry:
    """Maps adapter names to instances."""

    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}
        self._default = GenericAdapter()

    def register(self, adapter: Adapter) -> None:
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> Adapter:
        return self._adapters.get(name, self._default)

    def available(self) -> list[str]:
        return sorted(self._adapters.keys())


def get_default_registry() -> AdapterRegistry:
    """Build the standard adapter registry with all known providers."""
    registry = AdapterRegistry()
    registry.register(GenericAdapter())
    registry.register(OpenAIAdapter())
    registry.register(AnthropicAdapter())
    registry.register(GeminiAdapter())
    return registry


def detect_adapter(model: str) -> str:
    """Infer adapter name from model identifier.

    Examples:
        "claude-opus-4-20250514" → "anthropic"
        "gpt-4o" → "openai"
        "gemini-2.0-flash" → "gemini"
        "unknown-model" → "generic"
    """
    lower = model.lower()
    if any(prefix in lower for prefix in ("claude", "anthropic")):
        return "anthropic"
    if any(prefix in lower for prefix in ("gpt", "o1", "o3", "o4", "openai", "chatgpt")):
        return "openai"
    if any(prefix in lower for prefix in ("gemini", "google")):
        return "gemini"
    return "generic"
