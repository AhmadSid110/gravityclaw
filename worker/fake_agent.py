#!/usr/bin/env python3
"""Deterministic AGY-shaped worker used by the forced-crash acceptance test."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path


def emit(value: dict[str, object]) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


def step(conversation_id: str, index: int, step_type: str, **values: object) -> None:
    emit(
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": conversation_id,
                "step_index": index,
                "step_type": step_type,
                **values,
            },
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario")
    parser.add_argument("--conversation")
    parser.add_argument("--delay", type=float)
    parser.add_argument("--forbidden-path")
    args = parser.parse_args()
    conversation_id = args.conversation or str(uuid.uuid4())
    emit(
        {
            "event": "init",
            "conversation_id": conversation_id,
            "init": {"cwd": str(Path.cwd()), "permission_mode": "test"},
        }
    )
    step(conversation_id, 0, "user_input", state="DONE")

    if args.scenario == "text":
        delay = args.delay if args.delay is not None else 0.4
        for index, text in enumerate(("alpha ", "beta ", "gamma"), start=1):
            time.sleep(delay)
            step(
                conversation_id,
                index,
                "agent_response",
                state="DONE",
                text_delta=text,
            )
        response = "alpha beta gamma"
        status = "SUCCESS"
    elif args.scenario == "long-command":
        delay = args.delay if args.delay is not None else 8
        step(
            conversation_id,
            1,
            "tool",
            state="ACTIVE",
            tool_name="run_command",
            tool_info={"name": "run_command", "parameters": {"CommandLine": f"sleep {delay}"}},
        )
        subprocess.run(["sleep", str(delay)], check=True)
        step(
            conversation_id,
            1,
            "tool",
            state="DONE",
            tool_name="run_command",
            tool_info={"name": "run_command", "output": ""},
        )
        response = "command complete"
        status = "SUCCESS"
    elif args.scenario == "subagent":
        delay = args.delay if args.delay is not None else 6
        child_id = str(uuid.uuid4())
        step(
            conversation_id,
            1,
            "subagent",
            state="ACTIVE",
            subagent_info={"subagents": [{"role": "tester", "conversation_id": child_id}]},
        )
        time.sleep(delay)
        step(
            conversation_id,
            1,
            "subagent",
            state="DONE",
            subagent_info={"subagents": [{"role": "tester", "conversation_id": child_id}]},
        )
        response = "subagent complete"
        status = "SUCCESS"
    elif args.scenario == "tool-failure":
        time.sleep(args.delay if args.delay is not None else 3)
        step(
            conversation_id,
            1,
            "tool",
            state="ERROR",
            tool_name="run_command",
            tool_info={"error": {"message": "deterministic tool failure"}},
        )
        response = ""
        status = "ERROR"
    elif args.scenario == "isolation":
        if not args.forbidden_path:
            raise ValueError("isolation scenario requires --forbidden-path")
        leaked = Path(args.forbidden_path).exists()
        response = "isolation-failed" if leaked else "isolation-ok"
        status = "ERROR" if leaked else "SUCCESS"
    else:
        response = args.scenario
        status = "SUCCESS"

    step(
        conversation_id,
        99,
        "agent_response",
        state="DONE",
        text_delta=response,
    )
    emit(
        {
            "event": "result",
            "result": {
                "conversation_id": conversation_id,
                "status": status,
                "response": response,
            },
        }
    )
    return 0 if status == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
