# The assistant's persona

The bot answers as شركة الكيان للتشطيبات والمقاولات العامة: Egyptian Arabic by
default, friendly, short, and forbidden from inventing a price.

Everything that defines that voice is in one file, `app/services/persona.py`.

## Instructions versus copy

The file holds two different kinds of text, and the difference decides how each
is used.

| | What it is | Who produces the final words |
| --- | --- | --- |
| `SYSTEM_PROMPT` | Instructions: identity, tone, honesty rules | The model, guided by them |
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

The Egyptian-Arabic phrasing in `WELCOME`, `NOT_UNDERSTOOD` and the handoff
patterns was written to match the dialect customers actually use. It deserves a
read by a native speaker who knows the business, especially the handoff
triggers in `app/services/handoff.py`, where a false positive silences the bot.
