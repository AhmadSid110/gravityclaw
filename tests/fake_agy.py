#!/usr/bin/env python3
"""Deterministic subprocess fixture implementing the relevant AGY CLI shape."""

from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path


def raise_exit() -> None:
    raise SystemExit(130)


def emit(value: dict[str, object]) -> None:
    print(json.dumps(value), flush=True)


args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
conversation = (
    args[args.index("--conversation") + 1]
    if "--conversation" in args
    else "fixture-conversation"
)

if prompt == "malformed":
    print("not-json", flush=True)
    raise SystemExit(2)

emit({"event": "init", "conversation_id": conversation, "init": {"cwd": str(Path.cwd())}})

if prompt in {"hang", "cancel-error"}:
    if prompt == "cancel-error":
        def emit_error_and_exit(*_: object) -> None:
            emit(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": conversation,
                        "status": "ERROR",
                        "response": "interrupted",
                    },
                }
            )
            raise_exit()

        signal.signal(signal.SIGINT, emit_error_and_exit)
    else:
        signal.signal(signal.SIGINT, lambda *_: raise_exit())
    while True:
        time.sleep(0.1)

emit(
    {
        "event": "step_update",
        "step_update": {
            "conversation_id": conversation,
            "step_index": 1,
            "state": "DONE",
            "step_type": "agent_response",
            "text_delta": prompt,
        },
    }
)
print("fixture diagnostic", file=sys.stderr, flush=True)
emit(
    {
        "event": "result",
        "result": {
            "conversation_id": conversation,
            "status": "SUCCESS",
            "response": prompt,
        },
    }
)
