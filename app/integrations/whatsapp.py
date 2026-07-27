"""WhatsApp Cloud API client (Meta Graph API)."""

from typing import Any

import httpx

from app.config import Settings
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger

logger = get_logger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com"


class WhatsAppClient:
    """Thin async client over the WhatsApp Cloud API messages endpoint."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=GRAPH_API_BASE + "/" + settings.whatsapp_api_version,
            headers={"Authorization": "Bearer " + settings.whatsapp_token},
            timeout=30.0,
        )

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = "/" + self._settings.whatsapp_phone_number_id + "/messages"
        try:
            response = await self._client.post(url, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "whatsapp_api_error",
                status_code=exc.response.status_code,
                body=exc.response.text[:500],
            )
            raise ExternalServiceError("WhatsApp API request failed") from exc
        except httpx.HTTPError as exc:
            logger.error("whatsapp_network_error", error=str(exc))
            raise ExternalServiceError("WhatsApp API unreachable") from exc
        return response.json()

    async def send_text(self, to: str, text: str) -> dict[str, Any]:
        """Send a plain text message."""
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": text[:4096]},
            }
        )

    async def send_image(
        self,
        to: str,
        link: str | None = None,
        media_id: str | None = None,
        caption: str | None = None,
    ) -> dict[str, Any]:
        """Send an image by public URL or uploaded media id."""
        image: dict[str, Any] = {"link": link} if link else {"id": media_id}
        if caption:
            image["caption"] = caption
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "image",
                "image": image,
            }
        )

    async def send_document(
        self,
        to: str,
        link: str | None = None,
        media_id: str | None = None,
        filename: str | None = None,
        caption: str | None = None,
    ) -> dict[str, Any]:
        """Send a document by public URL or uploaded media id."""
        document: dict[str, Any] = {"link": link} if link else {"id": media_id}
        if filename:
            document["filename"] = filename
        if caption:
            document["caption"] = caption
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "document",
                "document": document,
            }
        )

    async def mark_as_read(self, message_id: str) -> None:
        """Mark an inbound message as read (sends the read receipt)."""
        try:
            await self._post(
                {
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": message_id,
                }
            )
        except ExternalServiceError:
            # Read receipts are best-effort; never fail message handling on them.
            logger.warning("mark_as_read_failed", message_id=message_id)

    async def aclose(self) -> None:
        await self._client.aclose()
