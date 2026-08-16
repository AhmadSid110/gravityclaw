#!/usr/bin/env python3
"""Authenticated live stress probes for the official AGY CLI.

These probes deliberately retain only event metadata and never inspect AGY's
credential store. Run them from the GravityClaw repository with PYTHONPATH=src.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import tempfile
import time
from collections import Counter
from pathlib import Path

from gravityclaw.agy import AgyAdapter, AgyRunRequest


async def cancellation(binary: str) -> dict[str, object]:
    adapter = AgyAdapter(binary)
    with tempfile.TemporaryDirectory(prefix="gravityclaw-cancel-") as directory:
        run = await adapter.start(
            AgyRunRequest(
                prompt=(
                    "Use run_command to execute exactly: sleep 120. "
                    "Do not use any other tool and wait for it to finish."
                ),
                cwd=Path(directory),
                print_timeout="3m",
                wall_timeout_seconds=45,
                sandbox=True,
                allow_all=True,
            )
        )
        types: list[str] = []
        terminal_status: str | None = None
        canceled_on_tool = False
        started = time.monotonic()
        async for event in run.events():
            types.append(event.type)
            if event.type in {"agent.completed", "agent.failed", "agent.interrupted"}:
                terminal_status = str(event.data.get("status"))
            if event.type == "tool.started" and not canceled_on_tool:
                canceled_on_tool = True
                await run.cancel(grace_seconds=2)
        elapsed = time.monotonic() - started
        return {
            "scenario": "cancellation",
            "canceled_on_tool": canceled_on_tool,
            "elapsed_seconds": round(elapsed, 3),
            "exit_code": run.process.returncode,
            "terminal_status": terminal_status,
            "event_counts": dict(Counter(types)),
            "passed": canceled_on_tool
            and elapsed < 30
            and "agent.interrupted" in types,
        }


async def parallel(binary: str) -> dict[str, object]:
    adapter = AgyAdapter(binary)
    with tempfile.TemporaryDirectory(prefix="gravityclaw-parallel-") as root:
        root_path = Path(root)
        workspaces = [root_path / "alpha", root_path / "beta"]
        for workspace in workspaces:
            workspace.mkdir()

        async def run_one(index: int) -> dict[str, object]:
            request = AgyRunRequest(
                prompt=f"Reply with exactly: parallel-{index}",
                cwd=workspaces[index],
                print_timeout="2m",
                wall_timeout_seconds=60,
            )
            run = await adapter.start(request)
            events = [event async for event in run.events()]
            started_event = next(event for event in events if event.type == "agent.started")
            completed = next(event for event in events if event.type == "agent.completed")
            return {
                "conversation_id": completed.conversation_id,
                "cwd": started_event.data.get("cwd"),
                "response": str(completed.data.get("response", "")).strip(),
                "exit_code": run.process.returncode,
            }

        started = time.monotonic()
        results = await asyncio.gather(run_one(0), run_one(1))
        elapsed = time.monotonic() - started
        distinct_conversations = len({item["conversation_id"] for item in results}) == 2
        correct_workspaces = {
            item["cwd"] for item in results
        } == {str(path) for path in workspaces}
        correct_responses = [item["response"] for item in results] == [
            "parallel-0",
            "parallel-1",
        ]
        return {
            "scenario": "parallel",
            "elapsed_seconds": round(elapsed, 3),
            "results": results,
            "passed": distinct_conversations and correct_workspaces and correct_responses,
        }


async def killed_stream(binary: str) -> dict[str, object]:
    """Simulate a backend crash after init and verify truncation detection."""
    adapter = AgyAdapter(binary)
    with tempfile.TemporaryDirectory(prefix="gravityclaw-kill-") as directory:
        run = await adapter.start(
            AgyRunRequest(
                prompt="Use run_command to execute exactly: sleep 120.",
                cwd=Path(directory),
                print_timeout="3m",
                wall_timeout_seconds=45,
                sandbox=True,
                allow_all=True,
            )
        )
        types: list[str] = []
        killed = False
        async for event in run.events():
            types.append(event.type)
            if event.type == "tool.started" and not killed:
                os.killpg(run.process.pid, signal.SIGKILL)
                killed = True
        return {
            "scenario": "killed_stream",
            "killed_on_tool": killed,
            "exit_code": run.process.returncode,
            "event_counts": dict(Counter(types)),
            "passed": killed and "backend.protocol_error" in types,
        }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=("cancellation", "parallel", "killed-stream"))
    parser.add_argument("--agy", default="agy")
    args = parser.parse_args()
    if args.scenario == "cancellation":
        result = await cancellation(args.agy)
    elif args.scenario == "parallel":
        result = await parallel(args.agy)
    else:
        result = await killed_stream(args.agy)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
