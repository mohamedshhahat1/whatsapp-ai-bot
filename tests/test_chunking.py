"""Chunker unit tests (no database, no network)."""

from app.core.chunking import chunk_text, count_tokens, normalize_text


def test_short_text_becomes_one_chunk():
    chunks = chunk_text("We install ceramic tiles and gypsum ceilings.")
    assert len(chunks) == 1
    assert "ceramic tiles" in chunks[0].text
    assert chunks[0].token_count > 0


def test_empty_text_produces_no_chunks():
    assert chunk_text("   \n\n  ") == []


def test_chunks_respect_the_token_budget():
    paragraphs = [f"Paragraph number {i} about finishing work." for i in range(200)]
    chunks = chunk_text("\n\n".join(paragraphs), max_tokens=100, overlap_tokens=20)

    assert len(chunks) > 1
    assert all(chunk.token_count <= 100 for chunk in chunks)


def test_oversized_paragraph_is_split():
    single = " ".join("word" for _ in range(1000))
    chunks = chunk_text(single, max_tokens=50, overlap_tokens=10)

    assert len(chunks) > 1
    assert all(chunk.token_count <= 50 for chunk in chunks)


def test_consecutive_chunks_overlap():
    paragraphs = [f"Sentence {i} describing painting services." for i in range(60)]
    chunks = chunk_text("\n\n".join(paragraphs), max_tokens=80, overlap_tokens=30)

    assert len(chunks) >= 2
    tail_words = set(chunks[0].text.split()[-8:])
    head_words = set(chunks[1].text.split()[:20])
    assert tail_words & head_words


def test_normalize_repairs_pdf_artifacts():
    raw = "reno-\nvation works\nfor villas\n\nSecond block"
    cleaned = normalize_text(raw)

    assert "renovation works" in cleaned
    assert "\n\n" in cleaned


def test_count_tokens_is_positive():
    assert count_tokens("kitchen renovation quote") > 0
