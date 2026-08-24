"""Server-side AGY model capability and resolution helpers."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class ModelCapability:
    id: str
    label: str


class AgyModelCatalog:
    """The server-owned model allow-list exposed to Web and channel clients.

    AGY does not provide a stable model-list command, so deployments declare the
    models their installed AGY/runtime can use. The installed binary version is
    probed server-side and persisted into each run snapshot.
    """

    def __init__(
        self,
        binary: str = "agy",
        models: Iterable[str] = (),
        default_model: str | None = None,
        version: str | None = None,
    ) -> None:
        normalized = tuple(dict.fromkeys(item.strip() for item in models if item.strip()))
        if default_model and default_model not in normalized:
            normalized = (*normalized, default_model)
        self.binary = binary
        self.models = tuple(ModelCapability(item, _model_label(item)) for item in normalized)
        self.default_model = default_model or (normalized[0] if normalized else None)
        self.version = version or probe_version(binary)

    def validate(self, model: str | None) -> str | None:
        if model is None:
            return None
        value = model.strip()
        if not value:
            return None
        if self.models and value not in {item.id for item in self.models}:
            raise ValueError(f"model is not available from the server: {value}")
        return value

    def snapshot(self) -> dict[str, Any]:
        return {
            "models": [{"id": item.id, "label": item.label} for item in self.models],
            "default_model": self.default_model,
            "agy_version": self.version,
            "binary": self.binary,
            "source": "server",
        }


def probe_version(binary: str) -> str:
    try:
        completed = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if completed.returncode != 0:
        return "unknown"
    value = (completed.stdout or completed.stderr).strip().splitlines()
    return value[0][:200] if value else "unknown"


_MODEL_LABELS: dict[str, str] = {
    "gpt-5.6-luna": "GPT-5.6 Luna",
    "gemini-3.7-flash": "Gemini 3.7 Flash",
    "gemini-3.6-flash": "Gemini 3.6 Flash",
    "gemini-3.5-flash": "Gemini 3.5 Flash",
    "gemini-3.1-pro": "Gemini 3.1 Pro",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "claude-opus-4-6-thinking": "Claude Opus 4.6 (Thinking)",
    "gpt-oss-120b": "GPT-OSS 120B",
}

MODEL_CONTEXT_LIMITS: dict[str, int] = {
    # Gemini models (1M token standard context window)
    "gemini-3.7-flash": 1_000_000,
    "gemini-3.6-flash": 1_000_000,
    "gemini-3.5-flash": 1_000_000,
    "gemini-3.1-pro": 1_000_000,
    "gemini-2.5-flash": 1_000_000,
    "gemini-2.5-pro": 1_000_000,
    "gemini-2.0-flash": 1_000_000,
    "gemini-1.5-pro": 2_000_000,
    "gemini-1.5-flash": 1_000_000,

    # Claude models (200k token context window)
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-6-thinking": 200_000,
    "claude-3-7-sonnet": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,

    # GPT / OpenAI models (128k / 256k token context window)
    "gpt-5.6-luna": 256_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-oss-120b": 128_000,
    "o1": 200_000,
    "o3-mini": 200_000,
}


def get_model_context_limit(model_id: str | None) -> int:
    """Return the context limit in tokens for a given model ID. Default 1M for Gemini."""
    if not model_id:
        return 1_000_000
    norm = model_id.lower().strip()
    if norm in MODEL_CONTEXT_LIMITS:
        return MODEL_CONTEXT_LIMITS[norm]
    if "gemini" in norm:
        return 1_000_000
    if "claude" in norm or "sonnet" in norm or "opus" in norm or "haiku" in norm:
        return 200_000
    if "gpt-5" in norm or "gpt-4.5" in norm:
        return 256_000
    if "gpt" in norm or "o1" in norm or "o3" in norm:
        return 128_000
    return 1_000_000


def _model_label(model_id: str) -> str:
    """Return a human-friendly label for a model ID."""
    if model_id in _MODEL_LABELS:
        return _MODEL_LABELS[model_id]
    # Fallback: title-case with dashes replaced
    return model_id.replace("-", " ").title()
