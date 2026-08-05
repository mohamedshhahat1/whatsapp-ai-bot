"""The normalised inbound event every channel is reduced to.

One shape, so everything below the adapter is written once. A WhatsApp
``wamid``, a Messenger ``mid`` and an Instagram comment id are all just
``provider_message_id`` here, which means the deduplication, freshness and
quota guards that already key on that value keep working untouched.

What is deliberately NOT in this model
--------------------------------------
Nothing platform-shaped survives normalisation: no ``entry``/``changes``
nesting, no ``interactive.type``, no Graph envelope. A field that cannot be
filled in for every channel does not belong here -- it belongs in
``context``, which is opaque to everything except the adapter that produced
it and the adapter that will answer it. A comment id and its post id travel
there, because replying to a comment needs them and nothing else does.

Fail-open timestamps
--------------------
``age`` returns None when the platform sent nothing usable, matching
``webhook_processor._message_age``. A format change on Meta's side must not
silence every reply the bot makes, and treating an unreadable timestamp as
fresh is the safe direction to be wrong in.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

# What the customer did. Mirrors the handlers ChatService already exposes, so
# routing a normalised event stays a lookup rather than a second
# interpretation of the payload.
EVENT_TEXT = "text"
EVENT_MEDIA = "media"
EVENT_SELECTION = "selection"
EVENT_UNSUPPORTED = "unsupported"

EVENT_KINDS = frozenset({EVENT_TEXT, EVENT_MEDIA, EVENT_SELECTION, EVENT_UNSUPPORTED})


@dataclass(frozen=True)
class InboundEvent:
    """One thing one customer did, on one channel.

    Frozen because it crosses a queue boundary and is read by several stages.
    An event editable in flight would make "what did the customer actually
    send?" unanswerable from the logs.
    """

    channel: str
    #: The platform's id for this person: wa_id, PSID, IGSID. Unique only
    #: within its own channel, which is why identity is keyed on the pair and
    #: never on this alone.
    sender_id: str
    #: wamid / mid / comment id. Carries the deduplication guarantee.
    provider_message_id: str
    kind: str
    #: Display name where the platform offers one. Often absent on Instagram,
    #: so nothing may depend on it being set.
    sender_name: str | None = None
    #: EVENT_TEXT.
    text: str | None = None
    #: EVENT_SELECTION -- a tapped button or quick reply. The id is what
    #: routes; see app/services/menu.py for why the title never does.
    selection_id: str | None = None
    selection_title: str | None = None
    #: EVENT_MEDIA.
    media_type: str | None = None
    media_id: str | None = None
    media_url: str | None = None
    caption: str | None = None
    #: When the platform says the customer sent it.
    sent_at: datetime | None = None
    #: Adapter-private routing data -- comment id, post id, parent comment.
    #: Opaque to the shared pipeline by design.
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def age(self) -> timedelta | None:
        """How long ago this was sent, or None when it cannot be known."""
        if self.sent_at is None:
            return None
        return datetime.now(UTC) - self.sent_at

    @property
    def body(self) -> str:
        """What to store in the transcript for this event.

        Defined once, so a stale delivery, a live reply and the dashboard all
        describe the same message the same way.
        """
        if self.kind == EVENT_TEXT:
            return self.text or ""
        if self.kind == EVENT_SELECTION:
            return self.selection_title or self.selection_id or ""
        if self.kind == EVENT_MEDIA:
            return self.caption or f"[{self.media_type or 'media'} received]"
        return "[unsupported message received]"

    @property
    def routable(self) -> bool:
        """Whether there is enough here to answer at all.

        Without a sender there is nobody to reply to; without a message id
        there is nothing to make the reply idempotent against -- and that is
        the guard stopping a redelivery from becoming a second answer.
        """
        return bool(self.sender_id and self.provider_message_id)
