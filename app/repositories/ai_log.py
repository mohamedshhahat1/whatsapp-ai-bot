"""AI usage log data access."""

from sqlalchemy import func, select

from app.models.ai_log import AILog
from app.models.conversation import Conversation
from app.repositories.base import BaseRepository
from app.repositories.tenant import resolve_tenant_id


class AILogRepository(BaseRepository):
    async def create(
        self,
        model: str,
        conversation_id: int | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        latency_ms: int = 0,
        error: str | None = None,
        *,
        tenant_id: int | None = None,
    ) -> AILog:
        """Record one model call.

        The tenant is inherited from the conversation the call was made for,
        which is what keeps spend attributable to whoever is billed for it. A
        call with no conversation -- an embedding run, a health probe, anything
        the admin API triggers directly -- falls back to the deployment's
        original tenant until Phase 1c supplies a real context.

        Note that the column is filled here rather than read back from the
        conversation later. ``conversation_id`` is ON DELETE SET NULL by
        design, so a deleted conversation must not take its cost record's
        attribution with it.
        """
        owner = tenant_id
        if owner is None and conversation_id is not None:
            owner = await self.session.scalar(
                select(Conversation.tenant_id).where(
                    Conversation.id == conversation_id
                )
            )
        log = AILog(
            tenant_id=await resolve_tenant_id(self.session, owner),
            model=model,
            conversation_id=conversation_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            error=error,
        )
        self.session.add(log)
        await self.session.flush()
        return log

    async def total_tokens(self) -> int:
        return int(await self.session.scalar(select(func.sum(AILog.total_tokens))) or 0)
