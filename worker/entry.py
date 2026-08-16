#!/usr/bin/env python3
"""Durable container entrypoint that envelopes child stdout/stderr.

Each output record has a monotonically increasing source sequence. Podman keeps
container logs after gateway death, allowing reconciliation to replay only
records not yet committed to SQLite.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from typing import Any


sequence = 0
child: asyncio.subprocess.Process | None = None


def emit(source: str, **payload: Any) -> None:
    global sequence
    sequence += 1
    print(
        json.dumps(
            {"source_sequence": sequence, "source": source, **payload},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )


async def main() -> int:
    global child
    command = sys.argv[1:]
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        emit("worker", error="missing command", exit_code=127)
        return 127

    child = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    def forward(signum: int) -> None:
        if child is not None and child.returncode is None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, forward, signum)

    async def drain(source: str, stream: asyncio.StreamReader) -> None:
        while line := await stream.readline():
            emit(source, line=line.decode("utf-8", errors="replace").rstrip("\r\n"))

    assert child.stdout is not None
    assert child.stderr is not None
    await asyncio.gather(drain("stdout", child.stdout), drain("stderr", child.stderr))
    exit_code = await child.wait()
    emit("worker", exit_code=exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
