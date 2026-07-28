"""Token-aware text chunking for the knowledge base.

Embedding quality depends heavily on chunk boundaries. Splitting on a fixed
character count cuts sentences in half; splitting per page makes chunks too
large and dilutes the vector. This module splits on paragraph boundaries and
measures size in *tokens* (the unit the embedding model actually bills and
truncates on), packing paragraphs together until the budget is reached.

Consecutive chunks share an overlap so a fact that straddles a boundary is
still fully present in at least one chunk.
"""

import re
from dataclasses import dataclass
from functools import lru_cache

import tiktoken

# Encoding used by text-embedding-3-* and the GPT-4 family.
DEFAULT_ENCODING = "cl100k_base"

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_WHITESPACE = re.compile(r"[ \t]+")


@dataclass(frozen=True)
class TextChunk:
    """One embeddable unit of text."""

    text: str
    token_count: int


@lru_cache(maxsize=4)
def _encoding(name: str = DEFAULT_ENCODING) -> tiktoken.Encoding:
    """Cached tiktoken encoding (loading one is expensive)."""
    return tiktoken.get_encoding(name)


def count_tokens(text: str, encoding_name: str = DEFAULT_ENCODING) -> int:
    """Number of tokens the model will see for ``text``."""
    return len(_encoding(encoding_name).encode(text))


def normalize_text(text: str) -> str:
    """Tidy up PDF extraction artefacts before chunking."""
    # PDFs often emit hyphenated line breaks and ragged single newlines.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    text = _WHITESPACE.sub(" ", text)
    return text.strip()


def _windows(tokens: list[int], size: int, step: int) -> list[list[int]]:
    """Split an oversized paragraph into overlapping token windows."""
    windows: list[list[int]] = []
    start = 0
    while start < len(tokens):
        windows.append(tokens[start : start + size])
        start += step
    return windows


def chunk_text(
    text: str,
    *,
    max_tokens: int = 400,
    overlap_tokens: int = 60,
    encoding_name: str = DEFAULT_ENCODING,
) -> list[TextChunk]:
    """Split ``text`` into overlapping, paragraph-aligned chunks.

    Args:
        text: Raw text, typically one PDF page.
        max_tokens: Hard upper bound on tokens per chunk.
        overlap_tokens: Tokens repeated from the end of the previous chunk.
        encoding_name: tiktoken encoding to measure with.
    """
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    overlap = max(0, min(overlap_tokens, max_tokens - 1))

    cleaned = normalize_text(text)
    if not cleaned:
        return []

    encoding = _encoding(encoding_name)
    separator = encoding.encode("\n\n")

    # Paragraphs are the atomic units; oversized ones are pre-split.
    units: list[list[int]] = []
    for paragraph in _PARAGRAPH_SPLIT.split(cleaned):
        stripped = paragraph.strip()
        if not stripped:
            continue
        tokens = encoding.encode(stripped)
        if len(tokens) <= max_tokens:
            units.append(tokens)
        else:
            units.extend(_windows(tokens, max_tokens, max_tokens - overlap))

    chunks: list[list[int]] = []
    current: list[int] = []
    for unit in units:
        if current and len(current) + len(separator) + len(unit) > max_tokens:
            chunks.append(current)
            current = current[-overlap:] if overlap else []
        current = current + separator + unit if current else list(unit)
    if current:
        chunks.append(current)

    result: list[TextChunk] = []
    for tokens in chunks:
        decoded = encoding.decode(tokens).strip()
        if decoded:
            result.append(TextChunk(text=decoded, token_count=len(tokens)))
    return result
