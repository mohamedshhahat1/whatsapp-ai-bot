"""SQLAlchemy 2.0 declarative base shared by all models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base; Alembic autogenerate targets its metadata."""
