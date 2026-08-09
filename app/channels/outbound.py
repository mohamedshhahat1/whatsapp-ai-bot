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
    meta_channels_for_object,
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

    One plain import per module rather than a single ``from`` list. Adding a
    fourth name to that list pushes it past 88 characters, and the
    parenthesised form black then produces puts each name on its own line --
    where the shared ``# noqa: F401`` no longer covers any of them, because
    ruff matches a noqa to the line its diagnostic starts on. One import per
    line keeps every suppression next to the thing it suppresses, and leaves
    room for the channels still to come.

    Every channel id in ``constants.ALL_CHANNELS`` now has a line here. A
    shipped adapter that is missing from this function is invisible in
    production while looking finished in the tests, because the test fixtures
    pin the registry directly; tests/test_outbound_routing.py therefore reads
    this function's source and asserts each module appears.
    """
    import app.channels.facebook_comments  # noqa: F401
    import app.channels.instagram  # noqa: F401
    import app.channels.instagram_comments  # noqa: F401
    import app.channels.messenger  # noqa: F401
    import app.channels.whatsapp  # noqa: F401


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


def _meta_adapter(
    channel: str,
    resolved: ChannelSettings,
) -> BaseChannelAdapter | None:
    """The adapter for one Meta channel, or None if it cannot serve traffic.

    Three ordinary states are folded into a single None: the channel is
    switched off, it is switched on without the credentials it needs, or no
    adapter has been written for it yet. Only the middle one is logged loudly,
    because it is the only one a deployer has to fix.

    Shared by both resolvers below so the four-step check exists once. Takes
    already-resolved settings rather than resolving its own, so a delivery
    carrying two surfaces reads configuration a single time and cannot see two
    different answers within one payload.
    """
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


def meta_inbound_adapter(
    object_type: str,
    *,
    settings: ChannelSettings | None = None,
) -> BaseChannelAdapter | None:
    """The adapter for the PRIVATE surface of a Meta delivery, if any.

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

    Answers about the DM surface alone, because ``meta_dm_channel`` is what it
    asks. Callers serving a whole delivery want :func:`meta_inbound_adapters`
    instead: comments arrive on these same objects under ``changes``, and this
    function cannot see them.

    No WhatsApp branch: WhatsApp does not arrive on this route.
    """
    channel = meta_dm_channel(object_type)
    if channel is None:
        return None
    return _meta_adapter(channel, settings or get_channel_settings())


def meta_inbound_adapters(
    object_type: str,
    *,
    settings: ChannelSettings | None = None,
) -> list[BaseChannelAdapter]:
    """Every adapter that can serve a Meta delivery of ``object_type``.

    One delivery can carry two surfaces: a ``page`` envelope holds Messenger
    messages under ``messaging`` and Facebook comments under ``changes``, and
    ``instagram`` splits the same way. Each adapter parses only the array it
    understands and ignores the other, so handing the same payload to both is
    what makes a mixed delivery attributable, rather than half of it being
    filed under whichever surface resolved first.

    Ordered private-surface-first, following ``meta_channels_for_object``. In
    the Celery task that order is load-bearing: a failure there is retried, and
    processing is idempotent, so answering DMs before comments means a
    persistently failing comment surface cannot hold up a customer's message.

    An empty list means this deployment cannot serve the delivery at all --
    an unrecognised object, or one whose surfaces are all switched off,
    unconfigured, or not yet implemented. Callers must treat that as ordinary:
    a webhook answers 200 either way.

    Every adapter returned owns an HTTP client, and the caller must close all
    of them -- including when one of the others raises.
    """
    resolved = settings or get_channel_settings()
    adapters: list[BaseChannelAdapter] = []
    for channel in meta_channels_for_object(object_type):
        adapter = _meta_adapter(channel, resolved)
        if adapter is not None:
            adapters.append(adapter)
    return adapters


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
