"""Schemas for the knowledge-base admin endpoints."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class KnowledgeDocumentRead(BaseModel):
    """An indexed source document."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    title: str
    chunk_count: int
    updated_at: datetime


class KnowledgeSearchHit(BaseModel):
    """One retrieval result, for debugging answer quality."""

    source: str
    score: float
    content: str
