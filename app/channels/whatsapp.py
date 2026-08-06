"""WhatsApp adapter: the existing Cloud API client behind the shared contract.

Wraps :class:`WhatsAppClient` rather than replacing it. Every byte that
reaches Meta is still composed by that client, so the live WhatsApp path
sends exactly what it sent before this file existed -- the adapter only gives
shared code a uniform way to reach it.

Inbound is deliberately not routed here. WhatsApp webhooks still flow through
``app/services/webhook_processor.py``, which predates the adapter architecture
and is the one inbound path proven in production. Offering a second
normalisation of the same payload would create two ways for a customer message
to become an event, and two ways drift. ``parse`` therefore refuses; when the
inbound path is migrated it will be implemented here and the processor will
delegate to it.
"""

from collections.abc import Iterable
from typing import Any

from app.channels.base import BaseChannelAdapter
from app.channels.constants import WHATSAPP
from app.channels.events import InboundEvent
from app.channels.registry import register_adapter
from app.integrations.whatsapp import WhatsAppClient


@register_adapter
class WhatsAppAdapter(BaseChannelAdapter):
    """The shared outbound contract, served by the existing WhatsApp client."""

    channel = WHATSAPP

    def __init__(self, client: WhatsAppClient) -> None:
        """Takes the client, not Settings.

        ``deps.get_whatsapp_client`` is an ``lru_cache`` singleton holding one
        httpx connection pool. Building a client from Settings here would open
        a second pool against the same endpoint on every operator reply.
        """
        self._client = client

    # --- Inbound ------------------------------------------------------------

    def parse(self, payload: dict[str, Any]) -> Iterable[InboundEvent]:
        """Not implemented here; see the module docstring."""
        raise NotImplementedError(
            "WhatsApp inbound is normalised by app/services/webhook_processor.py"
        )

    # --- Outbound -----------------------------------------------------------

    async def send_text(self, recipient: str, text: str) -> dict[str, Any]:
        """Send plain text. Truncation and payload shape stay in the client."""
        return await self._client.send_text(recipient, text)

    async def send_quick_replies(
        self,
        recipient: str,
        text: str,
        options: list[tuple[str, str]],
    ) -> dict[str, Any]:
        """Reply buttons, which is what WhatsApp calls quick replies.

        An empty ``options`` degrades to plain text rather than raising:
        ``send_buttons`` rejects an empty list, and shared code should not
        have to check before calling.
        """
        if not options:
            return await self._client.send_text(recipient, text)
        return await self._client.send_buttons(recipient, text, list(options))

    async def mark_as_read(self, provider_message_id: str) -> None:
        """Best-effort read receipt; the client already swallows failures."""
        await self._client.mark_as_read(provider_message_id)

    async def aclose(self) -> None:
        """Deliberately does not close the client.

        It is the process-wide singleton and other callers still hold it.
        Closing it here would break the next request's send.
        """
        return None
