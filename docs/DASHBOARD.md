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

| Screen | What it shows |
| --- | --- |
| Overview | Spend, projected monthly cost, tokens, cost per conversation, average and p95 response time, error rate, daily usage chart, most frequently asked questions |
| Customers | Every customer with conversation count, message count and last activity |
| Conversations | Conversation list, full transcript, and manual operator reply |
| Search | Substring search across all inbound and outbound messages |
| Knowledge base | Indexed documents and a retrieval tester |

## Endpoints behind it

All require the `X-API-Key` header and are rate limited by `ADMIN_LIMIT`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/admin/analytics/overview?days=30` | Headline KPIs and cost breakdown |
| GET | `/admin/analytics/daily?days=30` | Per-day messages, tokens, latency, cost |
| GET | `/admin/analytics/questions?days=30&limit=10` | Most frequently asked questions |
| GET | `/admin/analytics/customers?limit=50` | Conversations and messages per customer |
| GET | `/admin/search?q=...` | Message body search |
| POST | `/admin/conversations/{id}/reply` | Manual operator reply |
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

The OpenAI API does not return a price, only token counts. Spend is derived:

```
cost = prompt_tokens / 1e6 * OPENAI_INPUT_PRICE_PER_1M
     + completion_tokens / 1e6 * OPENAI_OUTPUT_PRICE_PER_1M
```

Both rates live in settings and default to `gpt-4.1-mini` pricing.

**Consequence worth knowing:** costs are recomputed from current prices
every time you load the page. Change a price and historical figures change
too. That is the right trade-off while you are on one model, but if you ever
run several models at once, price should be resolved per `ai_logs.model`
instead of from a single global setting.

The figures estimate OpenAI API spend only. They exclude WhatsApp
conversation charges, which Meta bills separately.

### Embeddings are not counted

RAG ingestion calls the embeddings API, and those calls are not written to
`ai_logs`. Ingestion is a rare batch job costing cents per run, but be aware
the dashboard total is chat spend, not your whole OpenAI bill.

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

The endpoint refuses to send when more than 24 hours have passed since the
customer's last message, because Meta only allows free-form replies inside
that window. Outside it an approved message template is required, which is
not implemented yet.

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

Per-customer counts use correlated subqueries rather than joins. Joining
users to conversations and messages at once multiplies rows and inflates
both counts -- the classic join fan-out bug.

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
