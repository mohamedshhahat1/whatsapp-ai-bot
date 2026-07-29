"""C1 regression: configuration must stay synchronized.

The RAG feature shipped with consumers reading ``settings.rag_top_k`` and
friends while Settings had no such fields, so the first customer message after
deployment would have raised AttributeError. These tests fail if that drift
ever returns, in either direction: a field with no documented variable, or a
documented variable with no field.
"""

from pathlib import Path

from app.config import Settings

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPOSITORY_ROOT / ".env.example"

# Every field the RAG pipeline reads at runtime.
RAG_FIELDS = (
    "rag_enabled",
    "rag_top_k",
    "rag_min_score",
    "rag_max_context_chars",
    "knowledge_dir",
    "embedding_model",
    "embedding_dimensions",
    "embedding_batch_size",
    "chunk_max_tokens",
    "chunk_overlap_tokens",
)


def _documented_variables() -> set[str]:
    """Uncommented KEY=value names in .env.example."""
    names: set[str] = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        names.add(stripped.split("=", 1)[0].strip())
    return names


def test_settings_defines_every_rag_field() -> None:
    missing = [name for name in RAG_FIELDS if name not in Settings.model_fields]
    assert not missing, f"Settings is missing RAG fields: {missing}"


def test_rag_fields_are_documented_in_env_example() -> None:
    documented = _documented_variables()
    missing = [name for name in RAG_FIELDS if name.upper() not in documented]
    assert not missing, f".env.example does not document: {missing}"


def test_every_documented_variable_is_a_real_setting() -> None:
    """No variable may be documented that the application never reads."""
    unknown = sorted(
        name
        for name in _documented_variables()
        if name.lower() not in Settings.model_fields
    )
    assert not unknown, f".env.example documents unknown settings: {unknown}"


def test_rag_defaults_are_internally_consistent() -> None:
    settings = Settings()
    # Overlap must be smaller than the chunk, or chunking cannot advance.
    assert settings.chunk_overlap_tokens < settings.chunk_max_tokens
    assert settings.rag_top_k > 0
    assert 0.0 <= settings.rag_min_score <= 1.0
    assert settings.rag_max_context_chars > 0
    assert settings.embedding_dimensions == 1536
