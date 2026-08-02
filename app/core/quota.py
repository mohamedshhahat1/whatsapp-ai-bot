"""Per-customer quotas, abuse detection and the OpenAI spend circuit breaker.

Why this exists alongside ``app/core/ratelimit.py``:

slowapi limits the HTTP endpoint by client IP. That is the correct thing for
the admin API, where each operator really is a different address. It does
almost nothing for the webhook, because every delivery arrives from Meta's
infrastructure -- one bucket shared by every customer in the business. One
person holding down send consumes the allowance for everyone, and the abuser
is indistinguishable from the abused.

The identity that matters here is the WhatsApp number, and it is only known
after the payload has been parsed, which is inside the worker rather than at
the edge. So these checks live on the processing path, in front of every paid
call.

Three independent protections:

1. **Rate**   -- sliding minute/hour/day windows per wa_id.
2. **Abuse**  -- flood bursts and repeated identical text, which buy a
                 temporary block rather than a per-message refusal.
3. **Spend**  -- daily USD and token ceilings across all customers, which
                 switch the model off globally when breached.

Everything fails OPEN. If Redis is down, every check allows the message and
logs the failure. A protective measure that silently stops answering paying
customers when a cache is unavailable is worse than the abuse it prevents.
"""

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.core.logging import get_logger
from app.core.metrics import (
    AI_DISABLED,
    CUSTOMER_ABUSE_BLOCKS_TOTAL,
    CUSTOMER_RATE_LIMITED_TOTAL,
    DAILY_SPEND_USD,
    SPEND_GUARD_TRIPS_TOTAL,
)

logger = get_logger(__name__)

# Key namespace. Everything is prefixed so a shared Redis stays legible and
# `redis-cli --scan --pattern 'quota:*'` shows the whole subsystem.
_MESSAGES = "quota:msgs:"  # sorted set, one member per message, 24h of history
_DUPLICATE = "quota:dup:"  # sorted set per (customer, message body), sliding window
_BLOCKED = "quota:blocked:"  # presence = temporarily blocked
_SPEND = "quota:spend:"  # daily USD, float
_TOKENS = "quota:tokens:"  # daily tokens, int
_ALERTED = "quota:alerted:"  # de-duplicates alerts within a day
_KILL_SWITCH = "quota:ai:disabled"  # set by an operator, cleared by an operator

# Reasons, used for metrics labels, logs and choosing the customer-facing copy.
RATE_LIMITED = "rate_limited"
FLOODING = "flooding"
SPAMMING = "spamming"
BLOCKED = "blocked"
SPEND_EXCEEDED = "spend_exceeded"
TOKENS_EXCEEDED = "tokens_exceeded"
AI_DISABLED_MANUALLY = "ai_disabled"


@dataclass(frozen=True)
class QuotaDecision:
    """The outcome of the checks for one inbound message.

    ``notify`` distinguishes "tell the customer something" from "drop this
    silently". A customer who has genuinely tripped a limit gets one
    explanation; the next forty messages of the same flood get nothing, or the
    reply itself becomes the flood.
    """

    allowed: bool
    reason: str | None = None
    notify: bool = False
    retry_after_seconds: int | None = None

    @property
    def blocked_for_cost(self) -> bool:
        return self.reason in (SPEND_EXCEEDED, TOKENS_EXCEEDED, AI_DISABLED_MANUALLY)


ALLOWED = QuotaDecision(allowed=True)


# ---------------------------------------------------------------------------
# Customer-facing copy.
#
# Arabic, matching the persona. These are sent WITHOUT a model call -- the
# whole point is that the customer has hit a limit, so spending a completion
# to phrase the refusal would defeat it.
# ---------------------------------------------------------------------------

RATE_LIMIT_MESSAGE = (
    "\u0648\u0635\u0644\u062a\u0646\u0627 \u0631\u0633\u0627\u0626\u0644\u0643 \u0648\u0634\u0643\u0631\u064b\u0627 \u0644\u062a\u0648\u0627\u0635\u0644\u0643 \u0645\u0639\u0646\u0627. \u062f\u0639\u0646\u0627 \u0646\u0623\u062e\u0630 \u062f\u0642\u062a\u0642\u0629 "
    "\u0644\u0644\u0631\u062f \u0639\u0644\u064a\u0643 \u0628\u0634\u0643\u0644 \u0645\u0646\u0627\u0633\u0628 \u0648\u0643\u0627\u0645\u0644. \u0645\u0646 \u0641\u0636\u0644\u0643 \u0623\u0631\u0633\u0644 \u0633\u0624\u0627\u0644\u0643 "
    "\u0641\u062a \u0631\u0633\u0627\u0644\u0629 \u0648\u0627\u062d\u062f\u0629 \u0648\u0633\u0646\u0648\u0627\u0641\u064a\u0643 \u062d\u0642\u0647 \u0641\u0648\u0631\u064b\u0627."
)

ABUSE_MESSAGE = (
    "\u0644\u0627\u062d\u0638\u0646\u0627 \u0639\u062f\u062f\u064b\u0627 \u0643\u0628\u062a\u0631\u064b\u0627 \u0645\u0646 \u0627\u0644\u0631\u0633\u0627\u0626\u0644 \u0627\u0644\u0645\u062a\u0643\u0631\u0631\u0629. "
    "\u0633\u0646\u062a\u0648\u0642\u0641 \u0645\u0624\u0642\u062a\u064b\u0627 \u0639\u0646 \u0627\u0644\u0631\u062f \u0627\u0644\u0622\u0644\u064a \u0644\u0641\u062a\u0631\u0629 \u0642\u0635\u062a\u0631\u0629. "
    "\u0625\u0630\u0627 \u0643\u0646\u062a \u062a\u062d\u062a\u0627\u062c \u0645\u0633\u0627\u0639\u062f\u0629 \u0639\u0627\u062c\u0644\u0629 \u0641\u0627\u0643\u062a\u0628 \u0643\u0644\u0645\u0629 \u0645