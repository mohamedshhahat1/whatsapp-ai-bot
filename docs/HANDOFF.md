# Human handoff

The bot used to answer everything, including "I want to speak to a
representative". That is the one message where an AI reply is worse than no
reply at all.

A conversation now has an owner. While a human owns it, the bot does not
generate anything for that conversation -- not an answer, not an apology, not
even its "I can't process voice notes" line.

## Two axes, not one state machine

The obvious implementation is a third `status` value: `active`, `handoff`,
`closed`. It is also a trap, and worth explaining because the old version of
`docs/DASHBOARD.md` recommended exactly that.

`status` is load-bearing. Migration `0003` creates

```sql
CREATE UNIQUE INDEX uq_active_conversation_per_user
    ON conversations (user_id) WHERE status = 'active';
```

and `ConversationRepository.active_for_user` finds a customer's thread with
`WHERE status = 'active'`. Set `status = 'handoff'` and that conversation stops
being the customer's active one. The next inbound message would then find no
active conversation, create a **second** one, and the results would be:

- the transcript splits in half, so the operator answers in one thread while
  the customer's later messages land in another;
- the new conversation is in bot mode, so **the bot starts answering again** --
  the exact failure the feature exists to prevent;
- the model's history for the new thread is empty, so it re-asks what the
  customer already explained.

Lifecycle and ownership are independent facts, so they are independent columns:

| Column | Question it answers | Values |
| --- | --- | --- |
| `status` | Is this thread open? | `active`, `archived` |
| `mode` | Who is answering it? | `bot`, `human` |

A handed-off conversation stays `status = 'active'` for its whole handoff. The
partial unique index keeps doing its job, and `active_for_user` deliberately
does **not** filter on `mode`.

## Schema

Migration `0004_conversation_handoff`:

| Column | Type | Notes |
| --- | --- | --- |
| `mode` | `varchar(16)` NOT NULL | `server_default 'bot'`, indexed |
| `assigned_operator` | `varchar(64)` NULL | Free text; there are no operator accounts |
| `handoff_at` | `timestamptz` NULL | When the *current* handoff started |

`server_default` rather than only a Python-side default, for two reasons: it
backfills existing rows, and `get_or_create_active` inserts through
`pg_insert` (for the `ON CONFLICT` clause), which bypasses ORM defaults
entirely.

`assigned_operator` and `handoff_at` are cleared when the AI resumes, so they
describe the present, not history. The transitions are in the structured logs
(`handoff_requested_by_customer`, `message_left_for_operator`); a real audit
trail would need its own table and per-operator accounts.

## The gate

```
customer message
     v
webhook -> Celery -> ChatService
     v
dedupe on wa_message_id
     v
save inbound message  +  mark as read
     v
mode == human ?  --yes-->  commit, notify the dashboard, STOP
     |                     (no OpenAI call, nothing sent)
     no
     v
asks for a human ?  --yes-->  mode = human, send one acknowledgement,
     |                        notify the dashboard, STOP
     no
     v
retrieve, generate, reply
```

The gate sits **after** the message is stored and marked read, and before any
model call. That ordering matters: a handed-off customer's messages are still
persisted, still marked read, and still announced to the dashboard in real
time. Only the generation is skipped. Nothing disappears.

It applies to all three inbound paths -- text, media (the request is often in a
photo's caption) and unsupported types. The unsupported-type path is the easy
one to forget: while a human owns the conversation, the automatic "please send
text" reply must not go out either.

## Detection

`app/services/handoff.py`. Deterministic regex over English and Arabic, not an
OpenAI intent classifier.

That is a deliberate choice. Asking the model to classify "stop talking to the
bot" puts the escalation path behind the thing being escalated away from: if
OpenAI is down, rate limited or out of budget, the one request that most needs
to work is the one that fails. Regex costs nothing, adds no latency to an
already-frustrating message, and is unit-tested in CI.

The cost is coverage, and the failure direction is chosen on purpose:

- **A missed phrasing** means the bot keeps helping a customer who wanted a
  person. Recoverable -- the operator sees the message in the dashboard.
- **A false positive** means the bot goes silent and a customer waits for a
  human who was never asked for. Much worse.

So patterns require intent, not keywords. `manager` alone does not match,
because "do you have a project manager for site visits?" is an ordinary
question for a finishing business; `speak to the manager` and `I want the
manager` do. `human` alone does not match either.

Matched today:

- speak/talk/chat/connect **to** a human, person, someone, agent,
  representative, operator, manager, supervisor, owner
- "real person", "human agent", "live agent", "live chat"
- "customer service" / "support" / "care"
- "call me", "phone me", "someone call me"
- want/need/get + manager, supervisor, representative, agent, operator
- "I don't want to interact with the bot", "stop the bot", "transfer me",
  "escalate"
- Arabic: a request word (aayez / aawez / ureed / mehtaag / momken) within a
  short distance of a person word (modeer / mowazzaf / mas'ool / bashar /
  insaan); a negation next to "bot"; "khedmat al-omalaa"; "kallemni"

Known gaps, stated rather than hidden:

- Arabic dialect spelling varies more than the pattern list does. Both `aryd`
  spellings are handled, but there is no diacritic or letter normalisation.
- The generic Arabic "someone" (`hadd`) is excluded: it is two letters and
  appears inside many unrelated words, so it produced false positives.
- "I want a human hair transplant" would match. Irrelevant for this business,
  and the fix (a negative lookahead per noun) costs more than it buys.
- Sarcasm, and asking for a person in the middle of a long paragraph about
  something else, are not detected.
- All Arabic in the source is written as `\uXXXX` escapes so the files stay
  pure ASCII and cannot be mangled by a tool that mishandles bidirectional
  text. Read the transliteration comments, not the escapes.

The keyword list is code, not configuration. It has no `.env` setting on
purpose: it needs review when it changes, and a regex list in an environment
variable is a syntax error waiting for production.

## The acknowledgement

One message is sent when a customer triggers a handoff, in Arabic and English
(no language detection has happened at that point):

> Someone from our team will reply to you shortly. / Thanks - I am passing you
> to a colleague. Someone will reply here shortly.

Then silence until an operator replies or presses Resume AI. Going quiet
immediately would leave someone who just asked for a person with no signal that
anything happened; a second automated message would be worse than the first.

It is sent **before** the transaction commits, matching the rest of
`ChatService`: if Meta rejects the send, the whole turn rolls back and Celery
retries it cleanly, rather than leaving a conversation flipped to `human` with
the customer never told.

An operator taking over from the dashboard sends **no** message. The customer
cannot tell the difference between the bot and a person, and announcing an
internal state change to them serves nothing.

## Operator controls

| Method | Path | Effect |
| --- | --- | --- |
| POST | `/admin/conversations/{id}/takeover` | `mode = human`, records the operator, stops the bot |
| POST | `/admin/conversations/{id}/resume-ai` | `mode = bot`, clears the operator |

Both are idempotent and return the conversation. The takeover body
(`{"operator": "Ahmed"}`) is optional -- omitting the name still stops the bot.

In the dashboard, the conversation list gains an "Answered by" column and the
transcript header gains a **Take Over** / **Resume AI** button plus a badge
showing the current owner. The operator name is asked for once and kept in
`localStorage`.

Be clear about what that name is: a label so two operators do not answer the
same customer. It is not an identity and not a permission -- anyone with the
admin key can take any conversation over, and the name is self-reported. Real
operator accounts are the prerequisite for treating it as anything more.

**Sending a manual reply does not take the conversation over.** The two actions
are separate on purpose: one clarifying message from an operator should not
permanently silence the assistant. If you want the bot to stop, press Take
Over.

## Resuming

Resume AI returns `mode` to `bot` and the next customer message is answered
normally. The operator's messages stay in the transcript as ordinary outbound
messages, so they are part of the history the model reads: if a person
corrected the bot, the bot sees the correction.

There is **no automatic resume**. A conversation handed to a human stays handed
to a human until someone presses the button, including overnight and over a
weekend. A timeout would silently hand a waiting customer back to the bot that
they explicitly rejected, which is a worse failure than a stale badge. If a
handoff SLA is wanted later, the honest version is an alert on conversations in
`human` mode with no operator reply for N hours -- not an auto-resume.

## Events

Ownership changes publish `conversation.handoff` on the existing
`dashboard:events` Redis channel:

```json
{
  "type": "conversation.handoff",
  "conversation_id": 42,
  "mode": "human",
  "assigned_operator": "Ahmed",
  "reason": "customer_asked_for_a_human",
  "at": "2026-01-01T12:00:00+00:00"
}
```

A separate event type rather than a field on `conversation.activity`, so a
dashboard can distinguish "a message arrived" from "the bot has stopped
answering this", and so the activity payload stays exactly four keys wide.
`reason` is one of `customer_asked_for_a_human`, `operator_took_over`,
`operator_resumed_ai`.

`assigned_operator` is staff data, not customer data -- it is the one thing the
bus has to carry, because a second operator's screen must show who already owns
the conversation.

## Tests

`tests/test_handoff.py`:

- every example phrasing is detected, and ordinary business questions
  (including "project manager") are not;
- with `mode = human`, a real webhook payload produces **zero** OpenAI calls
  and **zero** outbound messages, while the inbound message is still stored
  and marked read;
- asking for a representative produces exactly one outbound message (the
  acknowledgement, not an AI answer) and flips `mode` in the database, and a
  follow-up message is then left completely alone;
- after Resume AI the bot answers again, and `assigned_operator` /
  `handoff_at` are cleared;
- the endpoints work end to end, require the admin key, and return 404 for a
  conversation that does not exist.
