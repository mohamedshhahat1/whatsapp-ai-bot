# Model pricing history

## The problem

The OpenAI API returns token counts, never a price. Spend has to be derived,
and the first version of the dashboard derived it from two settings values:

```
cost = prompt_tokens / 1e6 * OPENAI_INPUT_PRICE_PER_1M
     + completion_tokens / 1e6 * OPENAI_OUTPUT_PRICE_PER_1M
```

That is correct only while the price never changes. Switch from a cheap model
to an expensive one in six months and every historical figure is recomputed at
the new rate: last quarter's spend silently inflates, the monthly trend line
bends, and the numbers no longer match what the bank actually charged.

The bug is subtle because nothing errors. The report still renders, it is just
wrong, and it is wrong in a way you cannot detect by looking at it.

## The fix

Prices are now rows in `model_pricing`, not settings:

| model | input_price_per_1m | output_price_per_1m | effective_from | note |
| --- | --- | --- | --- | --- |
| gpt-4.1-mini | 0.400000 | 1.600000 | 1970-01-01 | Seeded from settings |
| gpt-4.1-mini | 0.300000 | 1.200000 | 2026-09-01 | Price drop |
| gpt-4.1 | 2.000000 | 8.000000 | 2027-01-15 | Upgraded model |

Each row means "from this instant, this model costs this much". Rows are never
edited, only superseded by a newer `effective_from`. A call made in August is
costed at 0.40, a call made in October at 0.30, forever.

### Why the epoch seed

The migration inserts one row for your configured model dated `1970-01-01`.
Without it, calls older than your first real price row would match nothing.
Starting the first period at the beginning of time means every historical row
is covered, and the seeded rate is exactly what those rows were already being
costed at -- so applying this migration does not move a single number.

### Why Numeric and not Float

Prices are money. Binary floating point cannot represent 0.40 exactly, and the
error compounds across millions of tokens. The columns are `NUMERIC(12, 6)`,
and the service converts through `str()` before `Decimal`, because
`Decimal(0.4)` is `0.4000000000000000222...` while `Decimal("0.4")` is exactly
`0.4`.

## How the lookup works

Each `ai_logs` row is matched to its price with an as-of join:

```sql
SELECT sum(
         l.prompt_tokens  / 1000000.0 * p.input_price_per_1m
       + l.completion_tokens / 1000000.0 * p.output_price_per_1m
       )
FROM ai_logs l
LEFT JOIN LATERAL (
    SELECT input_price_per_1m, output_price_per_1m
    FROM model_pricing mp
    WHERE mp.model = l.model
      AND mp.effective_from <= l.created_at
    ORDER BY mp.effective_from DESC
    LIMIT 1
) p ON true
WHERE l.created_at >= :since;
```

Three properties matter here:

- **`LIMIT 1` picks the latest price at or before the call.** That is the
  whole temporal lookup, in one clause.
- **`LEFT JOIN LATERAL` cannot change the row count.** Counts, averages and
  percentiles computed in the same query stay correct. A plain join to
  `model_pricing` would have matched every historical price row and multiplied
  the results.
- **Unmatched rows fall back to the settings values** via `COALESCE`, so an
  empty table or an unpriced model degrades to the old behaviour instead of
  reporting zero.

The index on `(model, effective_from)` serves the lookup. It is ascending;
Postgres scans a btree backwards at the same speed, so no `DESC` index is
needed.

## Using it

Open **Model pricing** in the dashboard, or use the API:

```bash
# Record a price change
curl -X POST http://localhost:8000/admin/pricing \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "gpt-4.1",
        "input_price_per_1m": 2.0,
        "output_price_per_1m": 8.0,
        "effective_from": "2027-01-15T00:00:00Z",
        "note": "Switched to the full model"
      }'

# See the history
curl -H "X-API-Key: $ADMIN_API_KEY" http://localhost:8000/admin/pricing

# Spend per model over the last 30 days
curl -H "X-API-Key: $ADMIN_API_KEY" \
  "http://localhost:8000/admin/analytics/models?days=30"
```

**When you change `OPENAI_MODEL`, add a pricing row for the new model.** If
you forget, its calls fall back to the settings prices, which are probably the
old model's -- wrong, but quietly so. The per-model breakdown is the place to
catch this: a model showing calls but implausible cost has no pricing row.

Deleting a row genuinely does rewrite history for the period it covered. It is
for correcting a typo, not for retiring a price. To retire one, add a newer
row.

## What this still does not cover

- **Cached input tokens** are billed at a discount by OpenAI and are not
  tracked separately in `ai_logs`, so cached calls are costed as if they were
  full price. Fixing this means recording cached token counts at the point of
  the API call.
- **Embedding calls** during RAG ingestion never reach `ai_logs`, so they are
  absent from every total. Cents per run, but not zero.
- **WhatsApp conversation charges** are billed by Meta and are outside this
  system entirely.
