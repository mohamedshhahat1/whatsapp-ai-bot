"""SQLAlchemy models.

Importing them here is what registers every table on ``Base.metadata``, which
is how ``alembic/env.py`` sees the full schema for autogenerate diffs.
"""

from app.models.ai_log import AILog
from app.models.conversation import Conversation
from app.models.document import Document, DocumentChunk
from app.models.message import Message
from app.models.model_pricing import ModelPricing
from app.models.user import User

__all__ = [
    "AILog",
    "Conversation",
    "Document",
    "DocumentChunk",
    "Message",
    "ModelPricing",
    "User",
]
