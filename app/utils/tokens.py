"""Accurate token counting and history trimming using tiktoken.

Counting real tokens (instead of a character heuristic) lets the context
budget be used fully and safely: no premature trimming, no overshooting the
model's window.
"""

from functools import lru_cache

import tiktoken

from app.config import get_settings

# Per-message formatting overhead (role markers, separators) per OpenAI's
# token-counting guidance for chat-formatted input.
_MESSAGE_OVERHEAD_TOKENS = 4

# Encoding used by the gpt-4o / gpt-4.1 model families; fallback for model
# names tiktoken does not recognize yet.
_FALLBACK_ENCODING = "o200k_base"


@lru_cache
def _get_encoding(model: str) -> tiktoken.Encoding:
    """Resolve (and cache) the tokenizer for a model name."""
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return tiktoken.get_encoding(_FALLBACK_ENCODING)


def count_tokens(text: str, model: str | None = None) -> int:
    """Count the exact number of tokens in `text` for the configured model."""
    encoding = _get_encoding(model or get_settings().openai_model)
    return len(encoding.encode(text))


# Backwards-compatible alias (previously a rough character-based estimate).
estimate_tokens = count_tokens


def trim_history(
    history: list[dict[str, str]],
    max_messages: int,
    max_tokens: int,
) -> list[dict[str, str]]:
    """Return the most recent messages that fit within both budgets.

    Walks the history from newest to oldest, adding messages until either the
    message count or the token budget is exhausted. The newest message is
    always kept, even if it alone exceeds the token budget, so the model
    always sees the user's latest input.
    """
    trimmed: list[dict[str, str]] = []
    total_tokens = 0
    for message in reversed(history[-max_messages:]):
        cost = count_tokens(message.get("content", "")) + _MESSAGE_OVERHEAD_TOKENS
        if trimmed and total_tokens + cost > max_tokens:
            break
        total_tokens += cost
        trimmed.append(message)
    return list(reversed(trimmed))
