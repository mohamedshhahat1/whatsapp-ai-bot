"""Knowledge-base documents and their embedded chunks (RAG storage)."""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# Dimensions of text-embedding-3-small. Changing the embedding model means
# changing this value AND running a migration that rebuilds the column.
EMBEDDING_DIMENSIONS = 1536


class Document(Base):
    """One source file from the knowledge folder."""

    __tablename__ = "documents"
    __table_args__ = (
        # Two tenants may both upload "pricing.pdf", and before 0016 the
        # second upload found the first tenant's row and overwrote it.
        UniqueConstraint("tenant_id", "source", name="uq_documents_tenant_source"),
        # The key chunks point at. See the note on User.
        UniqueConstraint("tenant_id", "id", name="uq_documents_tenant_scoped_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), index=True
    )
    # Path relative to the knowledge folder, e.g. "prices.pdf". Indexed for
    # lookup; uniqueness lives in the tenant-scoped constraint above.
    source: Mapped[str] = mapped_column(String(512), index=True)
    title: Mapped[str] = mapped_column(String(255))
    # sha256 of the file: lets ingestion skip unchanged documents.
    content_hash: Mapped[str] = mapped_column(String(64))
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class DocumentChunk(Base):
    """An embedded slice of a document, searchable by vector similarity."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        # CASCADE carried over from 0001 unchanged; passive_deletes above
        # depends on the database performing it.
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            name="fk_document_chunks_document",
            ondelete="CASCADE",
        ),
        UniqueConstraint("document_id", "chunk_index", name="uq_chunk_position"),
        # HNSW beats IVFFlat for small/medium corpora and needs no training
        # step, so the index works from the very first inserted row.
        Index(
            "ix_document_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Denormalised rather than reached through the document, because a vector
    # index cannot be filtered through a join. Tenant-filtered retrieval is
    # Phase 4 and needs the column to exist on this row to be possible at all;
    # the strategy for combining it with the HNSW index is a measurement, not
    # a decision to be made here.
    tenant_id: Mapped[int] = mapped_column(index=True)
    document_id: Mapped[int] = mapped_column(index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    # 1-based PDF page, NULL for plain-text sources.
    page: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="chunks")
