# Admin dashboard

A React + Vite single-page app served by FastAPI at `/dashboard`.

## Why Vite and not Next.js

Next.js is a server framework. Using it would mean running a second
always-on Node process next to the FastAPI app, plus its own Dockerfile,
health check, log stream and deploy step -- for a dashboard that renders
JSON from an API you already own.

Vite builds to plain static files. Node appears only in the Docker build
stage; the production image is still a single Python container. Nothing new
to deploy, monitor or secure.

Next.js would start paying off if the dashboard needed SEO (it must not be
public), server-side rendering for first-paint speed (it is an internal
tool), or its own backend routes (it has one already).

## Screens

| Screen | What it shows | Auto refresh |
| --- | --- | --- |
| Overview | Spend, projected monthly cost, tokens, cost per conversation, average and p95 response time, error rate, daily usage chart, most frequently asked questions | 60s |
| Customers | Every customer with conversation count, message count and last activity | 60s |
| Conversations | Conversation list, full transcript, and manual operator reply | 30s list / 10s transcript |
| Search | Substring search across all inbound and outbound messages | on demand |
| Knowledge base | Indexed documents and a retrieval tester | on demand |
| Pricing | Token price history per model, and spend per model | on demand |

## Loading, error, empty and refresh states

All data fetching goes through one hook, `useAsync` in
`dashboard/src/components/Async.tsx`, so every screen behaves the same way:

- **Loading** - shown only when there is nothing on screen yet.
- **Refreshing** - a background poll shows a small "Updating..." marker
  instead of replacing the page with a spinner, so numbers do not flicker
  every interval.
- **Error** - a failed refresh keeps the data already displayed rather than
  blanking a working dashboard.
- **Empty** - each list states that it is empty rather than rendering an
  empty table.

Two details that polling makes necessary: responses are matched to a request
id, so a slow earlier response cannot land after a newer one and show stale
numbers; and intervals are cleared on unmount.

**Why polling and not WebSockets.** The screens are read-mostly and used by
one or two operators. A socket would add a second transport, reconnect and
backoff logic, and server-side connection state, to save a handful of
requests a minute. Live transcripts are the only place latency is felt, and
10 seconds is enough there. If the tool ever grows to many operators watching
live conversations, a socket on the transcript view alone would be the change
to make.

The Pricing screen does not poll: it is a configuration form, and rows moving
underneath someone who is typing would be worse than slightly stale prices.

## Endpoints behind it

All require the `X-API-Key` header and are rate limited by `ADMIN_LIMIT`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/admin/analytics/overview?days=30` | Headline KPIs and cost breakdown |
| GET | `/admin/analytics/daily?days=30` | Per-day messages, tokens, latency, cost |
| GET | `/admin/analytics/models?days=30` | Spend per model |
| GET | `/admin/analytics/questions?days=30&limit=10` | Most frequently asked questions |
| GET | `/admin/analytics/customers?limit=50` | Conversations and messages per customer |
| GET | `/admin/conversations?limit=50` | Conversation list |
| GET | `/admin/conversations/{id}` | Full transcript |
| POST | `/admin/conversations/{id}/reply` | Manual operator reply |
| GET | `/admin/search?q=...` | Message body search |
| GET | `/admin/pricing` | Price history |
| POST | `/admin/pricing` | Add a price period |
| DELETE | `/admin/pricing/{id}` | Remove a price period |
| GET | `/admin/knowledge` | Indexed RAG documents |
| GET | `/admin/knowledge/search?q=...` | Retrieval preview |

## Running it

### Production

The Docker build compiles the dashboard automatically:

```bash
docker compose up -d --build
# then open http://localhost:8000/dashboard
```

Sign in with the value of `ADMIN_API_KEY`.

### Local development

```bash
uvicorn app.main:app --reload                 # terminal 1
cd dashboard && npm install && npm run dev    # terminal 2 -> :5173
```

Vite proxies `/admin` to `localhost:8000`. The API also enables CORS for
`localhost:5173`, but only when `DEBUG=true`.

If `dashboard/dist` does not exist, the API logs `dashboard_not_built` at
startup and simply does not mount the route. Nothing else is affected, so
the backend still runs fine for anyone who never builds the frontend.

## How costs are calculated

The OpenAI API returns token counts, never a price. Spend is derived in SQL,
per call, using the price that was in force **at the moment of the call**:

```
cost = prompt_tokens     / 1e6 * <input price effective at created_at>
     + completion_tokens / 1e6 * <output price effective at created_at>
```

Rates come from the `model_pricing` table, matched with a LATERAL join. That
is the single source of truth - see [PRICING.md](PRICING.md). Adding a new
price period never changes past figures.

The two `OPENAI_*_PRICE_PER_1M` settings are a **fallback only**, used if a
call's model has no pricing row at all. Migration `0002` seeds rows from the
epoch, so in practice the fallback is unreachable.

There is deliberately no Prometheus counter for spend. One existed, computed
from the settings prices, and it disagreed with the dashboard as soon as a
price changed. Two numbers for one fact is worse than one number in one place.

The figures estimate OpenAI API spend only. They exclude WhatsApp
conversation charges, which Meta bills separately.

### Embeddings are not counted

RAG ingestion calls the embeddings API, and those calls are not written to
`ai_logs`. Ingestion is a rare batch job costing cents per run, but be aware
the dashboard total is chat spend, not your whole OpenAI bill.

## Time windows

Every figure is either lifetime or scoped to the selected period, and the
names say which: `total_*` is lifetime, while `new_*`, `active_*` and
`*_in_period` follow the `days` parameter.

This distinction is load-bearing. Cost per conversation divides window spend
by the conversations **active in that same window**. Dividing 30 days of
spend by the lifetime conversation count made the number sink a little
further every month while nothing had actually changed.

## Most frequently asked questions

Questions are grouped by exact text after lowercasing and collapsing
whitespace, ignoring messages shorter than 8 characters so that greetings do
not dominate the list.

This finds repeated *phrasings*, not repeated *meanings*. Two customers
asking the same thing in different words count separately. Real semantic
clustering would mean embedding each question and clustering the vectors --
worth doing once volume justifies it, since the embedding infrastructure is
already in place from the RAG work.

Use the list to decide what belongs in `knowledge/`: a question asked twenty
times is a question your PDFs should answer without the model improvising.

## Manual replies

`POST /admin/conversations/{id}/reply` sends as a human operator. The message
is persisted only after Meta accepts it, so a failed send leaves no phantom
entry in the transcript.

The endpoint returns **409 Conflict** when more than 24 hours have passed
since the customer's last message, because Meta only allows free-form replies
inside that window. Outside it an approved message template is required,
which is not implemented yet. It is a 409 and not a 500 on purpose: the
request is valid, the conversation is simply in a state that forbids it, and
the dashboard shows the explanation verbatim.

Manual replies are stored as ordinary outbound messages, so they become part
of the context the model sees on the next turn. If an operator corrects the
bot, the bot sees the correction.

There is no takeover flag: the bot keeps answering new inbound messages even
while an operator is typing. If you want a real handover mode, the natural
place is the existing `conversations.status` column -- set it to `human` and
have `ChatService` skip generation for those conversations.

## Performance

Every figure is a SQL aggregate; nothing is summed in Python. `ai_logs` and
`messages` both have an index on `created_at`, which is what the date filters
and `date_trunc` grouping rely on.

The overview's seven volume counts are evaluated as scalar subqueries in a
single statement rather than seven awaited `COUNT(*)` round trips. Besides
being faster, it gives every figure the same snapshot of the data.

Per-customer counts are one grouped aggregate with
`count(DISTINCT conversation_id)`, which is what keeps the join fan-out from
inflating the conversation count. An earlier version used correlated
subqueries per row; ordering by last activity meant the `LIMIT` could not
short-circuit them, so every user was evaluated to render one page.

Message search is `ILIKE '%term%'`. A leading wildcard rules out a B-tree, so
it is served by the `pg_trgm` GIN index `ix_messages_content_trgm` from
migration `0003`. Search terms are escaped: without that, searching for
"50%" returned every message in the database, confidently and silently.

The queries scan the full period each time they are called. At a few hundred
thousand `ai_logs` rows that is still fast. Well beyond that, the answer is a
nightly rollup table of daily totals rather than a bigger database.

## Security

The dashboard authenticates with the same `ADMIN_API_KEY` as the rest of the
admin API, held in `sessionStorage` and sent as `X-API-Key`.

Be clear about what this is: a shared secret in a browser tab. It is
reasonable for a single operator on an internal tool, and it is the same key
that already protects the admin API. It is not a user system -- there are no
accounts, no per-person permissions, and no audit trail of which operator
sent which manual reply.

Before more than one or two people use this, three things should change:
real user accounts with sessions, an operator id recorded on manual replies,
and HTTPS enforced at nginx. Until then, do not expose port 8000 publicly --
the production compose file already binds it to `127.0.0.1`.

The dashboard also displays full customer conversations, which for a
finishing and renovation business means names, phone numbers, addresses and
quoted prices. Treat access to it as access to your customer database.
