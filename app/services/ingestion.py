"""Knowledge ingestion: PDF -> chunks -> embeddings -> vector store.

Run through ``scripts/ingest_knowledge.py``. Ingestion is idempotent: each
file's sha256 is stored, so re-running only re-embeds documents that actually
changed (embeddings cost money per token).

Documents that still contain the ``[[TODO]]`` marker are skipped rather than
indexed -- see ``app/services/knowledge_guard.py`` for why an unfinished
template is worse than a missing one.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.chunking import TextChunk, chunk_text
from app.core.logging import get_logger
from app.integrations.embeddings import EmbeddingClient
from app.repositories.document import ChunkInput, DocumentRepository
from app.services import knowledge_guard

logger = get_logger(__name__)

SUPPORTED_SUFFIXES = (".pdf", ".md", ".txt")


@dataclass
class IngestionResult:
    """Outcome for a single file."""

    source: str
    status: str  # indexed | unchanged | skipped | removed | failed
    chunks: int = 0
    error: str | None = None
    # Explanation for a deliberate skip. Kept separate from ``error`` so that
    # an unfinished template does not count as a pipeline failure.
    note: str | None = None


def file_hash(path: Path) -> str:
    """Streaming sha256 so large PDFs never land fully in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_pdf(path: Path) -> list[tuple[int | None, str]]:
    """Extract text per page so chunks keep a page citation."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages: list[tuple[int | None, str]] = []
    for number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append((number, text))
    return pages


def read_pages(path: Path) -> list[tuple[int | None, str]]:
    """Return ``(page_number, text)`` pairs for a supported file."""
    if path.suffix.lower() == ".pdf":
        return _read_pdf(path)
    text = path.read_text(encoding="utf-8", errors="ignore").strip()
    return [(None, text)] if text else []


def humanize(stem: str) -> str:
    """'price_list-2026' -> 'Price List 2026'."""
    return stem.replace("_", " ").replace("-", " ").strip().title() or stem


class KnowledgeIngestionService:
    """Builds and refreshes the vector index from the knowledge folder."""

    def __init__(
        self,
        session: AsyncSession,
        embeddings: EmbeddingClient,
        settings: Settings,
    ) -> None:
        self._session = session
        self._embeddings = embeddings
        self._settings = settings
        self._documents = DocumentRepository(session)

    async def ingest_file(
        self, path: Path, root: Path | None = None, force: bool = False
    ) -> IngestionResult:
        """Index one file. Never raises: failures are reported per document."""
        source = str(path.relative_to(root)) if root else path.name
        try:
            content_hash = file_hash(path)
            existing = await self._documents.get_by_source(source)
            if existing and existing.content_hash == content_hash and not force:
                return IngestionResult(source, "unchanged", existing.chunk_count)

            pages = read_pages(path)
            if not pages:
                return IngestionResult(
                    source,
                    "failed",
                    error="no extractable text (scanned PDF? needs OCR)",
                )

            # An unfinished document must never become a retrievable chunk.
            # If a filled version was indexed earlier, drop it: leaving the old
            # text in the store would keep answering customers from a document
            # somebody has since taken back into editing.
            full_text = "\n".join(text for _, text in pages)
            if knowledge_guard.is_unfilled(full_text):
                note = knowledge_guard.describe(full_text)
                if existing:
                    await self._documents.delete_by_source(source)
                    await self._session.commit()
                    note = f"{note}; removed from the index"
                logger.warning(
                    "document_skipped_unfilled", source=source, detail=note
                )
                return IngestionResult(source, "skipped", note=note)

            pieces: list[tuple[int | None, TextChunk]] = []
            for page_number, text in pages:
                for chunk in chunk_text(
                    text,
                    max_tokens=self._settings.chunk_max_tokens,
                    overlap_tokens=self._settings.chunk_overlap_tokens,
                ):
                    pieces.append((page_number, chunk))

            if not pieces:
                return IngestionResult(source, "failed", error="produced no chunks")

            vectors = await self._embeddings.embed_texts(
                [chunk.text for _, chunk in pieces]
            )
            payload = [
                ChunkInput(
                    chunk_index=index,
                    content=chunk.text,
                    token_count=chunk.token_count,
                    embedding=vector,
                    page=page_number,
                )
                for index, ((page_number, chunk), vector) in enumerate(
                    zip(pieces, vectors, strict=True)
                )
            ]

            document = await self._documents.upsert(
                source=source, title=humanize(path.stem), content_hash=content_hash
            )
            await self._documents.replace_chunks(document, payload)
            await self._session.commit()

            logger.info("document_indexed", source=source, chunks=len(payload))
            return IngestionResult(source, "indexed", len(payload))
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            await self._session.rollback()
            logger.error("document_ingestion_failed", source=source, error=str(exc))
            return IngestionResult(source, "failed", error=str(exc))

    async def ingest_directory(
        self, directory: Path, force: bool = False, prune: bool = False
    ) -> list[IngestionResult]:
        """Index every supported file under ``directory``.

        Args:
            force: Re-embed even when the file hash is unchanged.
            prune: Delete indexed documents whose file no longer exists.
        """
        if not directory.is_dir():
            raise FileNotFoundError(f"Knowledge folder not found: {directory}")

        files = sorted(
            path
            for path in directory.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
        )

        results = [
            await self.ingest_file(path, root=directory, force=force) for path in files
        ]

        if prune:
            present = {str(path.relative_to(directory)) for path in files}
            for document in await self._documents.list_documents():
                if document.source not in present:
                    await self._documents.delete_by_source(document.source)
                    results.append(IngestionResult(document.source, "removed"))
            await self._session.commit()

        return results
