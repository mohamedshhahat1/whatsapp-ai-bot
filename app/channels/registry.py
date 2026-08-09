"""Which channels are on, which are actually usable, and who serves them.

Enabled is not the same as ready
--------------------------------
A flag set without the credentials behind it is the failure mode this module
exists to make visible. Left alone it surfaces as a 400 from the Graph API on
the first real customer message -- by which point the customer is already
waiting. ``readiness`` answers the same question at boot, where a log line or
a health check can be read before anyone is affected.

Adapters register themselves rather than being imported here. A registry that
imported every adapter would need editing for each new channel -- the exact
change this architecture exists to avoid -- and it would drag the Instagram
HTTP client into the WhatsApp worker's import graph for nothing.
"""

from app.channels.base import BaseChannelAdapter
from app.channels.config import ChannelSettings, get_channel_settings
from app.channels.constants import (
    FACEBOOK_COMMENT,
    INSTAGRAM_COMMENT,
    INSTAGRAM_DM,
    MESSENGER,
    WHATSAPP,
    is_known,
)

#: What each channel needs from ChannelSettings before it can send anything.
#: WhatsApp is empty on purpose: Settings.REQUIRED_IN_PRODUCTION already
#: guards its four credentials, and restating them here would be a second
#: copy to keep true.
REQUIRED_CREDENTIALS: dict[str, tuple[str, ...]] = {
    WHATSAPP: (),
    MESSENGER: ("facebook_page_id", "facebook_page_access_token"),
    INSTAGRAM_DM: ("instagram_account_id", "instagram_token"),
    FACEBOOK_COMMENT: ("facebook_page_id", "facebook_page_access_token"),
    INSTAGRAM_COMMENT: ("instagram_account_id", "instagram_token"),
}

#: The channels that arrive on the shared ``/webhook/meta`` route. WhatsApp is
#: absent deliberately: it has its own route with its own verify token, and
#: keeping it out means turning every Meta surface off cannot silence it.
META_CHANNELS: frozenset[str] = frozenset(
    {MESSENGER, INSTAGRAM_DM, FACEBOOK_COMMENT, INSTAGRAM_COMMENT}
)

#: Which private-message channel each Meta webhook ``object`` carries.
#:
#: Meta subscribes one URL for the whole app and names the surface in the
#: envelope's ``object`` field: ``page`` is Messenger, ``instagram`` is
#: Instagram DM. The two share a ``messaging`` array and little else, so the
#: object is the only safe thing to route on -- inferring the surface from the
#: array's shape would attribute an Instagram conversation to Messenger, and
#: ``conversations.channel`` is what every analytics figure is grouped by.
#:
#: Comment surfaces arrive on these same two objects under ``changes`` rather
#: than ``messaging``, so they are resolved separately. See docs/CHANNELS.md.
META_DM_CHANNELS: dict[str, str] = {
    "page": MESSENGER,
    "instagram": INSTAGRAM_DM,
}

#: Which comment channel each Meta webhook ``object`` carries.
#:
#: The same two objects as ``META_DM_CHANNELS``, and that is the whole
#: difficulty: a ``page`` delivery is Messenger when an entry carries
#: ``messaging`` and Facebook comments when it carries ``changes``, and one
#: delivery can carry both. So an object does not identify a channel here --
#: it identifies a pair of them, and which one a given entry belongs to is
#: settled by the array it actually contains.
#:
#: Kept as a second mapping rather than a tuple value in META_DM_CHANNELS
#: because every caller wants one surface or the other, never both unpacked
#: together: the DM adapter reads ``messaging`` and ignores ``changes``, and
#: the comment adapter does the reverse.
META_COMMENT_CHANNELS: dict[str, str] = {
    "page": FACEBOOK_COMMENT,
    "instagram": INSTAGRAM_COMMENT,
}

_ADAPTERS: dict[str, type[BaseChannelAdapter]] = {}


def register_adapter(adapter: type[BaseChannelAdapter]) -> type[BaseChannelAdapter]:
    """Register an adapter class for its channel. Usable as a decorator."""
    channel = getattr(adapter, "channel", "")
    if not is_known(channel):
        raise ValueError(f"{adapter.__name__} has no known channel id: {channel!r}")
    _ADAPTERS[channel] = adapter
    return adapter


def adapter_class(channel: str) -> type[BaseChannelAdapter] | None:
    """The adapter registered for ``channel``, if its module has been imported."""
    return _ADAPTERS.get(channel)


def is_enabled(channel: str, settings: ChannelSettings | None = None) -> bool:
    """Whether ``channel`` is switched on. Unknown ids are never enabled."""
    resolved = settings or get_channel_settings()
    return resolved.switches.get(channel, False)


def enabled_channels(settings: ChannelSettings | None = None) -> frozenset[str]:
    """Every channel currently switched on."""
    resolved = settings or get_channel_settings()
    return frozenset(c for c, on in resolved.switches.items() if on)


def meta_dm_channel(object_type: str) -> str | None:
    """The private-message channel a Meta ``object`` delivers, if this app
    serves one at all.
    """
    return META_DM_CHANNELS.get(object_type)


def meta_comment_channel(object_type: str) -> str | None:
    """The comment channel a Meta ``object`` delivers, if this app serves one.

    A truthy answer does not mean the delivery contains a comment. It means
    that if this delivery carries ``changes``, that is the channel they belong
    to.
    """
    return META_COMMENT_CHANNELS.get(object_type)


def meta_channels_for_object(object_type: str) -> tuple[str, ...]:
    """Every channel a Meta ``object`` can deliver, private surface first.

    A single ``page`` delivery may carry a Messenger message and a comment on
    a post, in separate entries or in the same one. Handing it to one channel
    would file half of it under the wrong surface, and comment-to-DM
    conversion is measured by telling those two halves apart -- so the caller
    is given both, and each adapter takes the array it understands.

    Order is fixed rather than incidental: dispatch follows it, so two
    surfaces in one delivery are always processed in the same sequence and a
    test can assert what happened without depending on dict iteration.

    Says nothing about whether either channel is switched on, configured, or
    has an adapter. ``is_enabled``, ``missing_credentials`` and
    ``adapter_class`` answer those, and the caller must still ask.
    """
    resolved = (meta_dm_channel(object_type), meta_comment_channel(object_type))
    return tuple(channel for channel in resolved if channel is not None)


def any_meta_channel_enabled(settings: ChannelSettings | None = None) -> bool:
    """Whether any surface served by the shared Meta webhook is switched on.

    The route uses this to decide whether a delivery is worth parsing at all: a
    deployment with every Meta channel off should not spend CPU on a body it
    will discard, and must not answer 4xx either, because Meta retries anything
    that is not a 200 for hours.
    """
    resolved = settings or get_channel_settings()
    return any(resolved.switches.get(channel, False) for channel in META_CHANNELS)


def missing_credentials(
    channel: str, settings: ChannelSettings | None = None
) -> tuple[str, ...]:
    """Which credentials ``channel`` needs and does not have."""
    resolved = settings or get_channel_settings()
    missing: list[str] = []
    for name in REQUIRED_CREDENTIALS.get(channel, ()):
        attr = getattr(resolved, name)
        # instagram_token is a method, because it falls back to the page
        # token; the plain credentials are attributes.
        value = attr() if callable(attr) else attr
        if not str(value).strip():
            missing.append(name)
    return tuple(missing)


def readiness(settings: ChannelSettings | None = None) -> dict[str, tuple[str, ...]]:
    """Enabled channels that cannot send, and what each one is missing.

    Empty dict means every switched-on channel has what it needs. Intended
    for a startup log line and the health endpoint -- somewhere a person sees
    it before a customer does.
    """
    resolved = settings or get_channel_settings()
    report: dict[str, tuple[str, ...]] = {}
    for channel in sorted(enabled_channels(resolved)):
        missing = missing_credentials(channel, resolved)
        if missing:
            report[channel] = missing
    return report


def usable_channels(settings: ChannelSettings | None = None) -> frozenset[str]:
    """Channels that are both switched on and fully configured."""
    resolved = settings or get_channel_settings()
    unusable = readiness(resolved).keys()
    return frozenset(enabled_channels(resolved) - unusable)
