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

The states are **derived**, by the free function
`derive_session_state(status, mode, last_activity_at, closing_sent_at,
idle_after, now=None)` in `app/models/conversation.py`.
`Conversation.session_state(idle_after, now=None)` is a thin method over it.

| State | Condition |
| --- | --- |
| `CLOSED` | `status = 'closed'` |
| `CLOSING` | open, but `closing_sent_at` is set — claimed by the sweeper |
| `ACTIVE_HUMAN` | open and `mode = 'human'` |
| `WAITING_IDLE` | open, `mode = 'bot'`, and `now - last_activity_at >= idle_after` |
| `ACTIVE_BOT` | open and recently active |

Precedence runs top to bottom.

It is a free function, not only a method, because the API schema derives the
same state during serialisation without an ORM instance to hand. One function
is what stops the backend, the dashboard and the Flutter app from each growing
a slightly different version of this table.

The reason for deriving rather than storing: a stored state would have to be
written by something, and the only thing that could write `WAITING_IDLE` is the
sweeper — which runs once a minute. Between ticks the column would be a
confident lie. Deriving it means the answer is correct the microsecond you ask,
and there is no third source of truth to drift out of step with `status` and
`mode`.

`CLOSING` covers the short gap between the sweeper claiming a session and the
goodbye being delivered. It is brief but real, and an operator looking at the
row during it should see that it is on its way out rather than a state
implying they can still step in.

Note that a quiet **human** conversation reports `ACTIVE_HUMAN`, not
`WAITING_IDLE`. `WAITING_IDLE` means "due to be closed", and the sweeper never
closes a conversation an operator is holding. A state that contradicted the
behaviour would be worse than no state at all.

### Why there is no `REOPENED` state

Reviving a session clears `closed_at` and `closing_sent_at` and sets `status`
back to active — which leaves the row **identical in every column** to one that
never closed. There is nothing left to derive `REOPENED` from.

Reporting it would require storing a `reopened_at` column purely to colour a
badge, and that column would then need its own rules about when it is cleared
(does a session reopened in March still read as reopened in July?). The
reopen *event* carries the transition to anyone who is watching in real time,
which is where that information is actually useful.

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

### An operator acting on a closed session

There is a third path, and it is not driven by the customer at all.

An operator who replies to, takes over, or resumes the AI on a session the
sweeper has already closed used to write into a dead row. The message went
out, but the customer's answer either revived a *different* session or created
one — so the operator's question and the reply to it ended up in two separate
conversations, and neither read as a coherent exchange.

`revive_for_operator()` in `app/services/reply_service.py` now runs first on
all four of those actions. It calls `ConversationRepository.reopen()`, which
shares the same `_REVIVE` payload as `reopen_recent` — one dict, so the two
paths cannot drift into meaning different things.

**No time window is applied here, on purpose.** The reopen window exists to
guess whether a returning *customer* is continuing their visit or starting a
new one. An operator who has deliberately opened a specific conversation and
typed into it has stated their intent, and second-guessing it after an
arbitrary number of minutes would just resurrect the orphaned-reply bug for
older sessions.

Reviving can still fail, in exactly one way: the customer has since started
another session, and the partial unique index permits only one active
conversation per customer. That is **not** swallowed. The action is refused
with `409 conversation_superseded`, and both clients handle it — the dashboard
and the Flutter app each disable Reply and Take Over and explain why, rather
than leaving the operator typing into something the customer is no longer
reading.

Note what this means for the UI: controls are disabled on a **superseded**
conversation, not merely a closed one. Disabling on closed would block the
normal path, since closed sessions reopen on demand.

### A delivery that arrives late

Meta retries a webhook it could not deliver, and keeps retrying for hours.
That is the behaviour you want — it is what stops a deploy or a brief outage
from losing customer messages — but it interacts badly with everything above,
because a redelivery is indistinguishable from a message sent a second ago
unless something looks at the timestamp.

This is not hypothetical. A customer said goodbye, received the closing
message, and around forty minutes later — having sent nothing at all — received
a welcome and the interactive menu. The message that caused it was real, and
had been sent *before* the goodbye; it had simply never been processed, because
the webhook endpoint was returning 500 at the time. When the retry finally
succeeded it arrived past the reopen window, so it did not revive the session:
it minted a new one, with `welcome_sent_at` NULL, and that new session was
greeted exactly as designed.

Every individual step was correct, including the greeting. The defect was that
a message's **age was never an input** to any of those decisions.

So `app/services/webhook_processor.py` now measures how old an inbound message
is before dispatching it. Past `INBOUND_MAX_AGE_MINUTES`,
`record_without_answering()` in `app/services/stale_inbound.py` runs instead of
the normal handler: it claims the message through the same `claim_inbound`
path, commits it, publishes `conversation.activity`, logs
`stale_inbound_not_answered` with the state it found — and sends nothing.

Recording rather than discarding matters. The customer did write to you, an
operator should see it, and it should sit in the transcript in the right place.
What is suppressed is only the *reply*, because answering a forty-minute-old
message is worse than not answering it: the customer has moved on, and the
answer arrives with nothing to attach itself to.

**`INBOUND_MAX_AGE_MINUTES` must stay below
`CONVERSATION_REOPEN_WINDOW_MINUTES`.** Between the two lies a band of time in
which a delivery is fresh enough to answer but too old to revive the session it
belongs to — which reproduces the original bug exactly. The defaults leave
twenty minutes of headroom (10 against 30), and
`test_default_max_age_stays_below_the_reopen_window` in
`tests/test_inbound_freshness.py` fails if a later change closes that gap, so
this constraint is enforced rather than merely written down here.

The gate **fails open**. A message whose `timestamp` is missing or unparseable
is answered normally and logs `inbound_timestamp_unparseable`. A malformed
field or a future payload version should cost a log line, not a silent refusal
to answer a customer who is sitting there waiting.

Only `messages` are gated. Delivery and read receipts in the same payload are
processed whatever their age, since they send nothing and only move a row's
status forward — a late receipt is still true.

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

## Realtime events

Lifecycle transitions are published to the `dashboard:events` Redis channel by
`app/core/events.py` and relayed to every connected client by `/ws/events`.
The relay forwards whatever is on the channel, so adding an event type needs no
WebSocket change.

| Event | When |
| --- | --- |
| `conversation.activity` | a message arrived or was sent |
| `conversation.handoff` | ownership changed — takeover, resume, assignment |
| `conversation.closed` | a session ended, **for every close** |
| `conversation.reopened` | a closed session was revived (`reason`: `customer` or `operator`) |

`conversation.closed` is published for every close, not only the ones that
send a goodbye. That distinction is the entire reason it exists. The sweeper
closes silently whenever the closing message is disabled, the copy is empty, or
the session has fallen outside Meta's service window — and in those cases no
event fired at all. The conversation list **stops polling while the event
stream is connected**, so the stale row never self-corrected on a *healthy*
system; only a manual refresh fixed it.

Events are thin by design: they say *that* a conversation changed, never what
was said. The lifecycle events additionally carry the row's own status columns
so a client can repaint a badge without a round trip — those are facts about
the row, not about the person. No phone number, name or message body is ever
written to the bus.

## The operator list

`ConversationRepository.list()` orders by:

1. **Unclaimed sales leads first** — tagged, in human mode, nobody assigned,
   **and still active**.
2. **Everything else by `updated_at`, newest first.**

The `status = 'active'` condition in the first rule was added with this work
and is load-bearing. Before sessions closed themselves, a lead stayed active
until an operator claimed it. Now it closes after five idle minutes — and
without that condition it would stay pinned to the top of every operator's
screen permanently, which is exactly how people learn to ignore the top of a
list.

Recency deliberately dominates within the second group rather than status.
Pushing every closed session below every active one sounds tidier and is worse
in practice: an operator scanning the list wants what happened recently, and a
conversation that ended four minutes ago matters more to them than one sitting
open and silent since yesterday. Operators who want only live work filter for
it — `GET /admin/conversations?status=active`.

Sorting lives in the repository rather than in each client so that the web UI,
the mobile app and any direct API consumer agree, and so pagination stays
correct: sorting a page after fetching it only orders the fifty rows that
happened to land on it.

## The API

Every conversation endpoint returns the full lifecycle picture:
`status`, `last_activity_at`, `welcome_sent_at`, `closing_sent_at`,
`closed_at`, `created_at`, `updated_at`, plus three computed fields —
`session_state`, `idle_timeout_minutes` and `close_after_idle`. The last two
travel with the payload so a client can render "closes in about four minutes"
without being separately configured with the server's timeout.

- `GET /admin/conversations?offset&limit&status` — `status` accepts `active`
  or `closed`.
- `GET /admin/conversations/{id}`
- `GET /admin/conversations/{id}/history?limit` — the customer's **other**
  sessions, newest first, with a total count.

The history endpoint is an operator convenience and nothing more. Sessions stay
separate rows, because they were separate visits and merging them would
misrepresent what happened. It is explicitly **not** used to build model
context: the AI still sees only the current session, since silently widening
what it remembers would change its answers in ways nobody asked for.

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `ENABLE_CONVERSATION_SESSION` | `true` | Master switch. Off, conversations behave as they did before this feature: one endless thread per customer. |
| `CONVERSATION_IDLE_TIMEOUT_MINUTES` | `5` | Floored at 1 minute. |
| `CONVERSATION_CLOSE_AFTER_IDLE` | `true` | Off, the timer still runs and `WAITING_IDLE` is still reported, but nothing is closed. |
| `ENABLE_CONVERSATION_CLOSING_MESSAGE` | `true` | Off still closes sessions, silently. |
| `CONVERSATION_REOPEN_WINDOW_MINUTES` | `30` | `0` disables reopen; every closed session is final. Does not apply to operator actions. |
| `NEW_SESSION_AFTER_HOURS` | `24` | Outer bound; clamps the reopen window. |
| `ENABLE_WELCOME_ON_NEW_SESSION` | `true` | Off, sessions begin with an answer and no greeting. |
| `ENABLE_REPEAT_WELCOME_AFTER_NEW_SESSION` | `true` | Off greets each customer once, ever. |
| `PREVENT_DUPLICATE_WELCOME` | `true` | Should stay on. |
| `PREVENT_DUPLICATE_CLOSING` | `true` | Should stay on; see above for why it is structural. |
| `RESET_IDLE_TIMER_ON_OUTGOING_MESSAGE` | `true` | Off, the timer measures customer silence only. |
| `CONVERSATION_CLOSING_MESSAGE` | *(empty)* | Empty uses `persona.CLOSING`. |
| `REJECT_STALE_INBOUND` | `true` | Off, a redelivered message is answered however old it is — see [A delivery that arrives late](#a-delivery-that-arrives-late). This is the switch to use if you ever need the gate gone; do not raise the bound instead. |
| `INBOUND_MAX_AGE_MINUTES` | `10` | Must stay **below** `CONVERSATION_REOPEN_WINDOW_MINUTES`. A test enforces it for the defaults. |

The last two live in `app/core/inbound_config.py`, not `app/config.py`, because
they govern whether a delivery is answered at all rather than how a session
behaves once it is. They are listed here because this is where their effect is
visible.

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
