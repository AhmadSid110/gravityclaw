"""HarnessCompiler — assemble the GravityClaw system prompt for any model.

The compiler produces a single string injected as a TRUSTED identity document
into the ContextBuilder. It contains:

1. Core GravityClaw identity (stable, rarely changes)
2. Dynamic runtime facts (model, channel, workspace, learning state)
3. Available capability summary (grouped by category)
4. Relevant skill summaries (metadata only, progressive disclosure)
5. Adapter-specific instructions (tiny per-provider tweaks)

The compiled output deliberately does NOT repeat:
- Tool schemas (the tool list already provides exact calling interfaces)
- Full skill content (loaded on demand via skill_view)
- User/operational memory (injected separately by ContextBuilder)
"""

from __future__ import annotations

from .adapters import AdapterRegistry, get_default_registry
from .models import Capability, HarnessContext, SkillSummary


# ─────────────────────────────────────────────────────────────────────────────
# Stable core identity — change this text deliberately and infrequently
# ─────────────────────────────────────────────────────────────────────────────

_CORE_IDENTITY = """\
# GravityClaw

You are an AI agent operating inside GravityClaw.

GravityClaw is the agent runtime around the underlying language model. \
It provides your tools, persistent memory, procedural skills, scheduling, \
execution environment, communication channels, and other connected capabilities.

The underlying model is your reasoning engine. GravityClaw determines which \
capabilities are actually available for the current run.

## Capability truth

Only assume that a capability exists when it is listed as available below \
or when an available tool exposes it.

Do not claim that you performed an action unless the corresponding tool or \
runtime operation reports success.

## Execution

When the user asks you to perform an actionable task and appropriate tools \
are available, perform it instead of only explaining how.

Use tools when they provide more reliable or current information than your \
parametric knowledge.

Recover from failures when a reasonable alternative exists.

Verify important actions before reporting completion.

## Skills

Skills contain reusable procedural knowledge learned or installed in GravityClaw.

Use relevant skills when useful. Skills provide guidance; they do not grant \
capabilities.

If runtime evidence conflicts with an old skill, prefer current evidence.

## Memory

GravityClaw may provide durable user memory, operational memory, and retrieved \
historical context.

Do not invent memories that were not provided or retrieved.

## Learning

When Learning Mode is available, GravityClaw can retain durable facts and \
reusable procedures according to the configured trust policy.\
"""


class HarnessCompiler:
    """Compile a GravityClaw harness prompt from runtime context.

    Usage:
        compiler = HarnessCompiler()
        prompt = compiler.compile(harness_context)
    """

    def __init__(
        self,
        *,
        adapter_registry: AdapterRegistry | None = None,
        core_identity: str | None = None,
    ) -> None:
        self._adapters = adapter_registry or get_default_registry()
        self._core_identity = core_identity or _CORE_IDENTITY

    def compile(self, ctx: HarnessContext) -> str:
        """Produce the complete harness prompt string."""
        sections = [
            self._core_identity,
            self._runtime_section(ctx),
            self._capabilities_section(ctx),
            self._skills_section(ctx),
            self._adapter_section(ctx),
        ]
        return "\n\n".join(section for section in sections if section)

    def _runtime_section(self, ctx: HarnessContext) -> str:
        """Dynamic runtime facts — regenerated every dispatch."""
        if not ctx.sandbox_enabled:
            lines = [
                "# Runtime",
                "",
                "You are operating as GravityClaw on the host machine.",
                "",
                "Execution:",
                "- Host terminal: available",
                f"- Host: {ctx.host_name}",
                f"- User: {ctx.host_user}",
                f"- Working directory: current workspace ({ctx.workspace})",
                "- Filesystem access: host filesystem subject to OS permissions",
                "- Network access: available",
                "- System services: accessible according to OS permissions",
                "- Elevated commands may require approval",
                "- Attached media: when the user provides attachments or screenshots, use `view_file` on the provided local path to inspect the image or document",
                "",
                "There is no sandboxed execution environment for this run.",
                "",
                f"Agent runtime: GravityClaw",
                f"Model: {ctx.model}",
                f"Provider: {ctx.provider}",
                f"Channel: {ctx.channel}",
                f"Workspace: {ctx.workspace}",
            ]
        else:
            lines = [
                "# Current Runtime",
                "",
                f"Agent runtime: GravityClaw",
                f"Model: {ctx.model}",
                f"Provider: {ctx.provider}",
                f"Channel: {ctx.channel}",
                f"Workspace: {ctx.workspace}",
            ]
        # Feature state
        if ctx.learning_enabled:
            lines.append(f"Learning Mode: enabled (trust: {ctx.trust_mode})")
        else:
            lines.append("Learning Mode: disabled")
        return "\n".join(lines)

    def _capabilities_section(self, ctx: HarnessContext) -> str:
        """Available features grouped by category.

        Tool capabilities (derived from visible_tools) and runtime feature
        capabilities (derived from services/config) are rendered in a single
        list grouped by category. The category ordering naturally clusters
        them: execution/internet/tools are tool-derived; memory/learning/
        automation/agents/communication are service-derived.
        """
        available = ctx.available_capabilities
        unavailable = ctx.unavailable_capabilities
        if not available and not unavailable:
            return ""

        lines = ["# Available GravityClaw Features", ""]

        # Group available capabilities by category
        categories = ctx.capability_categories
        # Tool-derived categories first, then service-derived
        category_order = [
            # Tool capabilities (derived from visible_tools)
            "execution", "internet", "tools",
            # Runtime feature capabilities (derived from services/config)
            "memory", "learning", "automation", "agents", "communication",
        ]
        for category in category_order:
            caps = categories.get(category)
            if not caps:
                continue
            lines.append(f"{category.title()}")
            for cap in caps:
                lines.append(f"- {cap.name} — {cap.description}")
            lines.append("")

        # Any categories not in the fixed order
        for category, caps in sorted(categories.items()):
            if category in category_order:
                continue
            lines.append(f"{category.title()}")
            for cap in caps:
                lines.append(f"- {cap.name} — {cap.description}")
            lines.append("")

        # Explicitly note unavailable features (helps prevent hallucination)
        if unavailable:
            lines.append("Unavailable for this run")
            for cap in unavailable:
                reason = f" ({cap.reason})" if cap.reason else ""
                lines.append(f"- {cap.name}{reason}")
            lines.append("")

        return "\n".join(lines).rstrip()

    def _skills_section(self, ctx: HarnessContext) -> str:
        """Relevant skill summaries — metadata only."""
        if not ctx.skills:
            return ""

        lines = [
            "# Relevant Skills",
            "",
            "The following procedural skills may be relevant to the current task.",
            "Use skill_view to load the full procedure when needed.",
            "",
        ]
        for skill in ctx.skills:
            lines.append(f"- {skill.name}")
            lines.append(f"  {skill.description}")
        return "\n".join(lines)

    def _adapter_section(self, ctx: HarnessContext) -> str:
        """Provider-specific instructions (tiny tweaks)."""
        adapter = self._adapters.get(ctx.adapter)
        instructions = adapter.instructions(ctx)
        return instructions if instructions else ""
