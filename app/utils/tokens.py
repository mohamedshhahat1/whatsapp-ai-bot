"""Token estimation and context-window trimming utilities."""


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 characters per token heuristic)."""
    return max(1, len(text) // 4)


def trim_history(
    history: list[dict], max_messages: int, max_tokens: int
) -> list[dict]:
    """Trim conversation history to fit message-count and token budgets.

    Keeps the most recent messages, always retaining at least one.
    """
    trimmed = history[-max_messages:]
    total = 0
    result: list[dict] = []
    for item in reversed(trimmed):
        cost = estimate_tokens(str(item.get("content", "")))
        if result and total + cost > max_tokens:
            break
        total += cost
        result.append(item)
    return list(reversed(result))
