# Conversation session lifecycle

A **session** is one complete visit: welcome, questions, goodbye. A customer
who writes in March and again in July should be greeted twice and thanked
twice, and the July conversation should not carry March's history into the
model's context window.

Every duration and every behaviour below is configuration. There are no
timeouts in the code: see [Configuration](#configuration).

## The state model

A session **is** a `conversations` row. There is no separate session table and
no separate state column.

The four states in the spec are **derived**, by
`Conversation.session_state(idle_after, now=None)`:

| State | Condition |
| --- | --- |
| `CLOSED` | `status = 'closed'` |
| `ACTIVE_HUMAN` | open and `mode = 'human'` |
| `WAITING_IDLE` | open, `mode = 'bot'`, and `now - last_activity_at >= idle_after` |
| `ACTIVE_BOT` | open and recently active |

Precedence runs top to bottom.

The reason for deriving rather than storing: a stored state would have to be
written by something, and the only thing that could write `WAITING_IDLE` is the
sweeper — which runs once a minute. Between ticks the column would be a
confident lie. Deriving it means the answer is correct the microsecond you ask,
and there is no third source of truth to drift out of step with `status` and
`mode`.

Note that a quiet **human** conversation reports `ACTIVE_HUMAN`, not
`WAITING_IDLE`. `WAITING_IDLE` means "due to be closed", and the sweeper never
closes a conversation an operator is holding. A state that contradicted the
behaviour would be worse than no state at all.

### Columns

Added by migration `0007_conversation_session_lifecycle`:

- `last_activity_at` — NOT NULL. The idle timer.
- `welcome_sent_at` — the welcome flag. NULL means not yet greeted.
- `closing_sent_at` — the closing flag. NULL means not yet thanked.
- `closed_at` — when the session ended.

Plus a partial index for the sweep:

```sql
CREATE INDEX ix_conversations_idle_sweep ON conversations (last_activity_at)
  WHERE status = 'active' AND closing_sent_at IS NULL;
```

Partial because that predicate is the sweeper's entire query and it excludes
almost every row in the table. The index stays small however large the history
grows, and rows leave it permanently once closed.

## Coming back

There are two ways a customer returns after a goodbye, and which one they get
depends only on how long they took.

### Straight back: the reopen window

A goodbye followed thirty seconds later by "sorry, one more thing" is one
conversation, not two. Within `CONVERSATION_REOPEN_WINDOW_MINUTES` of
`closed_at`, `ConversationRepository.reopen_recent` **revives the same row**:
status back to active, `closed_at` and `closing_sent_at` cleared, timer reset.

Reviving rather than copying is the point. `welcome_sent_at` survives, so
`should_welcome` already returns `False` and nothing has to remember to
suppress a second greeting; the history survives too, so the model still knows
what "it" refers to. Clearing `closing_sent_at` re-arms the goodbye — without
that the resumed session could never be closed again, because a claimed
session is permanently ineligible.

Set the window to `0` to switch this off entirely and make every closed
session final.

### Later: a genuinely new session

Migration `0003` already carried a partial unique index,
`uq_active_conversation_per_user`, over `status = 'active'`. One open
conversation per customer, enforced by Postgres.

So closing a session **frees the slot**, and a message arriving past the
reopen window reaches the existing `get_or_create_active` and mints a fresh
row: new id, new history, `welcome_sent_at` NULL, `closing_sent_at` NULL,
timer at now. Every clause of the "new session" requirement — cleared closed
state, reset timers, reset closing flag, welcome again, message processed
normally — falls out of one index that already existed. Nothing special-cases
it, which is why nothing can forget to.

`NEW_SESSION_AFTER_HOURS` is the outer bound on the above: the reopen window
is clamped to it, so a window longer than the bound cannot let someone
returning next week land in last week's thread. The two are reconciled in
`Settings.new_session_after` rather than at the call site, so they cannot
disagree depending on which check runs first.

This is **unrelated** to Meta's 24-hour customer service window
(`CUSTOMER_SERVICE_WINDOW` in `app/services/reply_service.py`), which is a
platform rule rather than a preference and is deliberately not configurable.
They share a number today by coincidence.

## The idle timer

`last_activity_at` is bumped by `SessionService.touch()` on:

- every inbound customer message (text, media, unsupported)
- every AI reply, at reservation and again on confirmed send
- every manual operator reply (`ReplyService.send_manual_reply`)
- every mode switch between bot and human (`ConversationRepository.set_mode`)

Bumping on AI activity as well as customer activity is deliberate. If only
inbound messages counted, a reply that took six minutes to generate would be
overtaken by its own goodbye.

The outbound bumps pass `outgoing=True`, which is what
`RESET_IDLE_TIMER_ON_OUTGOING_MESSAGE` switches off. Turning it off makes the
timer measure silence from the customer alone and restores exactly the race
described above; it exists to make the choice visible, not because there is a
good reason to take it.

## Closing

Celery beat emits `conversations.close_idle_sessions` once a minute onto the
`webhooks` queue. `SWEEP_INTERVAL_SECONDS` is the *resolution*, not the
timeout: with a five-minute timeout the goodbye lands between 5:00 and 6:00
after the last activity. Each tick carries `expires` of one interval, so a
broker outage cannot queue a backlog of redundant sweeps that all fire at once
on recovery.

The sweeper claims work with a conditional update:

```sql
UPDATE conversations SET closing_sent_at = now(), status = 'closed', ...
 WHERE id IN (...) AND closing_sent_at IS NULL
 RETURNING id
```

and **commits the claim before sending anything** — the same shape as
`reserve_reply`. That ordering is what makes the guarantee hold:

- **Multiple workers / duplicate ticks.** Two sweepers racing the same row:
  one `UPDATE` matches, the other sees `closing_sent_at IS NOT NULL` and
  returns nothing. Postgres arbitrates; only one id is ever returned.
- **Worker restart mid-send.** The claim is already committed, so the retry
  finds nothing to claim. A crash at exactly the wrong moment costs one
  missing goodbye, never a duplicate one.
- **Server restart.** State is in Postgres, not in a timer object. Nothing is
  lost on restart and nothing needs rescheduling.

A failed WhatsApp send is logged (`closing_send_failed`) and never retried.
This is the deliberate trade: **a missing goodbye is a non-event; a duplicate
goodbye is the bot looking broken.** Retrying safely would need the claim to be
releasable, which reopens the duplicate window.

This is also why `PREVENT_DUPLICATE_CLOSING` is not read by
`_should_send_closing`. The guarantee it names is structural rather than
conditional: a second goodbye cannot be reached to be suppressed, because the
claim never hands the same session out twice. The flag documents the promise;
the claim keeps it.

### Two guards

- **`mode = 'bot'` only.** The sweeper never closes a conversation an operator
  holds. The handoff scenario still works: handing back to the AI resets the
  timer, and the conversation becomes eligible from that point.
- **The 24-hour Meta service window.** Sessions quiet longer than that are
  closed *silently* — the send would be rejected anyway. This also neutralises
  the first-deploy hazard: without it, the first sweep after release would
  greet every dormant conversation in the table with a goodbye. The migration
  backfills `last_activity_at` from `updated_at`/`created_at` rather than
  `now()` precisely so this guard can see how old they really are.

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `ENABLE_CONVERSATION_SESSION` | `true` | Master switch. Off, conversations behave as they did before this feature: one endless thread per customer. |
| `CONVERSATION_IDLE_TIMEOUT_MINUTES` | `5` | Floored at 1 minute. |
| `CONVERSATION_CLOSE_AFTER_IDLE` | `true` | Off, the timer still runs and `WAITING_IDLE` is still reported, but nothing is closed. |
| `ENABLE_CONVERSATION_CLOSING_MESSAGE` | `true` | Off still closes sessions, silently. |
| `CONVERSATION_REOPEN_WINDOW_MINUTES` | `30` | `0` disables reopen; every closed session is final. |
| `NEW_SESSION_AFTER_HOURS` | `24` | Outer bound; clamps the reopen window. |
| `ENABLE_WELCOME_ON_NEW_SESSION` | `true` | Off, sessions begin with an answer and no greeting. |
| `ENABLE_REPEAT_WELCOME_AFTER_NEW_SESSION` | `true` | Off greets each customer once, ever. |
| `PREVENT_DUPLICATE_WELCOME` | `true` | Should stay on. |
| `PREVENT_DUPLICATE_CLOSING` | `true` | Should stay on; see above for why it is structural. |
| `RESET_IDLE_TIMER_ON_OUTGOING_MESSAGE` | `true` | Off, the timer measures customer silence only. |
| `CONVERSATION_CLOSING_MESSAGE` | *(empty)* | Empty uses `persona.CLOSING`. |

The closing copy defaults to the Arabic in `app/services/persona.py`, matching
`WELCOME` and `NOT_UNDERSTOOD`. It lives in code for the same reason they do:
it is multi-line text a customer reads, it belongs in review, and a `.env`
value cannot hold a newline without escaping games. Set the variable to
override it — for a different business, or to switch the closing language.

## Operational requirement

**The `beat` container must run.** Without it, sessions are opened, greeted,
tracked and reset correctly — and nothing ever closes them. No closing message
is ever sent and every conversation stays open forever.

That failure is silent: the app is healthy, the worker is healthy, only the
absence of goodbyes reveals it. Beat answers no inspect ping, so monitor it
from the other end — alert on the `session_sweep_completed` log line going
quiet, or on active conversations older than the idle timeout.

Run **exactly one replica**. A second scheduler doubles the tick rate; the
conditional claim keeps that *correct*, so the cost is wasted queries rather
than duplicate messages — but there is no reason to pay it.
