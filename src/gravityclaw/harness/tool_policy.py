"""ToolPolicy — single source of truth for what tools are visible to a run.

This module enforces the hard invariant:

    visible_tools = tool_policy.filter(registered_tools, ...)

    visible_tools
         │
         ├──► API tool schemas (container mounts via apply_to_spec)
         │
         └──► capability descriptions (harness prompt)

One set, two representations. The capability resolver MUST derive its
tool-based capabilities from the same visible_tools that the container
receives. They cannot disagree.

Non-tool runtime features (learning mode, trust policy, channel, memory
service state) are separate — they come from RuntimeServices, not from
tool visibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class VisibleTool:
    """A single tool visible to the agent for this run.

    This is the canonical representation. Both the container spec (which
    mounts/exposes it) and the harness prompt (which describes it) derive
    from this same object.
    """

    id: str
    name: str
    category: str
    description: str
    source: str  # "builtin", "skill", "mcp"
    # For MCP tools: server name; for skills: skill id
    source_id: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    """Non-tool runtime features derived from configuration and services.

    These are things the agent can observe or that affect its behavior,
    but are NOT callable tools. They come from config/service state, not
    from the tool registry.
    """

    learning_enabled: bool = False
    trust_mode: str = "strict"
    memory_enabled: bool = True
    scheduler_enabled: bool = True
    delegation_enabled: bool = False
    channel: str = "web"
    network_available: bool = True


@dataclass(frozen=True, slots=True)
class VisibleToolSet:
    """Immutable set of tools visible for a specific run.

    This is the output of ToolPolicy.filter() and the single source of truth.
    Pass it to both the container spec builder and the capability resolver.
    """

    tools: tuple[VisibleTool, ...] = ()
    runtime_services: RuntimeServices = field(default_factory=RuntimeServices)

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(t.name for t in self.tools)

    @property
    def by_category(self) -> dict[str, list[VisibleTool]]:
        groups: dict[str, list[VisibleTool]] = {}
        for tool in self.tools:
            groups.setdefault(tool.category, []).append(tool)
        return groups

    @property
    def by_source(self) -> dict[str, list[VisibleTool]]:
        groups: dict[str, list[VisibleTool]] = {}
        for tool in self.tools:
            groups.setdefault(tool.source, []).append(tool)
        return groups

    def has_tool(self, name: str) -> bool:
        return name in self.tool_names

    def has_any(self, *names: str) -> bool:
        return bool(self.tool_names & set(names))


# ─────────────────────────────────────────────────────────────────────────────
# Built-in tool definitions — the baseline tools GravityClaw always exposes
# depending on container configuration.
# ─────────────────────────────────────────────────────────────────────────────

BUILTIN_TOOLS: tuple[VisibleTool, ...] = (
    VisibleTool(
        id="builtin:shell",
        name="shell",
        category="execution",
        description="Execute commands in the workspace.",
        source="builtin",
    ),
    VisibleTool(
        id="builtin:files",
        name="files",
        category="execution",
        description="Read, search, and modify workspace files.",
        source="builtin",
    ),
    VisibleTool(
        id="builtin:web_search",
        name="web_search",
        category="internet",
        description="Search current public information.",
        source="builtin",
    ),
    VisibleTool(
        id="builtin:web_fetch",
        name="web_fetch",
        category="internet",
        description="Read web resources.",
        source="builtin",
    ),
)


class ToolPolicy:
    """Filter registered tools into a VisibleToolSet for a specific run.

    Usage:
        policy = ToolPolicy()
        visible = policy.filter(
            manifest=capability_manifest,
            workspace_id=workspace.id,
            channel="telegram",
            profile="chat",
            container_capabilities={"shell": True, "files": True, ...},
            runtime_services=RuntimeServices(...),
        )

    The returned VisibleToolSet is then passed to BOTH:
    - The capability resolver (for the harness prompt)
    - The container spec builder (for tool mounting)
    """

    def filter(
        self,
        *,
        manifest: Mapping[str, Any] | None = None,
        container_capabilities: Mapping[str, bool] | None = None,
        runtime_services: RuntimeServices | None = None,
    ) -> VisibleToolSet:
        """Compute the visible tool set for a run.

        Args:
            manifest: The capability manifest from CapabilityManager.prepare_run().
                      Contains 'skills' and 'mcp' entries that are already filtered
                      by workspace/profile/binding.
            container_capabilities: Which built-in tools the container grants.
                                   Keys: shell, files, web_search, web_fetch, network.
                                   Missing keys default to True.
            runtime_services: Non-tool runtime service state.
        """
        services = runtime_services or RuntimeServices()
        caps = container_capabilities or {}
        tools: list[VisibleTool] = []

        # 1. Built-in tools gated by container capabilities
        has_shell = caps.get("shell", True)
        has_files = caps.get("files", True)
        has_network = caps.get("network", services.network_available)
        has_web_search = caps.get("web_search", has_network)
        has_web_fetch = caps.get("web_fetch", has_network)

        if has_shell:
            tools.append(_find_builtin("shell"))
        if has_files:
            tools.append(_find_builtin("files"))
        if has_web_search:
            tools.append(_find_builtin("web_search"))
        if has_web_fetch:
            tools.append(_find_builtin("web_fetch"))

        # 2. Skill-provided tools from manifest
        if manifest:
            for skill in manifest.get("skills", []):
                tools.append(VisibleTool(
                    id=str(skill.get("id", "")),
                    name=str(skill.get("name", "")),
                    category="skills",
                    description=f"Skill: {skill.get('name', '')}",
                    source="skill",
                    source_id=str(skill.get("id", "")),
                ))

            # 3. MCP-provided tools from manifest
            for server in manifest.get("mcp", []):
                tools.append(VisibleTool(
                    id=str(server.get("id", "")),
                    name=str(server.get("name", "")),
                    category="tools",
                    description=f"MCP server: {server.get('name', '')}",
                    source="mcp",
                    source_id=str(server.get("id", "")),
                ))

        return VisibleToolSet(
            tools=tuple(tools),
            runtime_services=services,
        )

    def from_runtime_snapshot(
        self,
        *,
        has_shell: bool = True,
        has_files: bool = True,
        has_network: bool = True,
        has_web_search: bool | None = None,
        has_web_fetch: bool | None = None,
        has_memory: bool = True,
        has_scheduler: bool = True,
        has_delegation: bool = False,
        has_learning: bool = False,
        trust_mode: str = "strict",
        channel: str = "web",
        bound_skills: Sequence[Mapping[str, Any]] = (),
        bound_mcps: Sequence[Mapping[str, Any]] = (),
    ) -> VisibleToolSet:
        """Convenience: build a VisibleToolSet from high-level flags.

        Used when you don't have a full manifest (e.g. tests, or simple
        dispatch paths that predate the CapabilityManager).
        """
        container_capabilities = {
            "shell": has_shell,
            "files": has_files,
            "network": has_network,
            "web_search": has_web_search if has_web_search is not None else has_network,
            "web_fetch": has_web_fetch if has_web_fetch is not None else has_network,
        }
        manifest: dict[str, Any] = {
            "skills": list(bound_skills),
            "mcp": list(bound_mcps),
        }
        services = RuntimeServices(
            learning_enabled=has_learning,
            trust_mode=trust_mode,
            memory_enabled=has_memory,
            scheduler_enabled=has_scheduler,
            delegation_enabled=has_delegation,
            channel=channel,
            network_available=has_network,
        )
        return self.filter(
            manifest=manifest,
            container_capabilities=container_capabilities,
            runtime_services=services,
        )


def _find_builtin(name: str) -> VisibleTool:
    """Look up a built-in tool by name."""
    for tool in BUILTIN_TOOLS:
        if tool.name == name:
            return tool
    raise KeyError(f"unknown built-in tool: {name}")
