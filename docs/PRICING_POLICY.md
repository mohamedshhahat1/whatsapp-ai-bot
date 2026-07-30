# Pricing Policy

The bot never states a financial figure. Not a price, range, estimate,
per-metre rate, quotation, package price, discount, deposit, instalment or
budget number. This holds even when a figure exists in the knowledge base.

That last clause is the reason this is code and not a prompt paragraph.

## Why a prompt is not enough

Three things defeat a single instruction:

1. **A retrieved document containing a number.** The model has been told for
   its entire context that documents outrank its own knowledge. Now it is told
   to ignore the one source it was told to trust. That conflict resolves the
   wrong way often enough to matter.
2. **Persistence.** A customer who asks five times, reframes as "just roughly",
   or claims a competitor quoted them is running pressure that works on people.
3. **`SYSTEM_PROMPT`.** It replaces the packaged persona wholesale. A rule that
   must survive misconfiguration cannot live somewhere configuration deletes.

## The four enforcement points

| # | Where | What it does |
|---|-------|--------------|
| 1 | `price_policy.redact`, called by `PromptBuilder` | Strips amounts from retrieved chunks before the prompt is built. The model cannot repeat a figure it never saw. |
| 2 | `price_policy.instruction_layer`, appended last | Tells the model the rule and that it outranks the documents. |
| 3 | `price_policy.mentions_amount`, in `ChatService._generate_and_send` | Scans the generated reply. A reply containing an amount is **discarded** and replaced with approved copy. |
| 4 | `price_policy.INSIST_THRESHOLD`, in `ChatService.handle_text_message` | A customer who raises money three times is handed to a human sales representative. |

Only layer 2 is a prompt. Layers 1, 3 and 4 hold regardless of what the model
decides to do.

## Why layer 3 replaces the whole message

It does not edit the number out. A sentence with its figure removed reads as
evasive, and often leaves the amount implied by what surrounds it ("that would
be about that per metre"). Replacing the entire reply with approved copy is the
only version that cannot leak.

The model does not get a second attempt. Retrying invites a reply that phrases
the same number differently and passes the regex.

## The trade this makes

Layer 3 is deliberately loose, so it produces false positives. A reply
mentioning "5 years" near a currency word may be replaced by a pricing
deflection that answers a question nobody asked.

That is the intended direction. A deflection sent in error costs one awkward
message. A figure sent in error is a number the company never agreed to, in
writing, on a customer's phone.

Measurements, room counts and durations are covered by tests and stay clean.

## Configuration

```
SALES_PHONE=01000000000
```

Contact information, not a credential, so `.env` is fine and it is not in
`REQUIRED_IN_PRODUCTION`.

- **Set** - the bot gives the number and offers to take the customer's instead.
- **Unset** - the bot asks for the customer's number and says the Sales Manager
  will call. It never invents a number.

The number is exempted from layer 3, or the deflection would trip the gate it
exists to satisfy.

## What the bot can still do

Only money is closed. The bot describes freely what a package **includes**, how
work is done, which materials are used, how long things take, warranty scope
and past projects. `redact` removes figures and keeps the prose, so a pricing
document still answers "what's in the super lux package?" usefully.

## Consequences for the knowledge base

`knowledge/pricing/*.md` and `knowledge/offers/*.md` are still worth filling.
Their figures will never be quoted, but their scope, inclusions and exclusions
will be, and the sales team works from the same files.

The welcome message no longer advertises "know the prices" - it now offers a
quotation from the Sales Manager, which is the thing the company actually
provides.

## Tests

`tests/test_price_policy.py` - detection in both directions, redaction,
threshold behaviour, and the phone-number exemption.
