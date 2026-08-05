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
