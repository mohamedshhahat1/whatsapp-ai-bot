"""User data access."""

from sqlalchemy import func, select

from app.models.user import User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository):
    async def get_by_wa_id(self, wa_id: str) -> User | None:
        return await self.session.scalar(select(User).where(User.wa_id == wa_id))

    async def get_or_create(self, wa_id: str, name: str | None = None) -> User:
        user = await self.get_by_wa_id(wa_id)
        if user is not None:
            if name and user.name != name:
                user.name = name
            return user
        user = User(wa_id=wa_id, name=name)
        self.session.add(user)
        await self.session.flush()
        return user

    async def list(self, offset: int = 0, limit: int = 50) -> list[User]:
        result = await self.session.scalars(
            select(User).order_by(User.created_at.desc()).offset(offset).limit(limit)
        )
        return list(result)

    async def count(self) -> int:
        return int(await self.session.scalar(select(func.count(User.id))) or 0)
