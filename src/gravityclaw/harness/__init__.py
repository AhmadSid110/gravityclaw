"""Agent Harness — Phase 6.

Compiles a truthful GravityClaw system prompt before every model call.
The harness tells the selected model what GravityClaw is, what features
it has, what tools are available, what skills are relevant, and what
runtime it is currently operating in.

Public API:
    HarnessCompiler  — core prompt assembly
    HarnessContext   — per-dispatch context snapshot
    Capability       — single available/unavailable feature
    SkillSummary     — metadata-only skill reference
    CapabilityResolver — derive capabilities from runtime state
    RuntimeSnapshot  — lightweight runtime state view (deprecated)
    ToolPolicy       — single source of truth for visible tools
    VisibleTool      — a tool visible to the agent
    VisibleToolSet   — immutable set of visible tools + runtime services
    RuntimeServices  — non-tool runtime feature state
    detect_adapter   — infer adapter from model name
"""

from .adapters import (
    Adapter,
    AdapterRegistry,
    AnthropicAdapter,
    GeminiAdapter,
    GenericAdapter,
    OpenAIAdapter,
    detect_adapter,
    get_default_registry,
)
from .capabilities import CapabilityResolver, RuntimeSnapshot
from .compiler import HarnessCompiler
from .models import Capability, HarnessContext, SkillSummary
from .tool_policy import RuntimeServices, ToolPolicy, VisibleTool, VisibleToolSet

__all__ = [
    "Adapter",
    "AdapterRegistry",
    "AnthropicAdapter",
    "Capability",
    "CapabilityResolver",
    "GeminiAdapter",
    "GenericAdapter",
    "HarnessCompiler",
    "HarnessContext",
    "OpenAIAdapter",
    "RuntimeServices",
    "RuntimeSnapshot",
    "RuntimeServices",
    "SkillSummary",
    "ToolPolicy",
    "VisibleTool",
    "VisibleToolSet",
    "detect_adapter",
    "get_default_registry",
]
