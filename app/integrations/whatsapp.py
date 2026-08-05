"""WhatsApp Cloud API client (Meta Graph API) with transient-failure retries."""

from typing import Any

import httpx

from app.config import Settings
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.core.retry import http_retry

logger = get_logger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com"

# WhatsApp's own limits on interactive messages. Exceeding any one of them is
# a 400 from the Graph API, which would surface as a failed customer reply, so
# they are clamped here rather than trusted to every caller.
BODY_MAX = 1024
FOOTER_MAX = 60
BUTTON_TITLE_MAX = 20
MAX_BUTTONS = 3


class WhatsAppClient:
    """Thin async client over the WhatsApp Cloud API messages endpoint."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=GRAPH_API_BASE + "/" + settings.whatsapp_api_version,
            headers={"Authorization": "Bearer " + settings.whatsapp_token},
            timeout=30.0,
        )

    @http_retry()
    async def _send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Single attempt; tenacity retries transient failures (429/5xx/network)."""
        url = "/" + self._settings.whatsapp_phone_number_id + "/messages"
        response = await self._client.post(url, json=payload)
        response.raise_for_status()
        return response.json()

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send with retries, translating exhausted failures to a domain error."""
        try:
            return await self._send(payload)
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

    async def send_buttons(
        self,
        to: str,
        body: str,
        buttons: list[tuple[str, str]],
        footer: str | None = None,
    ) -> dict[str, Any]:
        """Send up to three reply buttons.

        ``buttons`` is a list of ``(selection_id, title)``. The id comes back
        verbatim in the inbound webhook, which is why callers route on it and
        never on the title -- see app/services/menu.py.
        """
        if not buttons:
            raise ValueError("send_buttons requires at least one button")
        interactive: dict[str, Any] = {
            "type": "button",
            "body": {"text": body[:BODY_MAX]},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {"id": bid, "title": title[:BUTTON_TITLE_MAX]},
                    }
                    for bid, title in buttons[:MAX_BUTTONS]
                ]
            },
        }
        if footer:
            interactive["footer"] = {"text": footer[:FOOTER_MAX]}
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "interactive",
                "interactive": interactive,
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
