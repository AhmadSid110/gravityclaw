"""CapabilityResolver — derive what an agent can actually do from live runtime state.

Source of truth is the VisibleToolSet produced by ToolPolicy:

    visible_tools = tool_policy.filter(registered_tools, ...)

    capabilities = capability_resolver.resolve(
        visible_tools=visible_tools,
        runtime_services=runtime_services,
    )

Callable tool capabilities come from visible_tools.
Runtime feature capabilities come from runtime_services/configuration.
One set, two representations — tool schemas and capability descriptions
can never disagree.

The legacy RuntimeSnapshot path is preserved for backward compatibility
but delegates to the canonical from_visible_tools() internally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .models import Capability
from .tool_policy import RuntimeServices, VisibleTool, VisibleToolSet


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """Lightweight view of live runtime state at dispatch time.

    DEPRECATED: Prefer ToolPolicy.filter() → VisibleToolSet → from_visible_tools().
    Kept for backward compatibility with tests and simple dispatch paths.
    """

    # What tools/features the container has access to
    has_shell: bool = True
    has_file_access: bool = True
    has_web_search: bool = False
    has_web_fetch: bool = False

    # Services
    has_memory: bool = True
    has_scheduler: bool = True
    has_delegation: bool = False
    has_learning: bool = False
    has_skills: bool = False

    # Channel
    channel: str = "web"
    has_network: bool = True

    # Capabilities from CapabilityManager
    bound_skill_count: int = 0
    bound_mcp_count: int = 0

    # Additional metadata
    extra: Mapping[str, Any] = ()  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # Normalize extra to a dict if passed as empty tuple (frozen default)
        if self.extra == ():
            object.__setattr__(self, "extra", {})


class CapabilityResolver:
    """Resolve available capabilities from runtime state.

    Canonical path (enforces tool parity invariant):
        visible = tool_policy.filter(...)
        caps = resolver.from_visible_tools(visible)

    Legacy path (still works, delegates internally):
        caps = resolver.resolve(runtime_snapshot)
        caps = resolver.from_settings(...)
    """

    # ─────────────────────────────────────────────────────────────────────
    # Canonical path: derive capabilities from VisibleToolSet
    # ─────────────────────────────────────────────────────────────────────

    def from_visible_tools(self, visible: VisibleToolSet) -> tuple[Capability, ...]:
        """Derive capabilities from the canonical VisibleToolSet.

        This is the ONLY path that guarantees tool parity: the same tools
        that get mounted in the container are the same tools that appear
        in the harness prompt.

        Capabilities are split into:
        1. Tool capabilities — derived directly from visible_tools
        2. Runtime feature capabilities — derived from runtime_services
        """
        capabilities: list[Capability] = []
        services = visible.runtime_services
        tools = visible.tools

        # ── Tool capabilities (from visible_tools) ──────────────────────

        # Execution tools
        if visible.has_tool("shell"):
            capabilities.append(Capability(
                name="shell",
                description="Execute commands on the host machine subject to user privileges.",
                category="execution",
            ))
        else:
            capabilities.append(Capability(
                name="shell",
                description="Execute commands on the host machine.",
                category="execution",
                available=False,
                reason="shell access not granted for this run",
            ))

        if visible.has_tool("files"):
            capabilities.append(Capability(
                name="files",
                description="Read, search, and modify workspace and host files.",
                category="execution",
            ))

        # Internet tools
        if services.network_available:
            if visible.has_tool("web_search"):
                capabilities.append(Capability(
                    name="web_search",
                    description="Search current public information.",
                    category="internet",
                ))
            if visible.has_tool("web_fetch"):
                capabilities.append(Capability(
                    name="web_fetch",
                    description="Read web resources.",
                    category="internet",
                ))
            if not visible.has_any("web_search", "web_fetch"):
                capabilities.append(Capability(
                    name="network",
                    description="Network access available for tool operations.",
                    category="internet",
                ))
        else:
            capabilities.append(Capability(
                name="network",
                description="Network connectivity.",
                category="internet",
                available=False,
                reason="network disabled for this run",
            ))

        # Skill tools
        skill_tools = [t for t in tools if t.source == "skill"]
        if skill_tools:
            desc = f"Discover and use reusable procedural skills. ({len(skill_tools)} skills available)"
            capabilities.append(Capability(
                name="skills",
                description=desc,
                category="learning",
            ))

        # MCP tools
        mcp_tools = [t for t in tools if t.source == "mcp"]
        if mcp_tools:
            capabilities.append(Capability(
                name="mcp_tools",
                description=f"External tool servers ({len(mcp_tools)} connected).",
                category="tools",
            ))

        # ── Runtime feature capabilities (from services/config) ─────────

        if services.memory_enabled:
            capabilities.append(Capability(
                name="persistent_memory",
                description="Store and retrieve durable information across sessions.",
                category="memory",
            ))

        if services.learning_enabled:
            capabilities.append(Capability(
                name="learning",
                description="Learn reusable procedural skills from conversations and sources.",
                category="learning",
            ))

        if services.scheduler_enabled:
            capabilities.append(Capability(
                name="scheduler",
                description="Create durable scheduled and recurring tasks.",
                category="automation",
            ))

        if services.delegation_enabled:
            capabilities.append(Capability(
                name="delegation",
                description="Delegate suitable work to subagents.",
                category="agents",
            ))

        if services.channel and services.channel != "internal":
            capabilities.append(Capability(
                name=f"channel:{services.channel}",
                description=f"Current communication channel: {services.channel}.",
                category="communication",
            ))

        return tuple(capabilities)

    # ─────────────────────────────────────────────────────────────────────
    # Legacy path: resolve from RuntimeSnapshot (backward compat)
    # ─────────────────────────────────────────────────────────────────────

    def resolve(self, runtime: RuntimeSnapshot) -> tuple[Capability, ...]:
        """Build the complete capability list from a RuntimeSnapshot.

        DEPRECATED: Prefer from_visible_tools() for new code. This method
        converts the snapshot to a VisibleToolSet internally to maintain
        the single-path invariant.
        """
        from .tool_policy import ToolPolicy

        policy = ToolPolicy()
        visible = policy.from_runtime_snapshot(
            has_shell=runtime.has_shell,
            has_files=runtime.has_file_access,
            has_network=runtime.has_network,
            has_web_search=runtime.has_web_search,
            has_web_fetch=runtime.has_web_fetch,
            has_memory=runtime.has_memory,
            has_scheduler=runtime.has_scheduler,
            has_delegation=runtime.has_delegation,
            has_learning=runtime.has_learning,
            channel=runtime.channel,
            bound_skills=[{"id": f"skill_{i}", "name": f"skill_{i}"}
                          for i in range(runtime.bound_skill_count)],
            bound_mcps=[{"id": f"mcp_{i}", "name": f"mcp_{i}"}
                        for i in range(runtime.bound_mcp_count)],
        )
        return self.from_visible_tools(visible)

    def from_settings(
        self,
        *,
        learning_enabled: bool = False,
        has_scheduler: bool = True,
        has_memory: bool = True,
        channel: str = "web",
        network: bool = True,
        bound_skills: int = 0,
        bound_mcps: int = 0,
    ) -> tuple[Capability, ...]:
        """Convenience: resolve capabilities from high-level settings.

        DEPRECATED: Prefer ToolPolicy.from_runtime_snapshot() → from_visible_tools().
        Kept for backward compatibility with existing tests.
        """
        snapshot = RuntimeSnapshot(
            has_shell=True,
            has_file_access=True,
            has_web_search=network,
            has_web_fetch=network,
            has_memory=has_memory,
            has_scheduler=has_scheduler,
            has_delegation=False,
            has_learning=learning_enabled,
            has_skills=learning_enabled and bound_skills > 0,
            channel=channel,
            has_network=network,
            bound_skill_count=bound_skills,
            bound_mcp_count=bound_mcps,
        )
        return self.resolve(snapshot)
