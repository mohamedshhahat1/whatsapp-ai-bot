"""Message data access."""

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.message import (
    STATUS_PENDING,
    STATUS_SENT,
    Message,
)
from app.repositories.base import BaseRepository


class MessageRepository(BaseRepository):
    async def create(
        self,
        conversation_id: int,
        direction: str,
        type: str = "text",
        content: str | None = None,
        wa_message_id: str | None = None,
        media_id: str | None = None,
        status: str | None = None,
        reply_to_wa_message_id: str | None = None,
    ) -> Message:
        message = Message(
            conversation_id=conversation_id,
            direction=direction,
            type=type,
            content=content,
            wa_message_id=wa_message_id,
            media_id=media_id,
            status=status,
            reply_to_wa_message_id=reply_to_wa_message_id,
        )
        self.session.add(message)
        await self.session.flush()
        return message

    async def claim_inbound(
        self,
        conversation_id: int,
        wa_message_id: str,
        type: str = "text",
        content: str | None = None,
        media_id: str | None = None,
    ) -> int | None:
        """Atomically take ownership of one inbound delivery.

        Returns the new row id, or ``None`` if this message was already stored
        -- meaning another worker (or an earlier attempt of this one) owns it
        and this caller must do nothing at all.

        This replaces asking ``exists_by_wa_id`` and then inserting, which is a
        time-of-check-to-time-of-use race. Meta redelivers aggressively, and
        with several worker threads two attempts routinely overlap: both read
        "not present", both continue, both call OpenAI, both send a reply, and
        only then does the second one trip the unique index -- after the
        customer has seen two answers and both have been billed.

        INSERT ... ON CONFLICT DO NOTHING RETURNING id collapses that into one
        statement. Postgres serialises the two inserts on the unique index
        itself, so exactly one caller is handed an id and the loser is told
        before it spends anything.
        """
        statement = (
            pg_insert(Message)
            .values(
                conversation_id=conversation_id,
                wa_message_id=wa_message_id,
                direction="inbound",
                type=type,
                content=content,
                media_id=media_id,
            )
            .on_conflict_do_nothing(index_elements=["wa_message_id"])
            .returning(Message.id)
        )
        return await self.session.scalar(statement)

    async def reserve_reply(
        self,
        conversation_id: int,
        reply_to_wa_message_id: str,
        content: str,
        type: str = "text",
    ) -> int | None:
        """Book the right to answer one customer message, before answering it.

        Returns the new row id, or ``None`` if a reply to this message has
        already been reserved by someone else.

        The row is written with status ``pending`` and committed *before* the
        WhatsApp call, which is the deliberate part. WhatsApp's send endpoint
        has no idempotency key, so once the request leaves us we can never
        again know whether the customer received it. Writing the intention
        first means a crash mid-send leaves evidence: the retry finds a pending
        row and declines to send a second time.

        That trades a rare lost reply for a never-duplicated one. It is the
        right way round. A customer who gets no answer sends "?" and gets one;
        a customer who gets two different answers to one question, or two
        contradictory ones about a quotation, loses trust in the business --
        and every duplicate is a second OpenAI charge.
        """
        statement = (
            pg_insert(Message)
            .values(
                conversation_id=conversation_id,
                direction="outbound",
                type=type,
                content=content,
                status=STATUS_PENDING,
                reply_to_wa_message_id=reply_to_wa_message_id,
            )
            .on_conflict_do_nothing(index_elements=["reply_to_wa_message_id"])
            .returning(Message.id)
        )
        return await self.session.scalar(statement)

    async def confirm_reply(
        self,
        message_id: int,
        wa_message_id: str | None,
        status: str = STATUS_SENT,
    ) -> None:
        """Record that a reserved reply actually reached WhatsApp."""
        await self.session.execute(
            update(Message)
            .where(Message.id == message_id)
            .values(wa_message_id=wa_message_id, status=status)
        )

    async def reply_status(self, reply_to_wa_message_id: str) -> str | None:
        """Status of the existing reply to this inbound message, if any.

        ``None`` means nothing has been reserved yet. ``pending`` means a
        previous attempt got as far as the WhatsApp call and its outcome is
        unknown, which is the case a retry has to treat as "already sent".
        """
        return await self.session.scalar(
            select(Message.status).where(
                Message.reply_to_wa_message_id == reply_to_wa_message_id
            )
        )

    async def release_reservation(self, message_id: int) -> None:
        """Delete a reservation whose send failed outright.

        Only safe when the WhatsApp call raised before the request was made --
        a connection error, a refused DNS lookup, a 4xx rejection. In those
        cases nothing reached the customer, so freeing the reservation lets the
        retry answer properly instead of permanently declining to.

        Never called after an ambiguous failure such as a timeout: there the
        message may well have been delivered, and the reservation is exactly
        what stops a second copy.
        """
        message = await self.session.get(Message, message_id)
        if message is not None:
            await self.session.delete(message)

    async def exists_by_wa_id(self, wa_message_id: str) -> bool:
        result = await self.session.scalar(
            select(Message.id).where(Message.wa_message_id == wa_message_id)
        )
        return result is not None

    async def recent(self, conversation_id: int, limit: int = 50) -> list[Message]:
        result = await self.session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        return list(reversed(list(result)))

    async def update_status_by_wa_id(self, wa_message_id: str, status: str) -> None:
        await self.session.execute(
            update(Message)
            .where(Message.wa_message_id == wa_message_id)
            .values(status=status)
        )

    async def last_inbound_at(self, conversation_id: int) -> datetime | None:
        """Timestamp of the customer's most recent message.

        Used to check the WhatsApp 24-hour customer service window before an
        operator sends a free-form manual reply.
        """
        return await self.session.scalar(
            select(func.max(Message.created_at)).where(
                Message.conversation_id == conversation_id,
                Message.direction == "inbound",
            )
        )

    async def count_inbound(self, conversation_id: int) -> int:
        """How many messages the customer has sent in this conversation.

        Used to decide whether the message just stored is their first, which is
        what makes the welcome deterministic: the model cannot be trusted to
        notice, and the welcome must appear exactly once.
        """
        return int(
            await self.session.scalar(
                select(func.count(Message.id)).where(
                    Message.conversation_id == conversation_id,
                    Message.direction == "inbound",
                )
            )
            or 0
        )

    async def count(self) -> int:
        return int(await self.session.scalar(select(func.count(Message.id))) or 0)

    async def count_since(self, since: datetime) -> int:
        return int(
            await self.session.scalar(
                select(func.count(Message.id)).where(Message.created_at >= since)
            )
            or 0
        )
