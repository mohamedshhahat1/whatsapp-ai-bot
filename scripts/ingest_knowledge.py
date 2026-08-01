#!/usr/bin/env python
"""Index the knowledge/ folder into the pgvector store.

Usage:
    python scripts/ingest_knowledge.py
    python scripts/ingest_knowledge.py --force
    python scripts/ingest_knowledge.py --path /data/knowledge --prune

Inside Docker:
    docker compose exec app python scripts/ingest_knowledge.py

Files that still contain the [[TODO]] marker are reported as skipped and are
not indexed. That is not an error: it means a knowledge_templates/ document
has not been filled in yet.
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Allow running the script directly from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.integrations.embeddings import get_embedding_client  # noqa: E402
from app.services.ingestion import KnowledgeIngestionService  # noqa: E402

STATUS_ICONS = {
    "indexed": "+",
    "unchanged": "=",
    "removed": "-",
    "skipped": ".",
    "failed": "!",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        default=None,
        help="Knowledge folder (defaults to the KNOWLEDGE_DIR setting)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-embed every document, even unchanged ones",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Remove indexed documents whose source file is gone",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    settings = get_settings()
    directory = Path(args.path or settings.knowledge_dir).resolve()

    print(f"Knowledge folder : {directory}")
    print(f"Embedding model  : {settings.embedding_model}")
    print(
        f"Chunk size       : {settings.chunk_max_tokens} tokens "
        f"(overlap {settings.chunk_overlap_tokens})"
    )
    print("-" * 62)

    async with SessionLocal() as session:
        service = KnowledgeIngestionService(session, get_embedding_client(), settings)
        try:
            results = await service.ingest_directory(
                directory, force=args.force, prune=args.prune
            )
        except FileNotFoundError as exc:
            print(f"error: {exc}")
            return 1

    if not results:
        print("No supported documents found (.pdf, .md, .txt).")
        return 0

    failures = 0
    for result in results:
        icon = STATUS_ICONS.get(result.status, "?")
        detail = f"{result.chunks} chunks" if result.chunks else ""
        if result.error:
            detail = result.error
            failures += 1
        elif result.note:
            detail = result.note
        print(f" {icon} {result.source:<40} {result.status:<10} {detail}")

    indexed = sum(1 for r in results if r.status == "indexed")
    chunks = sum(r.chunks for r in results if r.status == "indexed")
    skipped = sum(1 for r in results if r.status == "skipped")
    print("-" * 62)
    print(
        f"{indexed} document(s) indexed, {chunks} chunk(s) embedded, "
        f"{failures} failure(s)"
    )
    if skipped:
        print(
            f"{skipped} document(s) skipped: still contain [[TODO]] "
            f"placeholders and were not indexed."
        )

    await engine.dispose()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
