"""The outbound contract every channel implements.

The signature that matters
--------------------------
``send_text(recipient, text) -> dict`` is deliberately identical to
``ChatService.Sender``. An adapter is therefore already a valid sender, which
is what lets a later phase hand ChatService an adapter instead of a
WhatsAppClient without touching the reservation and confirmation bookkeeping,
the welcome logic, the handoff or the session lifecycle. The alternative --
a channel argument threaded through the orchestration -- would put a fork in
every one of those paths.

Degrade, do not branch
----------------------
``send_quick_replies`` falls back to ``send_text`` here, once, so a channel
without tappable replies needs no code and no caller needs to ask whether it
has them. Same bargain the WhatsApp buttons already make when the Graph API
refuses them: an answered customer beats a pretty one.

``mark_as_read`` is a no-op by default for the same reason -- read receipts
are best-effort everywhere and absent on some surfaces.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any, ClassVar

from app.channels.constants import ChannelProfile, profile
from app.channels.events import InboundEvent


class BaseChannelAdapter(ABC):
    """Translates one platform's envelopes to and from the shared pipeline.

    Subclasses own exactly two things: how a payload becomes InboundEvents,
    and how text leaves the process. Everything else -- what to say, whether
    to say it, whether a person should say it instead -- lives in the services
    and is shared.
    """

    #: Set by each subclass to one of the ids in ``constants``.
    channel: ClassVar[str]

    @property
    def profile(self) -> ChannelProfile:
        """What this channel supports. Data, not branches."""
        return profile(self.channel)

    # --- Inbound ------------------------------------------------------------

    @abstractmethod
    def parse(self, payload: dict[str, Any]) -> Iterable[InboundEvent]:
        """Turn one webhook delivery into normalised events.

        One delivery can carry several messages from several people, so this
        yields rather than returning one. Anything unroutable is dropped here
        rather than passed on as a half-filled event.
        """

    # --- Outbound -----------------------------------------------------------

    @abstractmethod
    async def send_text(self, recipient: str, text: str) -> dict[str, Any]:
        """Send plain text. Returns the platform's response envelope.

        Must raise ``ExternalServiceError`` on failure, as the WhatsApp client
        does: ``_send_once`` distinguishes a send that failed from one that
        never happened, and marks the reserved reply unconfirmed rather than
        losing it outright.
        """

    async def send_quick_replies(
        self,
        recipient: str,
        text: str,
        options: list[tuple[str, str]],
    ) -> dict[str, Any]:
        """Send text with tappable options, where the channel has them.

        Default: send the text alone. ``options`` are (selection_id, title)
        pairs, and the id is what comes back on a tap.
        """
        if not self.profile.supports_quick_replies:
            return await self.send_text(recipient, text)
        raise NotImplementedError(
            f"{type(self).__name__} claims quick replies but does not implement them"
        )

    async def mark_as_read(self, provider_message_id: str) -> None:
        """Acknowledge an inbound message. Best-effort, no-op by default."""
        return None

    async def aclose(self) -> None:
        """Release any transport held open. No-op by default."""
        return None


class CommentChannelAdapter(BaseChannelAdapter):
    """A public comment thread: answer in the open, continue in private.

    Split from the base class because these two operations exist only here,
    and putting them on every adapter would invite a DM path to call
    ``reply_to_comment`` on something that has no comments.

    Whether the private invitation is sent at all is a configuration
    decision, not an adapter one -- some pages may not message a commenter,
    and the business may simply not want to. The adapter only knows how.
    """

    @abstractmethod
    async def reply_to_comment(self, comment_id: str, text: str) -> dict[str, Any]:
        """Post a public reply underneath the customer's comment."""

    @abstractmethod
    async def invite_to_private_thread(
        self, comment_id: str, text: str
    ) -> dict[str, Any]:
        """Open a private thread with the commenter, where allowed.

        Platform rules decide whether this is permitted, and they differ
        between Facebook and Instagram. A refusal is ordinary traffic, not an
        incident: the public reply has already answered the customer, so
        callers log it and move on.
        """
