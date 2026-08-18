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
                caption = message.get("caption")
                # Accept text messages, photo messages, and document messages
                has_content = isinstance(text, str) or message.get("photo") or message.get("document")
                if isinstance(sender, dict) and isinstance(chat, dict) and has_content:
                    # Build attachment metadata from photo/document
                    attachments: list[dict[str, Any]] = []
                    if message.get("photo"):
                        # Telegram sends array of sizes; take the largest
                        photos = message["photo"]
                        if isinstance(photos, list) and photos:
                            best = photos[-1]
                            attachments.append({
                                "type": "photo",
                                "file_id": best.get("file_id"),
                                "file_unique_id": best.get("file_unique_id"),
                                "width": best.get("width"),
                                "height": best.get("height"),
                                "file_size": best.get("file_size"),
                            })
                    if message.get("document"):
                        doc = message["document"]
                        if isinstance(doc, dict):
                            attachments.append({
                                "type": "document",
                                "file_id": doc.get("file_id"),
                                "file_unique_id": doc.get("file_unique_id"),
                                "file_name": doc.get("file_name"),
                                "mime_type": doc.get("mime_type"),
                                "file_size": doc.get("file_size"),
                            })
                    if message.get("audio"):
                        audio = message["audio"]
                        if isinstance(audio, dict):
                            attachments.append({
                                "type": "audio",
                                "file_id": audio.get("file_id"),
                                "file_unique_id": audio.get("file_unique_id"),
                                "file_name": audio.get("file_name"),
                                "mime_type": audio.get("mime_type"),
                                "file_size": audio.get("file_size"),
                                "duration": audio.get("duration"),
                            })
                    if message.get("voice"):
                        voice = message["voice"]
                        if isinstance(voice, dict):
                            attachments.append({
                                "type": "voice",
                                "file_id": voice.get("file_id"),
                                "file_unique_id": voice.get("file_unique_id"),
                                "mime_type": voice.get("mime_type"),
                                "file_size": voice.get("file_size"),
                                "duration": voice.get("duration"),
                            })
                    if message.get("video"):
                        video = message["video"]
                        if isinstance(video, dict):
                            attachments.append({
                                "type": "video",
                                "file_id": video.get("file_id"),
                                "file_unique_id": video.get("file_unique_id"),
                                "file_name": video.get("file_name"),
                                "mime_type": video.get("mime_type"),
                                "file_size": video.get("file_size"),
                                "width": video.get("width"),
                                "height": video.get("height"),
                                "duration": video.get("duration"),
                            })
                    # Use text or caption as the message content
                    effective_text = text if isinstance(text, str) else (caption or "")
                    # Only create a message if there's text or attachments
                    if effective_text or attachments:
                        payload_data: dict[str, Any] = {
                            "date": message.get("date"),
                            "chat_type": chat.get("type"),
                        }
                        if attachments:
                            payload_data["attachments"] = attachments
                        normalized = InboundMessage(
                            channel=self.name,
                            provider_update_id=update_id,
                            sender_id=str(sender.get("id", "")),
                            chat_id=str(chat.get("id", "")),
                            text=effective_text,
                            provider_message_id=str(message.get("message_id", "")) or None,
                            thread_id=(
                                str(message["message_thread_id"])
                                if message.get("message_thread_id") is not None
                                else None
                            ),
                            payload=payload_data,
                        )
            updates.append(PolledUpdate(update_id, normalized))
        return updates

    async def get_file_url(self, file_id: str) -> str:
        """Get the download URL for a Telegram file."""
        result = await self._call("getFile", {"file_id": file_id}, ambiguous=False)
        if not isinstance(result, dict) or not result.get("file_path"):
            raise ChannelDeliveryError("Telegram returned no file path", retryable=True)
        file_path = result["file_path"]
        return f"{self._base_url.rsplit('/bot', 1)[0]}/file/bot{self._base_url.rsplit('/bot', 1)[1]}/{file_path}"

    async def download_file(self, file_id: str) -> tuple[bytes, str]:
        """Download a file from Telegram. Returns (data, file_path)."""
        result = await self._call("getFile", {"file_id": file_id}, ambiguous=False)
        if not isinstance(result, dict) or not result.get("file_path"):
            raise ChannelDeliveryError("Telegram returned no file path", retryable=True)
        file_path = str(result["file_path"])
        # Extract token from base_url for file download
        token_part = self._base_url.rsplit("/bot", 1)[1] if "/bot" in self._base_url else ""
        api_root = self._base_url.rsplit("/bot", 1)[0] if "/bot" in self._base_url else self._base_url
        download_url = f"{api_root}/file/bot{token_part}/{file_path}"
        try:
            response = await self._client.get(download_url, timeout=60)
            response.raise_for_status()
            return response.content, file_path
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            raise ChannelDeliveryError(
                f"Failed to download Telegram file: {exc}", retryable=True
            ) from exc

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
