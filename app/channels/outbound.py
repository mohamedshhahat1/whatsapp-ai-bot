"""Reaching a customer on the channel they actually wrote from.

Operator replies used to hold a ``WhatsAppClient`` and refuse everyone else.
The refusal was honest but permanent: nothing about a manual reply is
WhatsApp-shaped, only the transport was. This module resolves that transport
from ``conversations.channel``, so the reply path asks for "the way to reach
this person" and never asks which app they use.

Construction is the one place channels legitimately differ -- a WhatsApp
adapter wraps the shared Cloud API client, a Messenger adapter builds its own
transport from channel settings -- so it happens here, once, behind a single
function. Callers upstream stay free of channel conditionals, which is the
whole point of the adapter architecture.
"""

from typing import Any

from app.channels.base import BaseChannelAdapter
from app.channels.config import ChannelSettings, get_channel_settings
from app.channels.constants import WHATSAPP, is_known
from app.channels.registry import (
    adapter_class,
    is_enabled,
    meta_dm_channel,
    missing_credentials,
)
from app.core.exceptions import ConflictError
from app.core.logging import get_logger
from app.integrations.whatsapp import WhatsAppClient
from app.models.user import User

logger = get_logger(__name__)


class ChannelUnavailableError(ConflictError):
    """Raised when nothing in this deployment can carry a message.

    A 409 rather than a 500, for the same reason as the other conflict errors
    in the reply path: nothing is broken. The channel is switched off, or it
    is switched on without the credentials it needs, or no adapter for it has
    been written yet. All three are deployment states an operator can be told
    about, and none of them are exceptions in the programming sense.
    """

    code = "channel_unavailable"


def _register_adapters() -> None:
    """Import the adapter modules for their registration side effect.

    Inside a function rather than at module scope, following the rule
    registry.py sets out: a top-level import here would drag every channel's
    HTTP client into the import graph of everything that resolves an adapter,
    including the WhatsApp worker.
    """
    from app.channels import instagram, messenger, whatsapp  # noqa: F401


def recipient_id(user: User) -> str | None:
    """The id to address this customer by on their own channel.

    ``external_id`` first, ``wa_id`` as the fallback, which is correct on both
    sides of the 0009 expand/contract migration: a WhatsApp row written before
    the writers were updated has a phone number and no ``external_id``, while
    a Messenger row has a PSID and no phone number at all. Returns None for a
    row carrying neither, which callers must treat as unaddressable rather
    than sending to the empty string.
    """
    return user.external_id or user.wa_id


def outbound_adapter(
    channel: str,
    *,
    whatsapp_client: WhatsAppClient,
    settings: ChannelSettings | None = None,
) -> BaseChannelAdapter:
    """The adapter that can send on ``channel``, ready to use.

    Raises :class:`ChannelUnavailableError` rather than returning None, so no
    caller can forget to check and end up sending into the void.
    """
    if not is_known(channel):
        raise ChannelUnavailableError(f"Unknown channel: {channel}")

    resolved = settings or get_channel_settings()

    if not is_enabled(channel, resolved):
        raise ChannelUnavailableError(
            f"The {channel} channel is switched off in this deployment."
        )

    missing = missing_credentials(channel, resolved)
    if missing:
        # Logged at error because this is a misconfiguration a deployer needs
        # to see, not a customer-shaped problem: the switch was turned on and
        # the credentials behind it were never supplied.
        logger.error(
            "outbound_channel_misconfigured",
            channel=channel,
            missing=list(missing),
        )
        raise ChannelUnavailableError(
            f"The {channel} channel is enabled but not configured."
        )

    _register_adapters()
    cls = adapter_class(channel)
    if cls is None:
        raise ChannelUnavailableError(
            f"No outbound adapter is implemented for {channel} yet."
        )

    # The single channel conditional in the outbound path, and it is about
    # construction rather than behaviour: WhatsApp reuses the shared client,
    # everything else builds its own transport from channel settings.
    if channel == WHATSAPP:
        return cls(whatsapp_client)  # type: ignore[call-arg]
    return cls(resolved)  # type: ignore[call-arg]


def meta_inbound_adapter(
    object_type: str,
    *,
    settings: ChannelSettings | None = None,
) -> BaseChannelAdapter | None:
    """The adapter that parses and answers a Meta delivery of ``object_type``.

    A sibling of :func:`outbound_adapter` rather than a branch inside it,
    because the two differ in the one way that matters at the call site: this
    one returns None where that one raises. Every caller here is serving a
    webhook, and an unavailable channel has to end in a 200 -- Meta retries
    anything else for hours, and a raised error in a Celery task is retried
    five more times for a delivery that will never become servable.

    None therefore covers all four ordinary states: an object this app does not
    serve, a switched-off channel, a channel switched on without its
    credentials, and a channel whose adapter is not written yet. Only the third
    is logged loudly, because it is the only one a deployer needs to fix.

    No WhatsApp branch: WhatsApp does not arrive on this route.
    """
    channel = meta_dm_channel(object_type)
    if channel is None:
        return None

    resolved = settings or get_channel_settings()

    if not is_enabled(channel, resolved):
        return None

    missing = missing_credentials(channel, resolved)
    if missing:
        logger.error(
            "meta_inbound_channel_misconfigured",
            channel=channel,
            missing=list(missing),
        )
        return None

    _register_adapters()
    cls = adapter_class(channel)
    if cls is None:
        return None
    return cls(resolved)  # type: ignore[call-arg]


def provider_message_id(response: dict[str, Any]) -> str | None:
    """The platform's id for a message just sent, whichever shape it came in.

    WhatsApp answers ``{"messages": [{"id": ...}]}``; Messenger and Instagram
    both answer ``{"message_id": ...}``. Reading only the first shape -- which
    the reply path did -- stores NULL for every Messenger reply, and the
    provider id is what the delivery-status and idempotency paths key on.
    """
    sent = response.get("messages")
    if isinstance(sent, list) and sent:
        first = sent[0]
        if isinstance(first, dict) and first.get("id"):
            return str(first["id"])
    candidate = response.get("message_id")
    return str(candidate) if candidate else None
