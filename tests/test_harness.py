"""Tests for Agent Harness — Phase 6.

Behavioral tests (not string-matching):

Test 1 — Identity awareness
    The model knows it is operating inside GravityClaw.

Test 2 — Feature awareness
    With learning enabled, the prompt declares learning capability.

Test 3 — Disabled capability
    Network disabled → prompt explicitly marks it unavailable and does not
    list web_search/web_fetch as available.

Test 4 — Model switching
    Switching model changes the runtime section; all share GravityClaw identity.

Test 5 — Skill awareness
    Relevant skills appear as metadata-only summaries.

Test 6 — Action awareness (shell available vs unavailable)
    Shell available → listed; shell unavailable → marked unavailable with reason.
"""

from __future__ import annotations

import unittest

from gravityclaw.harness import (
    Capability,
    CapabilityResolver,
    HarnessCompiler,
    HarnessContext,
    RuntimeSnapshot,
    SkillSummary,
    detect_adapter,
)


class TestIdentityAwareness(unittest.TestCase):
    """Test 1 — The model knows it is inside GravityClaw."""

    def test_prompt_declares_gravityclaw(self) -> None:
        ctx = self._basic_context("gemini-2.0-flash")
        prompt = HarnessCompiler().compile(ctx)
        # The model should unambiguously know what runtime it's in
        self.assertIn("GravityClaw", prompt)
        self.assertIn("agent runtime", prompt.lower())

    def test_prompt_declares_model_identity(self) -> None:
        ctx = self._basic_context("claude-opus-4-20250514")
        prompt = HarnessCompiler().compile(ctx)
        self.assertIn("claude-opus-4-20250514", prompt)

    def test_prompt_does_not_claim_to_be_model_directly(self) -> None:
        """The harness says the model powers GravityClaw, not that it IS the model."""
        ctx = self._basic_context("gpt-4o")
        prompt = HarnessCompiler().compile(ctx)
        # Should not have "You are GPT-4o" or similar
        self.assertNotIn("You are gpt-4o", prompt)
        self.assertNotIn("You are GPT", prompt)

    def _basic_context(self, model: str) -> HarnessContext:
        resolver = CapabilityResolver()
        caps = resolver.from_settings(learning_enabled=True, channel="web")
        return HarnessContext(
            model=model,
            provider="Test",
            channel="web",
            workspace="/test",
            capabilities=caps,
            adapter=detect_adapter(model),
        )


class TestFeatureAwareness(unittest.TestCase):
    """Test 2 — Learning enabled → prompt knows about it."""

    def test_learning_enabled_appears(self) -> None:
        caps = CapabilityResolver().from_settings(learning_enabled=True)
        ctx = HarnessContext(
            model="gemini-2.0-flash",
            provider="Google",
            channel="web",
            workspace="/test",
            learning_enabled=True,
            trust_mode="balanced",
            capabilities=caps,
            adapter="gemini",
        )
        prompt = HarnessCompiler().compile(ctx)
        self.assertIn("Learning Mode: enabled", prompt)
        self.assertIn("balanced", prompt)
        # The learning capability should be listed
        self.assertIn("learning", prompt.lower())

    def test_learning_disabled_excluded(self) -> None:
        caps = CapabilityResolver().from_settings(learning_enabled=False)
        ctx = HarnessContext(
            model="gpt-4o",
            provider="OpenAI",
            channel="web",
            workspace="/test",
            learning_enabled=False,
            capabilities=caps,
            adapter="openai",
        )
        prompt = HarnessCompiler().compile(ctx)
        self.assertIn("Learning Mode: disabled", prompt)
        # learning capability should NOT be in the available list
        # (it may appear in the static core text explaining what learning is)
        available_section = prompt.split("# Available GravityClaw Features")[1] if "# Available" in prompt else ""
        self.assertNotIn("Learn reusable procedural skills", available_section)


class TestDisabledCapability(unittest.TestCase):
    """Test 3 — Disabled features are explicitly marked unavailable."""

    def test_network_disabled(self) -> None:
        resolver = CapabilityResolver()
        runtime = RuntimeSnapshot(has_network=False, has_learning=False, has_skills=False)
        caps = resolver.resolve(runtime)
        ctx = HarnessContext(
            model="gpt-4o",
            provider="OpenAI",
            channel="web",
            workspace="/test",
            learning_enabled=False,
            capabilities=caps,
            adapter="openai",
        )
        prompt = HarnessCompiler().compile(ctx)
        # Network should be explicitly unavailable
        self.assertIn("Unavailable for this run", prompt)
        self.assertIn("network disabled", prompt.lower())
        # web_search and web_fetch should NOT be in the available list
        available_part = prompt.split("Unavailable")[0]
        self.assertNotIn("web_search", available_part)
        self.assertNotIn("web_fetch", available_part)

    def test_shell_disabled(self) -> None:
        resolver = CapabilityResolver()
        runtime = RuntimeSnapshot(has_shell=False)
        caps = resolver.resolve(runtime)
        ctx = HarnessContext(
            model="gpt-4o",
            provider="OpenAI",
            channel="web",
            workspace="/test",
            capabilities=caps,
            adapter="openai",
        )
        prompt = HarnessCompiler().compile(ctx)
        self.assertIn("Unavailable for this run", prompt)
        self.assertIn("shell", prompt.split("Unavailable")[1])


class TestModelSwitching(unittest.TestCase):
    """Test 4 — Different models produce different runtime sections but same identity."""

    def test_three_models_share_identity(self) -> None:
        compiler = HarnessCompiler()
        models = [
            ("claude-opus-4-20250514", "Anthropic", "anthropic"),
            ("gpt-4o", "OpenAI", "openai"),
            ("gemini-2.0-flash", "Google", "gemini"),
        ]
        prompts: list[str] = []
        for model, provider, adapter in models:
            caps = CapabilityResolver().from_settings(learning_enabled=True, channel="telegram")
            ctx = HarnessContext(
                model=model,
                provider=provider,
                channel="telegram",
                workspace="/data/gravityclaw",
                learning_enabled=True,
                trust_mode="balanced",
                capabilities=caps,
                adapter=adapter,
            )
            prompts.append(compiler.compile(ctx))

        # All three should have GravityClaw identity
        for prompt in prompts:
            self.assertIn("# GravityClaw", prompt)
            self.assertIn("Agent runtime: GravityClaw", prompt)

        # Each should declare its own model
        self.assertIn("claude-opus-4-20250514", prompts[0])
        self.assertIn("gpt-4o", prompts[1])
        self.assertIn("gemini-2.0-flash", prompts[2])

        # Each should declare its own provider
        self.assertIn("Provider: Anthropic", prompts[0])
        self.assertIn("Provider: OpenAI", prompts[1])
        self.assertIn("Provider: Google", prompts[2])

    def test_anthropic_adapter_adds_notes(self) -> None:
        caps = CapabilityResolver().from_settings(learning_enabled=True)
        ctx = HarnessContext(
            model="claude-opus-4-20250514",
            provider="Anthropic",
            channel="web",
            workspace="/test",
            capabilities=caps,
            adapter="anthropic",
        )
        prompt = HarnessCompiler().compile(ctx)
        self.assertIn("Provider notes", prompt)
        self.assertIn("Prefer available tools", prompt)

    def test_openai_adapter_no_extra_notes(self) -> None:
        caps = CapabilityResolver().from_settings(learning_enabled=True)
        ctx = HarnessContext(
            model="gpt-4o",
            provider="OpenAI",
            channel="web",
            workspace="/test",
            capabilities=caps,
            adapter="openai",
        )
        prompt = HarnessCompiler().compile(ctx)
        self.assertNotIn("Provider notes", prompt)


class TestSkillAwareness(unittest.TestCase):
    """Test 5 — Relevant skills appear as metadata summaries."""

    def test_skills_in_prompt(self) -> None:
        skills = (
            SkillSummary(
                skill_id="sk_001",
                name="systemd-gateway-recovery",
                description="Diagnose and recover a GravityClaw gateway managed with systemd.",
            ),
            SkillSummary(
                skill_id="sk_002",
                name="telegram-recovery",
                description="Diagnose polling, webhook and delivery failures.",
            ),
        )
        caps = CapabilityResolver().from_settings(learning_enabled=True)
        ctx = HarnessContext(
            model="gpt-4o",
            provider="OpenAI",
            channel="web",
            workspace="/test",
            capabilities=caps,
            skills=skills,
            adapter="openai",
        )
        prompt = HarnessCompiler().compile(ctx)
        self.assertIn("Relevant Skills", prompt)
        self.assertIn("systemd-gateway-recovery", prompt)
        self.assertIn("telegram-recovery", prompt)
        self.assertIn("skill_view", prompt)  # tells model how to get full content

    def test_no_skills_section_when_empty(self) -> None:
        caps = CapabilityResolver().from_settings(learning_enabled=True)
        ctx = HarnessContext(
            model="gpt-4o",
            provider="OpenAI",
            channel="web",
            workspace="/test",
            capabilities=caps,
            skills=(),
            adapter="openai",
        )
        prompt = HarnessCompiler().compile(ctx)
        self.assertNotIn("Relevant Skills", prompt)

    def test_skill_content_not_included(self) -> None:
        """Full skill SKILL.md content should NOT be in the harness prompt."""
        skills = (
            SkillSummary(
                skill_id="sk_001",
                name="test-skill",
                description="A test skill.",
            ),
        )
        caps = CapabilityResolver().from_settings(learning_enabled=True)
        ctx = HarnessContext(
            model="gpt-4o",
            provider="OpenAI",
            channel="web",
            workspace="/test",
            capabilities=caps,
            skills=skills,
            adapter="openai",
        )
        prompt = HarnessCompiler().compile(ctx)
        # Only metadata is present, not any full content
        lines = [line for line in prompt.split("\n") if "test-skill" in line]
        # Should be just the name line and description line
        self.assertLessEqual(len(lines), 2)


class TestActionAwareness(unittest.TestCase):
    """Test 6 — Shell available vs unavailable changes agent behavior framing."""

    def test_shell_available(self) -> None:
        resolver = CapabilityResolver()
        runtime = RuntimeSnapshot(has_shell=True)
        caps = resolver.resolve(runtime)
        ctx = HarnessContext(
            model="gpt-4o",
            provider="OpenAI",
            channel="web",
            workspace="/test",
            capabilities=caps,
            adapter="openai",
        )
        prompt = HarnessCompiler().compile(ctx)
        # shell should be in the available features section
        available_section = prompt.split("# Available GravityClaw Features")[1]
        if "Unavailable" in available_section:
            available_part = available_section.split("Unavailable")[0]
        else:
            available_part = available_section
        self.assertIn("shell", available_part)
        self.assertIn("Execute commands", available_part)

    def test_shell_unavailable(self) -> None:
        resolver = CapabilityResolver()
        runtime = RuntimeSnapshot(has_shell=False)
        caps = resolver.resolve(runtime)
        ctx = HarnessContext(
            model="gpt-4o",
            provider="OpenAI",
            channel="web",
            workspace="/test",
            capabilities=caps,
            adapter="openai",
        )
        prompt = HarnessCompiler().compile(ctx)
        # shell should be in the UNAVAILABLE section
        self.assertIn("Unavailable for this run", prompt)
        unavailable_section = prompt.split("Unavailable for this run")[1]
        self.assertIn("shell", unavailable_section)


class TestCapabilityResolver(unittest.TestCase):
    """Unit tests for the resolver itself."""

    def test_full_runtime(self) -> None:
        resolver = CapabilityResolver()
        runtime = RuntimeSnapshot(
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
            bound_skill_count=5,
            bound_mcp_count=2,
        )
        caps = resolver.resolve(runtime)
        names = {c.name for c in caps if c.available}
        self.assertIn("shell", names)
        self.assertIn("files", names)
        self.assertIn("web_search", names)
        self.assertIn("web_fetch", names)
        self.assertIn("persistent_memory", names)
        self.assertIn("scheduler", names)
        self.assertIn("delegation", names)
        self.assertIn("learning", names)
        self.assertIn("skills", names)
        self.assertIn("channel:telegram", names)
        self.assertIn("mcp_tools", names)
        # No unavailable caps
        unavailable = [c for c in caps if not c.available]
        self.assertEqual(len(unavailable), 0)

    def test_minimal_runtime(self) -> None:
        resolver = CapabilityResolver()
        runtime = RuntimeSnapshot(
            has_shell=False,
            has_file_access=False,
            has_web_search=False,
            has_web_fetch=False,
            has_memory=False,
            has_scheduler=False,
            has_delegation=False,
            has_learning=False,
            has_skills=False,
            channel="internal",
            has_network=False,
        )
        caps = resolver.resolve(runtime)
        available = [c for c in caps if c.available]
        unavailable = [c for c in caps if not c.available]
        # Only shell and network should be explicitly unavailable
        unavailable_names = {c.name for c in unavailable}
        self.assertIn("shell", unavailable_names)
        self.assertIn("network", unavailable_names)
        # Nothing valuable should be available
        available_names = {c.name for c in available}
        self.assertNotIn("web_search", available_names)
        self.assertNotIn("persistent_memory", available_names)

    def test_from_settings_convenience(self) -> None:
        resolver = CapabilityResolver()
        caps = resolver.from_settings(
            learning_enabled=True,
            has_scheduler=True,
            channel="web",
            network=True,
            bound_skills=3,
        )
        names = {c.name for c in caps if c.available}
        self.assertIn("shell", names)
        self.assertIn("web_search", names)
        self.assertIn("learning", names)
        self.assertIn("skills", names)


class TestAdapterDetection(unittest.TestCase):
    """Test detect_adapter covers known models."""

    def test_anthropic_models(self) -> None:
        self.assertEqual(detect_adapter("claude-opus-4-20250514"), "anthropic")
        self.assertEqual(detect_adapter("claude-3.5-sonnet"), "anthropic")
        self.assertEqual(detect_adapter("claude-haiku-3.5"), "anthropic")

    def test_openai_models(self) -> None:
        self.assertEqual(detect_adapter("gpt-4o"), "openai")
        self.assertEqual(detect_adapter("gpt-5.6-luna"), "openai")
        self.assertEqual(detect_adapter("o3-mini"), "openai")
        self.assertEqual(detect_adapter("o4-mini"), "openai")

    def test_gemini_models(self) -> None:
        self.assertEqual(detect_adapter("gemini-2.0-flash"), "gemini")
        self.assertEqual(detect_adapter("gemini-pro"), "gemini")

    def test_unknown_model(self) -> None:
        self.assertEqual(detect_adapter("llama-3-70b"), "generic")
        self.assertEqual(detect_adapter("mistral-large"), "generic")


class TestCompilerPromptSize(unittest.TestCase):
    """Sanity checks for prompt size — harness should stay compact."""

    def test_basic_prompt_under_4k(self) -> None:
        caps = CapabilityResolver().from_settings(learning_enabled=True, channel="web")
        ctx = HarnessContext(
            model="gpt-4o",
            provider="OpenAI",
            channel="web",
            workspace="/test",
            learning_enabled=True,
            trust_mode="strict",
            capabilities=caps,
            adapter="openai",
        )
        prompt = HarnessCompiler().compile(ctx)
        # The harness prompt should be compact — under 4KB
        self.assertLess(len(prompt), 4096)

    def test_with_skills_under_5k(self) -> None:
        skills = tuple(
            SkillSummary(
                skill_id=f"sk_{i:03d}",
                name=f"skill-{i}",
                description=f"Description for skill number {i} which does something useful.",
            )
            for i in range(8)
        )
        caps = CapabilityResolver().from_settings(learning_enabled=True, channel="telegram")
        ctx = HarnessContext(
            model="claude-opus-4-20250514",
            provider="Anthropic",
            channel="telegram",
            workspace="/data/gravityclaw",
            learning_enabled=True,
            trust_mode="balanced",
            capabilities=caps,
            skills=skills,
            adapter="anthropic",
        )
        prompt = HarnessCompiler().compile(ctx)
        # Even with 8 skills + anthropic adapter, should stay under 5KB
        self.assertLess(len(prompt), 5120)


if __name__ == "__main__":
    unittest.main()
