"""Message data access."""

from datetime import datetime

from sqlalchemy import func, select, update

from app.models.message import Message
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
    ) -> Message:
        message = Message(
            conversation_id=conversation_id,
            direction=direction,
            type=type,
            content=content,
            wa_message_id=wa_message_id,
            media_id=media_id,
            status=status,
        )
        self.session.add(message)
        await self.session.flush()
        return message

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
