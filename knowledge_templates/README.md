# knowledge_templates/

The full document structure for the RAG knowledge base of
شركة الكيان للتشطيبات والمقاولات العامة, with every company fact left as a
marker for the owner to fill in.

## Why the facts are not written for you

The bot answers company questions **only** from these documents, and the
response rules permit it to state a price as soon as a matching document
exists. A plausible invented price list is therefore not a draft -- it is a
quotation the bot will send to a real customer in writing, with the company's
name on it, and neither the model nor the retrieval layer can tell it from a
real one.

So prices, phone numbers, addresses, warranty periods, payment terms and
project histories are left as `[[TODO]]`. The structure, the headings, the
searchable section titles and the question lists are all here; the facts have
to come from the company.

## The workflow

```bash
cp -r knowledge_templates/. knowledge/   # copy the structure across
$EDITOR knowledge/pricing/economy.md     # fill in the real figures
rm knowledge/README.md                   # this file is not a knowledge document
python scripts/ingest_knowledge.py       # index what is finished
```

`knowledge/` is gitignored on purpose: price lists and contracts stay out of
version control. `knowledge_templates/` is committed, because structure is not
confidential and is worth reviewing in a pull request.

## The guard

Ingestion **refuses** any file that still contains `[[TODO]]`:

```
 . pricing/economy.md      skipped    7 unfilled [[TODO]] placeholders
 + company/about.md        indexed    9 chunks
```

Skipped files are not an error and do not fail the run -- they are simply not
indexed, so the bot behaves as if they do not exist and says it has no
information rather than reciting a marker. If a file was indexed while filled
and a placeholder is later reintroduced, the guard also **deletes** it from the
vector store, so an old version cannot keep answering customers after somebody
took the document back into editing.

See `app/services/knowledge_guard.py`. Partial filling works: finish
`company/branches.md` today and it is live tonight, while the pricing files
wait.

## Filling a file in

- Replace the marker **and** the surrounding label if the label is wrong for
  this company. Nothing here is sacred except the heading structure.
- Delete whole sections that do not apply. An empty section retrieves as noise
  and competes with the section that does have the answer.
- Keep the headings. They are the retrieval surface: the chunker is heading
  aware, and a question phrased like a heading is the easiest kind to match.
- Write the way customers ask. "سعر متر المحارة" retrieves better than
  "تكلفة أعمال البياض", because the first is what someone types into
  WhatsApp.
- Repeat key terms naturally in the body rather than relying on the heading
  alone. Chunks are retrieved individually, and a chunk that never names the
  service it describes is hard to find.
- Put every price in a table row with its unit (`جنيه/م²`, `جنيه/مطول`).
  A number with no unit is the single most common cause of a wrong answer.
- Date anything volatile. Offers and prices should carry a validity line, so a
  stale figure is visible rather than invisible.

## What must not go in here

- Customer names, phone numbers or addresses. Anything retrieved can be quoted
  back to a different customer.
- Internal costs, supplier margins or staff salaries. The bot has no notion of
  a confidential chunk: if it is indexed, it is answerable.
- Anything you would not accept being quoted verbatim, out of context, in
  writing, to a stranger.
