"""Tests for ToolPolicy and the tool parity invariant.

The hard invariant under test:

    visible_tools = tool_policy.filter(manifest, ...)

    visible_tools
         │
         ├──► container tool schemas (what gets mounted)
         │
         └──► capability descriptions (what the harness prompt says)

    These two representations MUST agree. If a tool is not in visible_tools,
    it must not appear as an available capability. If it IS in visible_tools,
    it MUST appear as available.
"""

from __future__ import annotations

import unittest

from gravityclaw.harness import (
    Capability,
    CapabilityResolver,
    HarnessCompiler,
    HarnessContext,
    RuntimeServices,
    RuntimeSnapshot,
    SkillSummary,
    ToolPolicy,
    VisibleTool,
    VisibleToolSet,
    detect_adapter,
)


class TestToolPolicy(unittest.TestCase):
    """Unit tests for ToolPolicy.filter()."""

    def test_full_manifest_produces_all_tools(self) -> None:
        policy = ToolPolicy()
        manifest = {
            "skills": [
                {"id": "global:git-workflow", "name": "git-workflow"},
                {"id": "workspace:ws1:deploy", "name": "deploy"},
            ],
            "mcp": [
                {"id": "mcp:tavily", "name": "tavily"},
            ],
        }
        visible = policy.filter(
            manifest=manifest,
            container_capabilities={"shell": True, "files": True, "network": True,
                                    "web_search": True, "web_fetch": True},
            runtime_services=RuntimeServices(learning_enabled=True, channel="telegram"),
        )
        # All built-in tools present
        self.assertTrue(visible.has_tool("shell"))
        self.assertTrue(visible.has_tool("files"))
        self.assertTrue(visible.has_tool("web_search"))
        self.assertTrue(visible.has_tool("web_fetch"))
        # Skill and MCP tools present
        names = visible.tool_names
        self.assertIn("git-workflow", names)
        self.assertIn("deploy", names)
        self.assertIn("tavily", names)
        # 4 built-in + 2 skills + 1 mcp = 7
        self.assertEqual(len(visible.tools), 7)

    def test_disabled_shell_excluded(self) -> None:
        policy = ToolPolicy()
        visible = policy.filter(
            container_capabilities={"shell": False, "files": True, "network": True,
                                    "web_search": True, "web_fetch": True},
        )
        self.assertFalse(visible.has_tool("shell"))
        self.assertTrue(visible.has_tool("files"))

    def test_no_network_excludes_web_tools(self) -> None:
        policy = ToolPolicy()
        visible = policy.filter(
            container_capabilities={"shell": True, "files": True, "network": False,
                                    "web_search": False, "web_fetch": False},
            runtime_services=RuntimeServices(network_available=False),
        )
        self.assertFalse(visible.has_tool("web_search"))
        self.assertFalse(visible.has_tool("web_fetch"))
        self.assertTrue(visible.has_tool("shell"))

    def test_empty_manifest_no_skill_or_mcp_tools(self) -> None:
        policy = ToolPolicy()
        visible = policy.filter(manifest={"skills": [], "mcp": []})
        # Only built-in tools (defaults all True)
        self.assertEqual(len(visible.tools), 4)
        self.assertFalse(visible.has_any("git-workflow", "tavily"))

    def test_none_manifest_defaults(self) -> None:
        policy = ToolPolicy()
        visible = policy.filter(manifest=None)
        # All 4 built-in tools with defaults
        self.assertEqual(len(visible.tools), 4)

    def test_runtime_services_preserved(self) -> None:
        policy = ToolPolicy()
        services = RuntimeServices(
            learning_enabled=True,
            trust_mode="balanced",
            memory_enabled=True,
            scheduler_enabled=False,
            delegation_enabled=True,
            channel="discord",
            network_available=True,
        )
        visible = policy.filter(runtime_services=services)
        self.assertEqual(visible.runtime_services.learning_enabled, True)
        self.assertEqual(visible.runtime_services.trust_mode, "balanced")
        self.assertEqual(visible.runtime_services.scheduler_enabled, False)
        self.assertEqual(visible.runtime_services.delegation_enabled, True)
        self.assertEqual(visible.runtime_services.channel, "discord")

    def test_from_runtime_snapshot_convenience(self) -> None:
        policy = ToolPolicy()
        visible = policy.from_runtime_snapshot(
            has_shell=True,
            has_files=True,
            has_network=True,
            has_web_search=True,
            has_web_fetch=True,
            has_memory=True,
            has_scheduler=True,
            has_delegation=False,
            has_learning=True,
            trust_mode="strict",
            channel="web",
            bound_skills=[{"id": "sk1", "name": "skill-one"}],
            bound_mcps=[{"id": "mcp1", "name": "server-one"}],
        )
        self.assertTrue(visible.has_tool("shell"))
        self.assertIn("skill-one", visible.tool_names)
        self.assertIn("server-one", visible.tool_names)
        self.assertTrue(visible.runtime_services.learning_enabled)


class TestVisibleToolSet(unittest.TestCase):
    """Unit tests for VisibleToolSet helpers."""

    def test_by_category(self) -> None:
        tools = (
            VisibleTool(id="a", name="shell", category="execution",
                        description="", source="builtin"),
            VisibleTool(id="b", name="web_search", category="internet",
                        description="", source="builtin"),
            VisibleTool(id="c", name="my-skill", category="skills",
                        description="", source="skill", source_id="sk1"),
        )
        vs = VisibleToolSet(tools=tools)
        cats = vs.by_category
        self.assertEqual(len(cats["execution"]), 1)
        self.assertEqual(len(cats["internet"]), 1)
        self.assertEqual(len(cats["skills"]), 1)

    def test_by_source(self) -> None:
        tools = (
            VisibleTool(id="a", name="shell", category="execution",
                        description="", source="builtin"),
            VisibleTool(id="b", name="sk1", category="skills",
                        description="", source="skill", source_id="sk1"),
            VisibleTool(id="c", name="mcp1", category="tools",
                        description="", source="mcp", source_id="mcp1"),
        )
        vs = VisibleToolSet(tools=tools)
        sources = vs.by_source
        self.assertEqual(len(sources["builtin"]), 1)
        self.assertEqual(len(sources["skill"]), 1)
        self.assertEqual(len(sources["mcp"]), 1)

    def test_has_any(self) -> None:
        tools = (
            VisibleTool(id="a", name="shell", category="execution",
                        description="", source="builtin"),
        )
        vs = VisibleToolSet(tools=tools)
        self.assertTrue(vs.has_any("shell", "files"))
        self.assertFalse(vs.has_any("web_search", "web_fetch"))


class TestParityInvariant(unittest.TestCase):
    """The critical test: capabilities MUST match visible_tools.

    If a tool is in visible_tools, it appears as an available capability.
    If a tool is NOT in visible_tools, it appears as unavailable or absent.
    """

    def test_shell_in_tools_means_shell_available(self) -> None:
        """Shell in visible_tools → shell capability is available."""
        policy = ToolPolicy()
        resolver = CapabilityResolver()

        visible = policy.filter(container_capabilities={"shell": True, "files": True})
        caps = resolver.from_visible_tools(visible)

        shell_caps = [c for c in caps if c.name == "shell"]
        self.assertEqual(len(shell_caps), 1)
        self.assertTrue(shell_caps[0].available)

    def test_shell_not_in_tools_means_shell_unavailable(self) -> None:
        """Shell NOT in visible_tools → shell capability is unavailable."""
        policy = ToolPolicy()
        resolver = CapabilityResolver()

        visible = policy.filter(container_capabilities={"shell": False, "files": True})
        caps = resolver.from_visible_tools(visible)

        shell_caps = [c for c in caps if c.name == "shell"]
        self.assertEqual(len(shell_caps), 1)
        self.assertFalse(shell_caps[0].available)
        self.assertIsNotNone(shell_caps[0].reason)

    def test_web_tools_match_visibility(self) -> None:
        """web_search/web_fetch capabilities track visible_tools exactly."""
        policy = ToolPolicy()
        resolver = CapabilityResolver()

        # With web tools
        visible_with = policy.filter(
            container_capabilities={"web_search": True, "web_fetch": True},
            runtime_services=RuntimeServices(network_available=True),
        )
        caps_with = resolver.from_visible_tools(visible_with)
        cap_names = {c.name for c in caps_with if c.available}
        self.assertIn("web_search", cap_names)
        self.assertIn("web_fetch", cap_names)

        # Without web tools
        visible_without = policy.filter(
            container_capabilities={"web_search": False, "web_fetch": False, "network": False},
            runtime_services=RuntimeServices(network_available=False),
        )
        caps_without = resolver.from_visible_tools(visible_without)
        cap_names_without = {c.name for c in caps_without if c.available}
        self.assertNotIn("web_search", cap_names_without)
        self.assertNotIn("web_fetch", cap_names_without)

    def test_mcp_tools_in_manifest_appear_in_capabilities(self) -> None:
        """MCP servers in manifest → mcp_tools capability available."""
        policy = ToolPolicy()
        resolver = CapabilityResolver()

        visible = policy.filter(manifest={
            "skills": [],
            "mcp": [
                {"id": "mcp:tavily", "name": "tavily"},
                {"id": "mcp:github", "name": "github"},
            ],
        })
        caps = resolver.from_visible_tools(visible)
        mcp_caps = [c for c in caps if c.name == "mcp_tools" and c.available]
        self.assertEqual(len(mcp_caps), 1)
        self.assertIn("2 connected", mcp_caps[0].description)

    def test_no_mcp_in_manifest_means_no_mcp_capability(self) -> None:
        """No MCP servers in manifest → no mcp_tools capability."""
        policy = ToolPolicy()
        resolver = CapabilityResolver()

        visible = policy.filter(manifest={"skills": [], "mcp": []})
        caps = resolver.from_visible_tools(visible)
        mcp_caps = [c for c in caps if c.name == "mcp_tools"]
        self.assertEqual(len(mcp_caps), 0)

    def test_skills_in_manifest_appear_in_capabilities(self) -> None:
        """Skills in manifest → skills capability with correct count."""
        policy = ToolPolicy()
        resolver = CapabilityResolver()

        visible = policy.filter(manifest={
            "skills": [
                {"id": "sk1", "name": "deploy"},
                {"id": "sk2", "name": "test-runner"},
                {"id": "sk3", "name": "lint"},
            ],
            "mcp": [],
        })
        caps = resolver.from_visible_tools(visible)
        skill_caps = [c for c in caps if c.name == "skills" and c.available]
        self.assertEqual(len(skill_caps), 1)
        self.assertIn("3 skills available", skill_caps[0].description)

    def test_runtime_services_independent_of_tools(self) -> None:
        """Runtime features like memory/scheduler derive from services, not tools."""
        policy = ToolPolicy()
        resolver = CapabilityResolver()

        # Memory enabled in services but no special tool for it
        visible = policy.filter(
            runtime_services=RuntimeServices(
                memory_enabled=True,
                scheduler_enabled=True,
                delegation_enabled=True,
                channel="telegram",
            ),
        )
        caps = resolver.from_visible_tools(visible)
        cap_names = {c.name for c in caps if c.available}
        self.assertIn("persistent_memory", cap_names)
        self.assertIn("scheduler", cap_names)
        self.assertIn("delegation", cap_names)
        self.assertIn("channel:telegram", cap_names)

    def test_disabled_services_not_in_capabilities(self) -> None:
        """Disabled runtime services don't appear as capabilities."""
        policy = ToolPolicy()
        resolver = CapabilityResolver()

        visible = policy.filter(
            runtime_services=RuntimeServices(
                memory_enabled=False,
                scheduler_enabled=False,
                delegation_enabled=False,
                learning_enabled=False,
                channel="internal",
            ),
        )
        caps = resolver.from_visible_tools(visible)
        cap_names = {c.name for c in caps if c.available}
        self.assertNotIn("persistent_memory", cap_names)
        self.assertNotIn("scheduler", cap_names)
        self.assertNotIn("delegation", cap_names)
        self.assertNotIn("learning", cap_names)
        # "internal" channel produces no channel capability
        channel_caps = [c for c in caps if c.name.startswith("channel:")]
        self.assertEqual(len(channel_caps), 0)


class TestLegacyPathParity(unittest.TestCase):
    """Legacy RuntimeSnapshot path must produce the same result as canonical path."""

    def test_resolve_matches_from_visible_tools(self) -> None:
        """CapabilityResolver.resolve(snapshot) == from_visible_tools(policy(snapshot))."""
        resolver = CapabilityResolver()
        policy = ToolPolicy()

        # Full runtime
        snapshot = RuntimeSnapshot(
            has_shell=True,
            has_file_access=True,
            has_web_search=True,
            has_web_fetch=True,
            has_memory=True,
            has_scheduler=True,
            has_delegation=True,
            has_learning=True,
            has_skills=True,
            channel="telegram",
            has_network=True,
            bound_skill_count=3,
            bound_mcp_count=2,
        )

        # Legacy path
        legacy_caps = resolver.resolve(snapshot)

        # Canonical path (manually replicate the same inputs)
        visible = policy.from_runtime_snapshot(
            has_shell=True,
            has_files=True,
            has_network=True,
            has_web_search=True,
            has_web_fetch=True,
            has_memory=True,
            has_scheduler=True,
            has_delegation=True,
            has_learning=True,
            channel="telegram",
            bound_skills=[{"id": f"skill_{i}", "name": f"skill_{i}"} for i in range(3)],
            bound_mcps=[{"id": f"mcp_{i}", "name": f"mcp_{i}"} for i in range(2)],
        )
        canonical_caps = resolver.from_visible_tools(visible)

        # Same names
        legacy_names = {c.name for c in legacy_caps}
        canonical_names = {c.name for c in canonical_caps}
        self.assertEqual(legacy_names, canonical_names)

        # Same availability
        legacy_available = {c.name for c in legacy_caps if c.available}
        canonical_available = {c.name for c in canonical_caps if c.available}
        self.assertEqual(legacy_available, canonical_available)

    def test_from_settings_still_works(self) -> None:
        """from_settings() convenience still produces valid capabilities."""
        resolver = CapabilityResolver()
        caps = resolver.from_settings(
            learning_enabled=True,
            has_scheduler=True,
            channel="web",
            network=True,
            bound_skills=2,
        )
        names = {c.name for c in caps if c.available}
        self.assertIn("shell", names)
        self.assertIn("web_search", names)
        self.assertIn("learning", names)
        self.assertIn("skills", names)
        self.assertIn("scheduler", names)


class TestCompilerWithVisibleTools(unittest.TestCase):
    """End-to-end: ToolPolicy → CapabilityResolver → HarnessCompiler."""

    def test_full_pipeline_produces_valid_prompt(self) -> None:
        policy = ToolPolicy()
        resolver = CapabilityResolver()
        compiler = HarnessCompiler()

        manifest = {
            "skills": [{"id": "sk1", "name": "deploy"}],
            "mcp": [{"id": "mcp1", "name": "tavily"}],
        }
        services = RuntimeServices(
            learning_enabled=True,
            trust_mode="balanced",
            memory_enabled=True,
            scheduler_enabled=True,
            channel="telegram",
        )
        visible = policy.filter(
            manifest=manifest,
            runtime_services=services,
        )
        caps = resolver.from_visible_tools(visible)

        ctx = HarnessContext(
            model="claude-opus-4-20250514",
            provider="Anthropic",
            channel="telegram",
            workspace="/data/gc",
            learning_enabled=True,
            trust_mode="balanced",
            capabilities=caps,
            visible_tools=visible,
            adapter="anthropic",
        )
        prompt = compiler.compile(ctx)

        # Core identity
        self.assertIn("GravityClaw", prompt)
        self.assertIn("Agent runtime: GravityClaw", prompt)
        # Tool capabilities present
        self.assertIn("shell", prompt)
        self.assertIn("web_search", prompt)
        # MCP present
        self.assertIn("mcp_tools", prompt)
        # Runtime services present
        self.assertIn("persistent_memory", prompt)
        self.assertIn("scheduler", prompt)
        self.assertIn("Learning Mode: enabled", prompt)
        self.assertIn("balanced", prompt)

    def test_restricted_manifest_restricts_prompt(self) -> None:
        """If manifest has no MCP, prompt must not claim MCP is available."""
        policy = ToolPolicy()
        resolver = CapabilityResolver()
        compiler = HarnessCompiler()

        visible = policy.filter(
            manifest={"skills": [], "mcp": []},
            container_capabilities={"shell": False, "web_search": False, "web_fetch": False},
            runtime_services=RuntimeServices(
                network_available=False,
                learning_enabled=False,
                scheduler_enabled=False,
            ),
        )
        caps = resolver.from_visible_tools(visible)

        ctx = HarnessContext(
            model="gpt-4o",
            provider="OpenAI",
            channel="web",
            workspace="/test",
            learning_enabled=False,
            capabilities=caps,
            visible_tools=visible,
            adapter="openai",
        )
        prompt = compiler.compile(ctx)

        # Shell and network unavailable
        self.assertIn("Unavailable for this run", prompt)
        self.assertIn("shell", prompt.split("Unavailable")[1])
        self.assertIn("network", prompt.split("Unavailable")[1])
        # MCP not mentioned in available section
        available_section = prompt.split("# Available GravityClaw Features")[1]
        if "Unavailable" in available_section:
            available_part = available_section.split("Unavailable")[0]
        else:
            available_part = available_section
        self.assertNotIn("mcp_tools", available_part)
        # Learning not in available features
        self.assertNotIn("Learn reusable", available_part)


if __name__ == "__main__":
    unittest.main()
