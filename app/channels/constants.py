"""The channels this platform speaks, and what each one can actually do.

Why capabilities are data
-------------------------
The channels differ in small, awkward ways: Messenger and Instagram take
tappable replies, a public comment takes none; a DM has a session with a
welcome and a goodbye, a comment thread has neither. Writing those
differences as ``if channel == ...`` at each call site is how five channels
turn into five forks of the same pipeline -- the exact outcome this work is
meant to avoid. They are attributes on a ChannelProfile instead, so shared
code asks what a channel *supports* and never asks which channel it *is*.

Why the ids are a contract
--------------------------
These strings get written to ``conversations.channel``, filtered on by the
dashboard and the Flutter app, and grouped by in analytics. Renaming one does
not migrate the rows already written, so a rename silently splits a channel's
history in two. Same rule as the selection ids in app/services/menu.py:
append-only, never reworded.

The longest id is ``instagram_comment`` at 17 characters. The column that
will store it is String(24), leaving room for a sibling like
``whatsapp_status`` without another migration.

Comments and their private sibling
----------------------------------
A comment is public and cannot hold a private conversation. Answering it in
the open and continuing in a DM is two channels, not one, so the invitation
is expressed as data -- ``private_sibling`` -- rather than as a special case
that has to be remembered in the Facebook comment path and again in the
Instagram one.
"""

from dataclasses import dataclass

# Channel ids. APPEND-ONLY -- see the module docstring.
WHATSAPP = "whatsapp"
MESSENGER = "messenger"
INSTAGRAM_DM = "instagram_dm"
FACEBOOK_COMMENT = "facebook_comment"
INSTAGRAM_COMMENT = "instagram_comment"

#: Widest id allowed, and therefore the width of conversations.channel.
CHANNEL_ID_MAX = 24


@dataclass(frozen=True)
class ChannelProfile:
    """What shared code is allowed to assume about one channel."""

    id: str
    #: Shown to operators in the dashboard and the Flutter app.
    label: str
    #: The dot operators scan a list by. Kept beside the label so the two
    #: cannot drift apart between the two clients.
    icon: str
    #: A private thread with one customer. False for public comment threads,
    #: where anyone can read the reply.
    private: bool
    #: Whether a session -- welcome, idle timeout, goodbye -- means anything
    #: here. A public comment gets none of it: there is no session to open,
    #: and greeting a comment thread would read as the bot talking to itself.
    has_session: bool
    #: Tappable replies. The platforms name them differently (reply buttons,
    #: quick replies) but the shape shared code needs is identical:
    #: (selection_id, title) pairs that come back as a stable id.
    supports_quick_replies: bool
    #: Whether the bot can send images and documents outbound.
    supports_media_out: bool
    #: How long after the customer's last message the platform allows a free
    #: reply. Every Meta surface is 24 hours; it lives here so send paths
    #: read it off the profile instead of hardcoding the number.
    reply_window_hours: int
    #: Where a public thread continues privately, when platform rules and the
    #: customer's own settings allow it.
    private_sibling: str | None = None


PROFILES: dict[str, ChannelProfile] = {
    WHATSAPP: ChannelProfile(
        id=WHATSAPP,
        label="WhatsApp",
        icon="\U0001f7e2",
        private=True,
        has_session=True,
        supports_quick_replies=True,
        supports_media_out=True,
        reply_window_hours=24,
    ),
    MESSENGER: ChannelProfile(
        id=MESSENGER,
        label="Messenger",
        icon="\U0001f535",
        private=True,
        has_session=True,
        supports_quick_replies=True,
        supports_media_out=True,
        reply_window_hours=24,
    ),
    INSTAGRAM_DM: ChannelProfile(
        id=INSTAGRAM_DM,
        label="Instagram DM",
        icon="\U0001f7e3",
        private=True,
        has_session=True,
        supports_quick_replies=True,
        supports_media_out=True,
        reply_window_hours=24,
    ),
    FACEBOOK_COMMENT: ChannelProfile(
        id=FACEBOOK_COMMENT,
        label="Facebook Comment",
        icon="\U0001f537",
        private=False,
        has_session=False,
        supports_quick_replies=False,
        supports_media_out=False,
        reply_window_hours=24,
        private_sibling=MESSENGER,
    ),
    INSTAGRAM_COMMENT: ChannelProfile(
        id=INSTAGRAM_COMMENT,
        label="Instagram Comment",
        icon="\U0001f49c",
        private=False,
        has_session=False,
        supports_quick_replies=False,
        supports_media_out=False,
        reply_window_hours=24,
        private_sibling=INSTAGRAM_DM,
    ),
}

ALL_CHANNELS: tuple[str, ...] = tuple(PROFILES)
PRIVATE_CHANNELS = frozenset(c for c, p in PROFILES.items() if p.private)
PUBLIC_CHANNELS = frozenset(c for c, p in PROFILES.items() if not p.private)


def is_known(channel: str) -> bool:
    """Whether this id is one we route."""
    return channel in PROFILES


def profile(channel: str) -> ChannelProfile:
    """The profile for ``channel``.

    Raises rather than returning a default. A mistyped channel id that
    quietly resolved to WhatsApp-ish defaults would send a customer the wrong
    thing down the wrong transport -- worse than a loud failure in the worker,
    where the retry and the error counter can both see it.
    """
    try:
        return PROFILES[channel]
    except KeyError:
        raise ValueError(f"unknown channel: {channel!r}") from None


def private_sibling(channel: str) -> str | None:
    """Where a public thread on ``channel`` can be continued privately."""
    return profile(channel).private_sibling
