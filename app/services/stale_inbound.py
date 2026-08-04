"""Recording an inbound delivery that arrived too late to be answered.

A redelivery Meta held for forty minutes is a real customer message, so
discarding it would lose a record the business may need. But it is not a live
conversation opening, and answering it is what produced a welcome and a menu
arriving out of nowhere long after a session had been closed.

So this path splits the two things the normal handlers do together: it keeps
the PERSISTENCE and drops the REPLY.

* The message is claimed through the same ``ON CONFLICT DO NOTHING`` insert as
  live traffic, so it appears in the transcript an operator reads, and a
  further redelivery of the same id is a no-op rather than a second record.
* Nothing is sent. Not the welcome, not the menu, not a completion, and not a
  read receipt -- the customer sees no sign that anything happened, which is
  the whole requirement.

``welcome_sent_at`` is deliberately left untouched. Suppressing the greeting
here must not consume it: if this customer writes again for real, that message
is their conversation opening and it is owed a welcome. Recording one as sent
would silently rob them of it.
"""

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.events import conversation_activity, publish
from app.core.logging import get_logger
from app.core.metrics import DUPLICATE_DELIVERIES_TOTAL
from app.services.conversation_service import ConversationService

logger = get_logger(__name__)


async def record_without_answering(
    session: AsyncSession,
    settings: Settings,
    *,
    wa_id: str,
    name: str | None,
    wa_message_id: str,
    type: str,
    content: str | None,
    age: timedelta,
) -> None:
    """Persist a late delivery and answer it with silence.

    The log line is the diagnostic the incident needed and nobody had: it names
    the conversation, whether that conversation had already greeted anybody,
    how late the delivery was, and which mechanism delivered it. An unprompted
    welcome previously left no evidence distinguishing "the bot spoke by
    itself" from "a real message arrived very late", which is why the cause was
    invisible.
    """
    conversations = ConversationService(session, settings)
    _, conversation = await conversations.get_context(wa_id, name)
    claimed = await conversations.claim_inbound(
        conversation.id, wa_message_id, type=type, content=content
    )
    if claimed is None:
        # Already stored by a live attempt or an earlier redelivery. Rolling
        # back also discards any conversation get_context created a moment ago.
        await session.rollback()
        DUPLICATE_DELIVERIES_TOTAL.labels(stage="inbound_claim").inc()
        logger.info("duplicate_webhook_delivery", wa_message_id=wa_message_id)
        return

    await session.commit()

    logger.warning(
        "stale_inbound_not_answered",
        conversation_id=conversation.id,
        conversation_status=conversation.status,
        welcome_already_sent=conversation.welcome_sent_at is not None,
        trigger="webhook_delivery",
        function="record_without_answering",
        reason="delivery_older_than_inbound_max_age",
        age_seconds=int(age.total_seconds()),
        wa_message_id=wa_message_id,
        type=type,
    )

    # The dashboard should still show that something arrived, or an operator
    # reading the transcript later finds a message that apparently appeared
    # from nowhere. inbound=True is honest: a customer did send this.
    await publish(
        conversation_activity(conversation_id=conversation.id, inbound=True),
        settings,
    )
