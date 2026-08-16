"""Database-backed event notification bus.

SQLite is the source of truth. The bus only wakes subscribers, so reconnecting
clients reconstruct state from persisted sequence numbers rather than memory.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict


class EventBus:
    def __init__(self) -> None:
        self._versions: dict[str, int] = defaultdict(int)
        self._condition = asyncio.Condition()

    async def notify(self, run_id: str) -> None:
        async with self._condition:
            self._versions[run_id] += 1
            self._condition.notify_all()

    async def wait(self, run_id: str, version: int, timeout: float = 15.0) -> int:
        async with self._condition:
            if self._versions[run_id] != version:
                return self._versions[run_id]
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(
                        lambda: self._versions[run_id] != version
                    ),
                    timeout=timeout,
                )
            except TimeoutError:
                pass
            return self._versions[run_id]

    def version(self, run_id: str) -> int:
        return self._versions[run_id]
