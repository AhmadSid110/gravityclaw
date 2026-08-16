"""Small live probe for the installed AGY stream protocol."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .agy import AgyAdapter, AgyRunRequest


async def _run(args: argparse.Namespace) -> int:
    adapter = AgyAdapter(args.agy)
    request = AgyRunRequest(
        prompt=args.prompt,
        cwd=args.workspace.resolve(),
        conversation_id=args.conversation,
        print_timeout=args.timeout,
        wall_timeout_seconds=args.wall_timeout,
        sandbox=args.sandbox,
        allow_all=args.allow_all,
    )
    run = await adapter.start(request)
    exit_code = 0
    async for event in run.events():
        print(
            json.dumps(
                {
                    "type": event.type,
                    "run_id": event.run_id,
                    "conversation_id": event.conversation_id,
                    "data": event.data,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if event.type in {"agent.failed", "backend.protocol_error"}:
            exit_code = 1
    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt")
    parser.add_argument("--agy", default="agy")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--conversation")
    parser.add_argument("--timeout", default="2m")
    parser.add_argument(
        "--wall-timeout",
        type=float,
        default=30.0,
        help="hard gateway timeout in seconds, including authentication startup",
    )
    parser.add_argument("--sandbox", action="store_true")
    parser.add_argument("--allow-all", action="store_true")
    raise SystemExit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
