"""Channel-neutral contracts for GravityClaw transports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class InboundMessage:
    channel: str
    provider_update_id: int
    sender_id: str
    chat_id: str
    text: str
    provider_message_id: str | None = None
    thread_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def thread_key(self) -> str:
        return self.thread_id or ""


@dataclass(frozen=True, slots=True)
class ProviderMessage:
    message_id: str


@dataclass(frozen=True, slots=True)
class PolledUpdate:
    update_id: int
    message: InboundMessage | None


class ChannelDeliveryError(RuntimeError):
    """A definite provider rejection for which retry policy may be applied."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        retry_after: float = 0,
        already_applied: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.already_applied = already_applied


class AmbiguousDeliveryError(RuntimeError):
    """The provider may have accepted the request before the connection failed."""


class ChannelAdapter(Protocol):
    name: str

    async def poll(self, offset: int, timeout: int) -> list[PolledUpdate]: ...

    async def send_message(
        self, chat_id: str, text: str, *, thread_id: str | None = None
    ) -> ProviderMessage: ...

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> None: ...

    async def close(self) -> None: ...
