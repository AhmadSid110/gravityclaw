from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from gravityclaw.agy import AgyAdapter, AgyRunRequest, _normalize_event


ROOT = Path(__file__).parent


class NormalizationTests(unittest.TestCase):
    def test_success_fixture_normalizes_expected_events(self) -> None:
        events = []
        for line in (ROOT / "fixtures" / "success.ndjson").read_text().splitlines():
            if not line:
                continue
            import json

            raw = json.loads(line)
            payload = raw.get(raw["event"], {})
            conversation_id = payload.get("conversation_id") or raw.get("conversation_id")
            events.append(
                _normalize_event(raw, run_id="run", conversation_id=conversation_id)
            )

        self.assertEqual(
            [event.type for event in events],
            [
                "agent.started",
                "agent.step",
                "message.delta",
                "message.delta",
                "tool.started",
                "tool.finished",
                "agent.completed",
            ],
        )
        self.assertTrue(all(event.conversation_id == "conv-123" for event in events))

    def test_unknown_event_is_preserved(self) -> None:
        raw = {"event": "future_event", "future_event": {"answer": 42}}
        event = _normalize_event(raw, run_id="run", conversation_id=None)
        self.assertEqual(event.type, "backend.event")
        self.assertEqual(event.raw, raw)

    def test_tool_error_is_not_misreported_as_started(self) -> None:
        raw = {
            "event": "step_update",
            "step_update": {
                "conversation_id": "conv-denied",
                "step_type": "tool",
                "state": "ERROR",
                "tool_name": "run_command",
                "tool_info": {"error": {"message": "User denied permission"}},
            },
        }
        event = _normalize_event(
            raw, run_id="run", conversation_id="conv-denied"
        )
        self.assertEqual(event.type, "tool.failed")

    def test_subagent_step_is_normalized_and_preserved(self) -> None:
        raw = {
            "event": "step_update",
            "step_update": {
                "conversation_id": "parent",
                "step_type": "subagent",
                "state": "ACTIVE",
                "subagent_info": {
                    "subagents": [{"role": "verifier", "conversation_id": "child"}]
                },
            },
        }
        event = _normalize_event(raw, run_id="run", conversation_id="parent")
        self.assertEqual(event.type, "subagent.updated")
        self.assertEqual(event.raw, raw)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.adapter = AgyAdapter(ROOT / "fake_agy.py")

    def request(self, prompt: str, **kwargs: object) -> AgyRunRequest:
        return AgyRunRequest(
            prompt=prompt,
            cwd=ROOT,
            **kwargs,
        )

    def test_resume_command_is_explicit(self) -> None:
        command = self.adapter.build_command(
            self.request("hello", conversation_id="conv-resume")
        )
        self.assertIn("--conversation", command)
        self.assertEqual(command[command.index("--conversation") + 1], "conv-resume")
        self.assertNotIn("--continue", command)

    async def test_subprocess_stream_and_stderr_are_drained(self) -> None:
        run = await self.adapter.start(self.request("hello"))
        events = [event async for event in run.events()]
        self.assertIn("message.delta", [event.type for event in events])
        self.assertIn("backend.diagnostic", [event.type for event in events])
        self.assertIn("agent.completed", [event.type for event in events])
        self.assertEqual(run.process.returncode, 0)

    async def test_malformed_stream_is_reported(self) -> None:
        run = await self.adapter.start(self.request("malformed"))
        events = [event async for event in run.events()]
        errors = [event for event in events if event.type == "backend.protocol_error"]
        self.assertGreaterEqual(len(errors), 2)
        self.assertEqual(run.process.returncode, 2)

    async def test_cancel_stops_process_group(self) -> None:
        run = await self.adapter.start(self.request("hang"))

        async def collect() -> list[str]:
            return [event.type async for event in run.events()]

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0.1)
        await run.cancel(grace_seconds=0.5)
        event_types = await asyncio.wait_for(collector, timeout=2)
        self.assertIn("agent.interrupted", event_types)
        self.assertIsNotNone(run.process.returncode)

    async def test_cancel_overrides_backend_error_result(self) -> None:
        run = await self.adapter.start(self.request("cancel-error"))

        async def collect() -> list[str]:
            return [event.type async for event in run.events()]

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0.1)
        await run.cancel(grace_seconds=0.5)
        event_types = await asyncio.wait_for(collector, timeout=2)
        self.assertIn("agent.interrupted", event_types)

    async def test_wall_timeout_stops_pre_result_stall(self) -> None:
        run = await self.adapter.start(
            self.request("hang", wall_timeout_seconds=0.1)
        )
        events = [event async for event in run.events()]
        self.assertIn("backend.timeout", [event.type for event in events])
        self.assertIsNotNone(run.process.returncode)

    async def test_independent_runs_can_execute_concurrently(self) -> None:
        async def run_one(prompt: str) -> str:
            run = await self.adapter.start(self.request(prompt))
            events = [event async for event in run.events()]
            completed = next(event for event in events if event.type == "agent.completed")
            return str(completed.data["response"])

        results = await asyncio.gather(run_one("one"), run_one("two"))
        self.assertEqual(results, ["one", "two"])


if __name__ == "__main__":
    unittest.main()
