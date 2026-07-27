"""Base repository holding the shared async session."""

from sqlalchemy.ext.asyncio import AsyncSession


class BaseRepository:
    """All repositories operate on a request-scoped AsyncSession."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
