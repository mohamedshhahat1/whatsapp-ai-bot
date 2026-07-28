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
uvicorn app.main:app --reload          # terminal 1
cd dashboard && npm install && npm run dev   # terminal 2 -> :5173
```

Vite proxies `/admin` to `localhost:8000`. The API also enables CORS for
`localhost:5173`, but only when `DEBUG=true`.

If `dashboard/dist` does not exist, the API logs `dashboard_not_built` at
startup and simply does not mount the route. Nothing else is affected.

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

The figures are an estimate of API spend. They exclude WhatsApp conversation
charges, which Meta bills separately.

### Embeddings are not counted

RAG ingestion calls the embeddings API, and those calls are not written to
`ai_logs`. Ingestion is a rare batch job costing cents per run, but the
dashboard total is chat spend, not the whole OpenAI bill.

## Most frequently asked questions

Questions are grouped by exact text after lowercasing and collapsing
whitespace, ignoring messages shorter than 8 characters so greetings do not
dominate.

This finds repeated *phrasings*, not repeated *meanings*. Two customers
asking the same thing in different words count separately. Real semantic
clustering would mean embedding each question and clustering the vectors --
worth doing once volume justifies it, since the embedding infrastructure is
already in place from the RAG work.

## Manual replies

`POST /admin/conversations/{id}/reply` sends as a human operator. The message
is persisted only after Meta accepts it, so a failed send leaves no phantom
entry in the transcript.

The endpoint refuses to send when more than 24 hours have passed since the
customer's last message, because Meta only allows free-form replies inside
that window. Outside it, an approved message template is required -- not yet
implemented.

Manual replies are stored as ordinary outbound messages, so they become part
of the context the model sees on the next turn. If an operator corrects the
bot, the bot sees the correction.

## Security

The dashboard authenticates with the same `ADMIN_API_KEY` as the rest of the
admin API, held in `sessionStorage` and sent as `X-API-Key`.

Be clear about what this is: a shared secret in a browser tab. It is
appropriate for