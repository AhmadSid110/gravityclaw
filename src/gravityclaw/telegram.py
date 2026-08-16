"""Telegram Bot API transport adapter."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .channels import (
    AmbiguousDeliveryError,
    ChannelDeliveryError,
    InboundMessage,
    PolledUpdate,
    ProviderMessage,
)


class TelegramAdapter:
    name = "telegram"

    def __init__(
        self,
        token: str,
        client: httpx.AsyncClient | None = None,
        *,
        api_root: str = "https://api.telegram.org",
    ) -> None:
        if not token.strip():
            raise ValueError("Telegram bot token must not be empty")
        self._base_url = f"{api_root.rstrip('/')}/bot{token}"
        # Telegram embeds the credential in the URL path. Suppress HTTP client
        # request logging so third-party logger configuration cannot print it.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self._client = client or httpx.AsyncClient(trust_env=False)
        self._owns_client = client is None

    async def poll(self, offset: int, timeout: int) -> list[PolledUpdate]:
        value = await self._call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["message"]},
            ambiguous=False,
            request_timeout=timeout + 10,
        )
        updates: list[PolledUpdate] = []
        for raw in value if isinstance(value, list) else []:
            if not isinstance(raw, dict) or not isinstance(raw.get("update_id"), int):
                continue
            update_id = int(raw["update_id"])
            message = raw.get("message")
            normalized: InboundMessage | None = None
            if isinstance(message, dict):
                sender = message.get("from")
                chat = message.get("chat")
                text = message.get("text")
                if isinstance(sender, dict) and isinstance(chat, dict) and isinstance(text, str):
                    normalized = InboundMessage(
                        channel=self.name,
                        provider_update_id=update_id,
                        sender_id=str(sender.get("id", "")),
                        chat_id=str(chat.get("id", "")),
                        text=text,
                        provider_message_id=str(message.get("message_id", "")) or None,
                        thread_id=(
                            str(message["message_thread_id"])
                            if message.get("message_thread_id") is not None
                            else None
                        ),
                        payload={
                            "date": message.get("date"),
                            "chat_type": chat.get("type"),
                        },
                    )
            updates.append(PolledUpdate(update_id, normalized))
        return updates

    async def send_message(
        self, chat_id: str, text: str, *, thread_id: str | None = None
    ) -> ProviderMessage:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if thread_id:
            payload["message_thread_id"] = thread_id
        result = await self._call("sendMessage", payload, ambiguous=True)
        if not isinstance(result, dict) or result.get("message_id") is None:
            raise ChannelDeliveryError("Telegram returned no message id", retryable=True)
        return ProviderMessage(str(result["message_id"]))

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> None:
        await self._call(
            "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": text},
            ambiguous=True,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        ambiguous: bool,
        request_timeout: float = 30,
    ) -> Any:
        try:
            response = await self._client.post(
                f"{self._base_url}/{method}", json=payload, timeout=request_timeout
            )
            value = response.json()
        except (httpx.TransportError, ValueError) as exc:
            if ambiguous:
                raise AmbiguousDeliveryError(
                    "Telegram acknowledgement was not received"
                ) from exc
            raise ChannelDeliveryError("Telegram request failed", retryable=True) from exc
        if not isinstance(value, dict) or not value.get("ok"):
            description = str(value.get("description", "Telegram rejected the request"))
            parameters = value.get("parameters")
            retry_after = (
                float(parameters.get("retry_after", 0))
                if isinstance(parameters, dict)
                else 0
            )
            error_code = int(value.get("error_code", response.status_code))
            raise ChannelDeliveryError(
                description,
                retryable=error_code == 429 or error_code >= 500,
                retry_after=retry_after,
                already_applied="message is not modified" in description.lower(),
            )
        return value.get("result")
