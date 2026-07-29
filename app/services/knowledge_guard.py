"""Stopping unfinished knowledge documents from reaching customers.

``knowledge_templates/`` ships the full document structure for the knowledge
base with every company fact left as a marker. That scaffolding is useful and
dangerous in equal measure, because a template is not inert:

1. A file is copied into ``knowledge/`` to be filled in.
2. Someone runs the ingestion script before finishing it.
3. The chunk containing the marker is embedded like any other text.
4. A customer asks about prices, retrieval returns that chunk, and the
   response rules see a matching company document -- so the model is now
   permitted to answer from it.

The result is a confident reply built around an empty slot, or worse, around
the example wording that happened to sit beside it. A comment in the file
saying "fill this in" prevents none of that: nothing reads comments.

So the marker is enforced mechanically instead. ``KnowledgeIngestionService``
asks this module before embedding anything, and a file that still contains the
marker is skipped with a reason rather than indexed.

Why a literal ASCII marker
--------------------------
``[[TODO]]`` is not a word, not Arabic, and cannot plausibly appear in a real
price list or contract, so a false positive is close to impossible. Detecting
"looks unfinished" heuristically -- short files, files full of dashes, files
without numbers -- would reject real documents, and rejecting a genuine price
list is how the bot ends up saying "I do not have that information" about the
one thing the company most wanted it to answer.
"""

PLACEHOLDER_MARKER = "[[TODO]]"


def count_placeholders(text: str) -> int:
    """How many unfilled slots remain in a document's text."""
    return text.count(PLACEHOLDER_MARKER)


def is_unfilled(text: str) -> bool:
    """True when a document still contains at least one unfilled slot."""
    return PLACEHOLDER_MARKER in text


def describe(text: str) -> str:
    """Reason string shown by the ingestion script for a skipped file."""
    remaining = count_placeholders(text)
    plural = "" if remaining == 1 else "s"
    return f"{remaining} unfilled {PLACEHOLDER_MARKER} placeholder{plural}"
