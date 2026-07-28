"""AI usage log data access."""

from sqlalchemy import func, select

from app.models.ai_log import AILog
from app.repositories.base import BaseRepository


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
    ) -> AILog:
        log = AILog(
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
