"""C1 regression: configuration must stay synchronized.

The RAG feature shipped with consumers reading ``settings.rag_top_k`` and
friends while Settings had no such fields, so the first customer message after
deployment would have raised AttributeError. These tests fail if that drift
ever returns, in either direction: a field with no documented variable, or a
documented variable with no field.

Configuration is not one class. ``app/config.py`` can only be rewritten whole
by the tooling that edits it, and one such rewrite silently dropped a hundred
lines of comments, so push notifications, inbound freshness, audit retention
and the channel switches each got their own ``BaseSettings`` module instead.
They all read the environment identically, so "is this a real setting?" has to
be asked of all of them. Asked of Settings alone it reports every PUSH_*,
FCM_*, INBOUND_*, AUDIT_* and channel variable as unknown -- which is not
drift, it is the question being put to the wrong object.

Both directions, and why the second one matters more
----------------------------------------------------
``test_every_documented_variable_is_a_real_setting`` catches the variable that
outlived its field: an operator sets it, nothing reads it, and the setting
appears to do nothing. That is annoying.

``test_every_setting_is_documented`` catches the opposite, which is worse and
more common: a field added in code that nobody can discover without reading
the source. It is exactly what happened when the channel layer landed --
``ChannelSettings`` arrived with sixteen fields and ``.env.example`` mentioned
none of them, so there was no way to learn that ENABLE_MESSENGER existed. As
more channels are added this is the direction that will drift first, because
adding a field is a code change and documenting it is not.

When the second test fails, the fix is almost always to add the variable to
``.env.example`` with a comment saying what it does. Exempting it instead is
for the rare field where documenting it would be actively wrong; see
``UNDOCUMENTED_BY_DESIGN``.
"""

from pathlib import Path

from pydantic_settings import BaseSettings

from app.channels.config import ChannelSettings
from app.config import Settings
from app.core.inbound_config import InboundSettings
from app.core.push_config import PushSettings
from app.core.retention_config import RetentionSettings

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPOSITORY_ROOT / ".env.example"

# Every class that reads configuration from the environment. A new one belongs
# here on the day it is added: until it does, every variable it introduces
# reads as documented-but-unused and this file goes red for the wrong reason.
SETTINGS_CLASSES: tuple[type[BaseSettings], ...] = (
    Settings,
    ChannelSettings,
    InboundSettings,
    PushSettings,
    RetentionSettings,
)

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

# Settings that deliberately have no uncommented .env.example line, mapped to
# the reason. A field belongs here only when documenting it would be actively
# wrong -- never merely because nobody has written the entry yet. Anything
# added here should read as an argument, because that is what the next person
# will weigh it as.
UNDOCUMENTED_BY_DESIGN: dict[str, str] = {
    "system_prompt": (
        "Present in .env.example as a commented-out line on purpose. "
        "Uncommented, SYSTEM_PROMPT= would REPLACE the reviewed Arabic "
        "persona in app/services/persona.py with an empty string on every "
        "fresh checkout -- the bot would lose its identity by default "
        "rather than by choice. See docs/PERSONA.md."
    ),
}


def _documented_variables() -> set[str]:
    """Uncommented KEY=value names in .env.example."""
    names: set[str] = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        names.add(stripped.split("=", 1)[0].strip())
    return names


def _configured_fields() -> set[str]:
    """Field names across every settings class, lowercase as declared."""
    return {name for cls in SETTINGS_CLASSES for name in cls.model_fields}


def test_settings_defines_every_rag_field() -> None:
    missing = [name for name in RAG_FIELDS if name not in Settings.model_fields]
    assert not missing, f"Settings is missing RAG fields: {missing}"


def test_rag_fields_are_documented_in_env_example() -> None:
    documented = _documented_variables()
    missing = [name for name in RAG_FIELDS if name.upper() not in documented]
    assert not missing, f".env.example does not document: {missing}"


def test_every_documented_variable_is_a_real_setting() -> None:
    """No variable may be documented that the application never reads."""
    known = _configured_fields()
    unknown = sorted(
        name for name in _documented_variables() if name.lower() not in known
    )
    assert not unknown, f".env.example documents unknown settings: {unknown}"


def test_every_setting_is_documented() -> None:
    """No setting may exist that .env.example never mentions.

    The complement of the test above, and the one that would have caught the
    channel switches shipping undocumented. To fix a failure, add the variable
    to .env.example -- or, if documenting it would be wrong, record it in
    UNDOCUMENTED_BY_DESIGN with the reason.
    """
    documented = _documented_variables()
    checked = _configured_fields() - set(UNDOCUMENTED_BY_DESIGN)
    missing = sorted(name for name in checked if name.upper() not in documented)
    assert not missing, f".env.example does not document settings: {missing}"


def test_documentation_exemptions_are_still_needed() -> None:
    """An exemption that has stopped being true is worse than none at all.

    Both halves are rot: the first names a setting that no longer exists, the
    second silently excuses a setting from a check it would now pass.
    """
    known = _configured_fields()
    documented = _documented_variables()
    exempt = set(UNDOCUMENTED_BY_DESIGN)

    stale = sorted(name for name in exempt if name not in known)
    assert not stale, f"Exemptions for settings that no longer exist: {stale}"

    redundant = sorted(name for name in exempt if name.upper() in documented)
    assert not redundant, f"Exemptions no longer needed: {redundant}"


def test_rag_defaults_are_internally_consistent() -> None:
    settings = Settings()
    # Overlap must be smaller than the chunk, or chunking cannot advance.
    assert settings.chunk_overlap_tokens < settings.chunk_max_tokens
    assert settings.rag_top_k > 0
    assert 0.0 <= settings.rag_min_score <= 1.0
    assert settings.rag_max_context_chars > 0
    assert settings.embedding_dimensions == 1536
