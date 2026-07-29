# The assistant's persona

The bot answers as شركة الكيان للتشطيبات والمقاولات العامة: Egyptian Arabic by
default, polite, short, and forbidden from inventing a price.

Everything that defines that voice is in one file, `app/services/persona.py`.

## Instructions versus copy

The file holds two different kinds of text, and the difference decides how each
is used.

| | What it is | Who produces the final words |
| --- | --- | --- |
| `SYSTEM_PROMPT` | Instructions: identity, style, honesty rules | The model, guided by them |
| `WELCOME` | Approved company copy | The code, verbatim |
| `NOT_UNDERSTOOD` | Approved company copy | The code, verbatim |

The welcome is not requested in the prompt. `ChatService` prepends it to the
reply. This matters more than it looks:

> "Always start with this welcome, and never repeat it" is a counting rule, and
> a language model holding twenty turns of history is not a counter. Asked
> nicely, it will eventually shorten the welcome, translate it, or send it
> twice to the one customer who mattered.

So the rule is enforced where rules can be enforced. `ChatService` asks the
database how many messages the customer has sent in this conversation
(`MessageRepository.count_inbound`). Exactly one means this is their first, and
only then is the welcome prepended. The prompt is *told* the welcome has
already been sent, so the model continues from it instead of greeting again.

## The style guide

| Rule | Where it lives |
| --- | --- |
| Respectful address: حضرتك, من فضلك, never انت | persona + response rules |
| Polite Egyptian register, no slang (يا باشا, يا معلم) | persona |
| At most one emoji per message | persona + response rules + first-message layer |
| About five short lines, one question at a time | persona + response rules |
| No invented company facts | persona + response rules |
| Offer a human when a question needs one | persona + response rules |
| Angry customer: apologise once, then fix or escalate | persona + response rules |
| Do not announce being an AI, never deny it | persona + response rules |

Two of these are not simply prose in the prompt.

**The emoji budget belongs to the message, not the model.** `WELCOME` already
spends it on the waving hand, and the code prepends that text, so a cheerful
first reply would arrive carrying two. The first-message layer therefore tells
the model to add none of its own, and `tests/test_style.py` asserts that
`WELCOME` itself contains exactly one emoji -- if the copy ever gains a second,
every opening message breaks the rule before the model writes a word.

**"Do not say you are an AI" is not permission to deny it.** The persona keeps
those two halves together: do not volunteer it, because nobody wants a preamble
before an answer about their apartment -- but if asked directly, answer
honestly, and never claim to be a human being or a named employee. A bot that
denies being a bot is a different product with a different legal problem, and
the test suite pins the honest half so it cannot be pruned as redundant.

**Anger routes to the existing escalation.** A complaint gets one apology
without excuses, no argument, and then either a fix or a transfer -- using the
same `HANDOFF_KEYWORD` as every other offer, not a second invented path. The
persona also forbids promising compensation, a discount or a revisit, because
those are a colleague's decision.

### Why the rules appear twice

Setting `SYSTEM_PROMPT` replaces the packaged persona wholesale. Any rule that
lives *only* in the persona is therefore lost the moment another business
reuses this codebase. Formality, length, the emoji budget, AI disclosure, anger
handling and the pricing rules are all repeated in the response rules layer,
which is always emitted, and a test asserts they survive a custom
`SYSTEM_PROMPT`.

## Why not SYSTEM_PROMPT

The persona is multi-paragraph text with newlines, bullets and an emoji. A
`.env` variable cannot hold that without escaping games, nothing code-reviews
it, and a copy-paste accident silently changes what every customer reads.

`SYSTEM_PROMPT` still overrides the packaged persona completely, so the
codebase can be reused for another business without editing Python. Setting it
is all-or-nothing: it replaces the persona rather than adding to it. Per-
business facts belong in `COMPANY_INFO` or the knowledge base instead.

Note that `.env.example` deliberately leaves `SYSTEM_PROMPT` commented out. It
used to ship a generic English value, which meant `cp .env.example .env` --
the first line of the quick start -- silently disabled the persona.

## The opening message

| First message from the customer | What is sent |
| --- | --- |
| A real question | welcome + the model's answer, one message |
| `مرحبا`, `Hello`, a greeting | welcome + one short line asking what they need |
| `.`, `...`, `؟`, `\U0001f44d`, empty | welcome + `NOT_UNDERSTOOD`, **no model call at all** |
| A photo or a document | welcome + acknowledgement (see the limits below) |
| An unsupported type (voice, location) | welcome + the "please send text" line |
| "I want a human" | the handoff acknowledgement, **without** the welcome |

That last row is deliberate: a service menu inviting questions would contradict
a message saying a colleague is taking over. See [HANDOFF.md](HANDOFF.md).

"Has no words" is decided by `is_unintelligible()`, which is deliberately
narrow: it is true only when a message contains no letter or digit in any
script. It does not try to judge whether a real sentence makes sense -- code
that guessed at meaning would silence answerable questions, and the persona
already tells the model to ask a clarifying question when intent is unclear.

## Where answers come from

Company-specific claims -- prices, contracts, guarantees, past projects -- may
only come from retrieved documents or `COMPANY_INFO`. General factual questions
about finishing work are answered from the model's own knowledge, labelled as
general information rather than a quotation. See [RAG.md](RAG.md) for the split
and for why refusing both was worse.

## What the bot cannot do yet

**It cannot see images, and it cannot read attachments.** `handle_media_message`
stores the file's `media_id` and passes the model a text placeholder -- the
fact that an image arrived, plus its caption. The bytes are never downloaded
and never sent to a model.

The persona therefore instructs it to acknowledge a photo, ask about area and
condition in words, and never describe what is "visible". This is not
timidity: an instruction to "analyse what is visible" would be followed, and
the model would produce a confident, invented description of a photo it never
received -- for a business quoting finishing work on real properties.

`tests/test_persona.py` pins this, so the claim cannot be re-added to the
persona without the test failing.

Making it real needs, in order: download media from the Graph API with the
`media_id` already stored, pass images to a vision-capable model as base64 or a
signed URL, and run OCR or a text extractor for PDFs (the ingestion pipeline in
`docs/RAG.md` already extracts PDF text, but only for files placed in
`knowledge/`, not for one arriving mid-conversation).

## Changing the wording

Edit `app/services/persona.py`. The file is intentionally written in real
Arabic rather than `\uXXXX` escapes, so the person who owns the wording can
proofread it.

`WELCOME` and `NOT_UNDERSTOOD` are sent exactly as written, including line
breaks and the bullet characters. `tests/test_persona.py` asserts on the
constants rather than on hard-coded strings, so rewording the copy does not
break the tests -- but it does change what every new customer sees, so treat it
as a product change, not a typo fix.

The style tests in `tests/test_style.py` do assert on short English phrases
from the prompt ("five short lines", "At most one emoji"). Rewording those
rules is fine; update the test in the same commit, and do not delete the
assertion instead.

The Egyptian-Arabic phrasing in `WELCOME`, `NOT_UNDERSTOOD` and the handoff
patterns was written to match the dialect customers actually use. It deserves a
read by a native speaker who knows the business, especially the handoff
triggers in `app/services/handoff.py`, where a false positive silences the bot.
