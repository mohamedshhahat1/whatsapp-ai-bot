# WhatsApp AI Bot

Production AI-powered WhatsApp Business chatbot built with **Python 3.12**, **FastAPI**, **PostgreSQL + pgvector**, **Redis**, **Celery**, and the **OpenAI Responses API**.

## Features

- **WhatsApp Cloud API integration** - webhook verification, inbound text/image/document messages, outbound replies, delivery status & read receipts, Meta signature verification (`X-Hub-Signature-256`).
- **Durable background processing** - webhook deliveries are enqueued to **Celery + Redis** with late acks and exponential-backoff retries. A delivery that exhausts every retry is parked on a **dead-letter list** rather than disappearing.
- **RAG over your own documents** - PDFs in `knowledge/` are chunked, embedded and searched with pgvector; retrieved passages are injected as clearly fenced reference material. See [docs/RAG.md](docs/RAG.md).
- **Prompt-injection resistant prompts** - retrieved documents are data, never instructions, and an empty retrieval makes the model decline rather than invent a price.
- **Human handoff** - a customer who asks for a person gets one, and the bot goes completely silent on that conversation until an operator presses Resume AI. Detection is deterministic, so it does not depend on the model being up. See [docs/HANDOFF.md](docs/HANDOFF.md).
- **Cost analytics with historical pricing** - every call is costed at the price that was in force when it was made, from the `model_pricing` table. See [docs/PRICING.md](docs/PRICING.md).
- **Admin dashboard** - React + Vite SPA served at `/dashboard`: spend, usage, customers, transcripts, takeover, manual replies, search, knowledge base and price management. See [docs/DASHBOARD.md](docs/DASHBOARD.md).
- **Live conversation stream** - new customer messages are pushed to the dashboard over a WebSocket (fanned out through Redis so the Celery worker and every API replica can reach it) and the conversation opens itself in front of the operator.
- **Rate limiting** - Redis-backed limits (slowapi) shared across replicas, with proxy-aware client IP resolution that cannot be forged.
- **Retry strategy** - tenacity with exponential backoff + jitter on all OpenAI and Meta Graph API calls; only transient failures are retried.
- **Clean architecture** - routers -> services -> repositories -> models, with dependency injection and environment-based configuration.
- **Secrets without .env in production** - Docker secrets, `<NAME>_FILE` variables or Vault. See [docs/SECRETS.md](docs/SECRETS.md).
- **Observability** - structured JSON logs (structlog), Prometheus metrics (authenticated), Grafana dashboard, liveness and readiness probes.
- **Deployment** - Dockerfile, compose files for development and production, automatic migrations, container health checks, CI/CD. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Project structure

```
whatsapp-ai-bot/
├── app/
│   ├── core/            # logging, security, rate limiting, retries, exceptions, metrics, secrets, chunking, events
│   ├── db/              # engine, session, declarative base
│   ├── models/          # User, Conversation, Message, AILog, ModelPricing, Document, DocumentChunk
│   ├── repositories/    # data access layer (repository pattern)
│   ├── services/        # chat, conversation, prompts, retrieval, ingestion, handoff, admin, analytics, pricing, reply
│   ├── schemas/         # Pydantic request/response models
│   ├── integrations/    # whatsapp.py, openai.py, embeddings.py
│   ├── routers/         # webhook, admin, health, metrics, events (WebSocket)
│   ├── workers/         # Celery app + tasks (durable queue, dead-letter)
│   ├── middleware/      # request logging, metrics
│   ├── dependencies/    # FastAPI dependency wiring
│   ├── utils/           # tiktoken token counting & history trimming
│   ├── main.py          # app factory
│   └── config.py        # pydantic-settings configuration
├── alembic/versions/    # 0000 baseline -> 0001 knowledge -> 0002 pricing -> 0003 search/concurrency -> 0004 handoff
├── dashboard/           # React + Vite admin SPA
├── docs/                # RAG, PRICING, HANDOFF, DASHBOARD, DEPLOYMENT, SECRETS
├── knowledge/           # your PDFs (gitignored)
├── monitoring/          # Prometheus config + Grafana dashboard
├── nginx/nginx.conf
├── scripts/             # ingest_knowledge.py, init-secrets.sh
├── tests/
├── docker-compose.yml           # development
├── docker-compose.prod.yml      # production
├── Dockerfile
├── requirements.txt / requirements-dev.txt
└── .env.example
```

## Quick start (Docker)

```bash
cp .env.example .env      # fill in your keys
docker compose up --build -d
```

That is the whole sequence. A one-shot `migrate` service runs
`alembic upgrade head` before the app and worker start, so a fresh database is
ready with no manual step. **Never run `alembic revision --autogenerate` as
part of a deployment** - the schema is owned by the checked-in migrations.

The API is at `http://localhost:8000`, the dashboard at
`http://localhost:8000/dashboard` (sign in with `ADMIN_API_KEY`), and Swagger
at `/docs` when `DEBUG=true`.

To answer from your own documents, drop PDFs into `knowledge/` and index them:

```bash
docker compose exec app python scripts/ingest_knowledge.py
```

## Local development

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
# in a second terminal (or set USE_TASK_QUEUE=false to skip the worker):
celery -A app.workers.celery_app.celery_app worker --loglevel=info -Q webhooks
```

## Structured prompts

Each AI generation composes its instructions in layers
(`app/services/prompt_builder.py`):

```
SYSTEM_PROMPT                  # persona / base behaviour
+ Company information          # COMPANY_INFO env var
+ Retrieved knowledge (RAG)    # fenced, labelled as reference material
+ Conversation context         # customer name, channel, current time
+ Response rules               # language matching, chat style, price honesty
```

Conversation history and the current user message are passed separately as the
Responses API `input` list. They are never mixed into the instructions, which
keeps customer-authored text out of the trusted channel entirely.

**Retrieved documents are data, not orders.** Chunks are wrapped in a
`<retrieved_documents>` fence, fence delimiters are stripped from the content
so a document cannot close its own container, and the response rules state
that nothing inside it is an instruction. If retrieval finds nothing above the
similarity floor, the prompt says so explicitly and the model is told to admit
it does not know rather than estimate a price.

## Human handoff

A conversation is answered either by the bot or by a person, tracked in
`conversations.mode` (`bot` / `human`):

- A customer who writes "I want to speak to a representative", "can someone
  call me?" or the Arabic equivalents is switched to `human`, gets one
  acknowledgement, and is then left to a person.
- An operator can press **Take Over** on any conversation, and **Resume AI**
  to give it back.
- While a conversation is `human`, inbound messages are saved, marked read and
  pushed to the dashboard, but **no** OpenAI call is made and nothing is sent.

Ownership is a separate column from `status` on purpose: `status = 'active'` is
what the partial unique index and `active_for_user` use to find a customer's
thread, so overloading it would split a handed-off customer's history in two
and let the bot start answering in the new half. Full reasoning, the detection
limits and the API in [docs/HANDOFF.md](docs/HANDOFF.md).

## Background processing

`POST /webhook` deliveries are validated, ACKed immediately, and enqueued to
the `webhooks` Celery queue backed by Redis (AOF persistence enabled).

- `task_acks_late` + `task_reject_on_worker_lost` - a delivery is acknowledged
  only after successful processing; if a worker dies mid-task it is requeued.
- Automatic retries with exponential backoff and jitter, up to 5 attempts.
- Retries are idempotent: messages are deduplicated by `wa_message_id`.
- **Dead-letter queue**: after the last retry the raw payload is pushed onto
  the capped Redis list `DEAD_LETTER_KEY` and `webhook_dead_letters_total` is
  incremented, so a lost message is visible and replayable instead of silent.
- Each task runs its own event loop and closes everything it opened - OpenAI
  client, WhatsApp client and database engine - so long-running workers do not
  leak connection pools.
- Once a turn is committed, the worker publishes a dashboard event to Redis so
  connected operators see it immediately.
- For development, `USE_TASK_QUEUE=false` falls back to in-process
  `BackgroundTasks` (no worker, no durability).

Scale workers independently: `docker compose up -d --scale worker=3`.

Inspect the dead-letter list:

```bash
docker compose exec redis redis-cli lrange webhooks:dead-letter 0 -1
```

## Rate limiting

Redis-backed fixed-window limits (slowapi), shared across all replicas:

- `GET/POST /webhook` - `RATE_LIMIT_WEBHOOK` (default `600/minute`) per client.
- `/admin/*` - `RATE_LIMIT_ADMIN` (default `60/minute`) per client.
- Exceeding a limit returns `429`.
- The `/ws/events` upgrade is **not** limited - slowapi covers HTTP routes
  only. See the security note in [docs/DASHBOARD.md](docs/DASHBOARD.md).

The client identity comes from the last `TRUSTED_PROXY_HOPS` entries of
`X-Forwarded-For`, not the left-most one. The left of that header is written
by the client and is forgeable: trusting it lets a caller mint a fresh quota
per request. Set `TRUSTED_PROXY_HOPS=0` when nothing proxies the app.

## Health checks

| Endpoint | Meaning | Depends on |
| --- | --- | --- |
| `GET /health` | Liveness: the process is up | nothing |
| `GET /health/ready` | Readiness: this replica can serve | database + Redis, checked concurrently; `503` when either is down |

Liveness deliberately checks nothing external, so a Redis blip does not
restart every container. Compose uses readiness for the app container and
`celery inspect ping` for the worker.

## Metrics

Prometheus metrics are served at `GET /metrics` and are **not public**:

- In-cluster scrapes (private peer, no `X-Forwarded-For`) are allowed, because
  Prometheus cannot attach a custom header in its scrape config.
- Anything arriving through the proxy must present `ADMIN_API_KEY`.
- nginx refuses `/metrics` outright, so there are two layers.

Exposed: `whatsapp_messages_total`, `openai_requests_total`,
`openai_tokens_total`, `openai_response_seconds`, `http_requests_total`,
`app_errors_total`, `webhook_dead_letters_total`. Spend is deliberately *not* a
metric - it lives in `model_pricing` so history stays correct.

## Environment variables

| Variable | Description | Default |
|---|---|---|
| `ENVIRONMENT` | `development` / `production` | `development` |
| `DEBUG` | Enables `/docs`, debug logging and dev CORS | `false` |
| `DATABASE_URL` | Async SQLAlchemy URL (`postgresql+asyncpg://...`) | local |
| `REDIS_URL` | Redis connection URL | local |
| `USE_TASK_QUEUE` | Celery (`true`) or in-process (`false`) | `true` |
| `CELERY_BROKER_URL` | Celery broker (defaults to `REDIS_URL`) | empty |
| `CELERY_RESULT_BACKEND` | Celery result backend (defaults to `REDIS_URL`) | empty |
| `RATE_LIMIT_ENABLED` | Enable Redis-backed rate limiting | `true` |
| `RATE_LIMIT_WEBHOOK` | Limit for `/webhook` | `600/minute` |
| `RATE_LIMIT_ADMIN` | Limit for `/admin/*` | `60/minute` |
| `TRUSTED_PROXY_HOPS` | Proxies that append to `X-Forwarded-For` | `1` |
| `RETRY_MAX_ATTEMPTS` | Max attempts per outbound call | `3` |
| `RETRY_BACKOFF_MAX_SECONDS` | Backoff cap between attempts | `8` |
| `DEAD_LETTER_KEY` | Redis list holding exhausted deliveries | `webhooks:dead-letter` |
| `DEAD_LETTER_MAX_ENTRIES` | Cap on that list | `1000` |
| `METRICS_ENABLED` | Mount `/metrics` and the metrics middleware | `true` |
| `WORKER_METRICS_PORT` | Port the worker exposes metrics on | `9100` |
| `OPENAI_INPUT_PRICE_PER_1M` | **Fallback only** input price | `0.40` |
| `OPENAI_OUTPUT_PRICE_PER_1M` | **Fallback only** output price | `1.60` |
| `SECRETS_DIR` | Docker secrets directory | `/run/secrets` |
| `VAULT_ENABLED` | Read secrets from HashiCorp Vault | `false` |
| `VAULT_ADDR` / `VAULT_KV_MOUNT` / `VAULT_SECRET_PATH` | Vault location | empty / `secret` / empty |
| `RAG_ENABLED` | Retrieve from the knowledge base | `true` |
| `KNOWLEDGE_DIR` | Folder ingested by the indexer | `knowledge` |
| `EMBEDDING_MODEL` | Embedding model (must match what was indexed) | `text-embedding-3-small` |
| `EMBEDDING_DIMENSIONS` | Vector width (must match migration 0001) | `1536` |
| `EMBEDDING_BATCH_SIZE` | Texts per embeddings call | `64` |
| `CHUNK_MAX_TOKENS` | Chunk size | `400` |
| `CHUNK_OVERLAP_TOKENS` | Overlap between chunks | `60` |
| `RAG_TOP_K` | Chunks injected per message | `5` |
| `RAG_MIN_SCORE` | Cosine similarity floor | `0.25` |
| `RAG_MAX_CONTEXT_CHARS` | Hard cap on injected context | `6000` |
| `OPENAI_API_KEY` | OpenAI API key | empty |
| `OPENAI_MODEL` | Model used by the Responses API | `gpt-4.1-mini` |
| `SYSTEM_PROMPT` | Base assistant persona | generic |
| `COMPANY_INFO` | Company facts injected into the prompt | empty |
| `MAX_OUTPUT_TOKENS` | Max tokens per reply | `512` |
| `MAX_CONTEXT_MESSAGES` | Max history messages | `20` |
| `MAX_CONTEXT_TOKENS` | Token budget for history | `6000` |
| `WHATSAPP_TOKEN` | Cloud API access token | empty |
| `WHATSAPP_PHONE_NUMBER_ID` | Sender phone number ID | empty |
| `WHATSAPP_VERIFY_TOKEN` | Webhook verification token | `change-me` |
| `WHATSAPP_APP_SECRET` | Verifies Meta signatures | empty |
| `WHATSAPP_API_VERSION` | Graph API version | `v21.0` |
| `ADMIN_API_KEY` | Key for `/admin/*`, `/ws/events` and external `/metrics` | `change-me` |

In production the six credentials in `REQUIRED_IN_PRODUCTION` must come from a
real secret backend; the app refuses to boot with placeholder values.

The live dashboard stream needs no configuration of its own: it uses
`REDIS_URL` and `ADMIN_API_KEY`. Handoff has no settings either - the phrases
that trigger it are code, reviewed like code.

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness probe |
| `GET` | `/health/ready` | Readiness probe (database + Redis) |
| `GET` | `/metrics` | Prometheus metrics (in-cluster or admin key) |
| `GET` | `/webhook` | Meta webhook verification |
| `POST` | `/webhook` | Inbound messages & status updates |
| `GET` | `/admin/users` | List customers |
| `GET` | `/admin/conversations` | List conversations |
| `GET` | `/admin/conversations/{id}` | Conversation with messages |
| `DELETE` | `/admin/conversations/{id}` | Delete a conversation |
| `POST` | `/admin/conversations/{id}/reply` | Manual operator reply (`409` outside the 24h window) |
| `POST` | `/admin/conversations/{id}/takeover` | Take over: the bot stops answering this conversation |
| `POST` | `/admin/conversations/{id}/resume-ai` | Hand the conversation back to the bot |
| `GET` | `/admin/stats` | Usage statistics |
| `GET` | `/admin/search?q=` | Message body search |
| `GET` | `/admin/analytics/overview?days=` | Headline KPIs and spend |
| `GET` | `/admin/analytics/daily?days=` | Per-day usage and cost |
| `GET` | `/admin/analytics/models?days=` | Spend per model |
| `GET` | `/admin/analytics/questions?days=` | Most frequent questions |
| `GET` | `/admin/analytics/customers` | Per-customer activity |
| `GET` | `/admin/pricing` | Token price history |
| `POST` | `/admin/pricing` | Record a new price period (`409` on duplicate) |
| `DELETE` | `/admin/pricing/{id}` | Delete a price period |
| `GET` | `/admin/knowledge` | Indexed RAG documents |
| `GET` | `/admin/knowledge/search?q=` | Retrieval preview |
| `WS` | `/ws/events` | Live conversation activity (admin key as the first frame) |
| `GET` | `/dashboard` | Admin SPA |

Admin endpoints require `X-API-Key: <ADMIN_API_KEY>`.

## Database & migrations

```
0000_initial_schema        users, conversations, messages, ai_logs
0001_knowledge_base        documents, document_chunks, pgvector + HNSW index
0002_model_pricing         model_pricing, seeded from the epoch
0003_search_and_concurrency  pg_trgm GIN index on messages.content,
                             partial unique index: one active conversation
                             per customer
0004_conversation_handoff    conversations.mode / assigned_operator /
                             handoff_at - ownership, kept separate from status
```

`alembic upgrade head` on an empty database produces the complete schema.

## Tests

```bash
pytest
```

The suite runs against a real PostgreSQL (with pgvector) and Redis, which is
what CI provides; the migrations are applied before pytest runs. Tests that
need a database skip themselves when one is not reachable, so `pytest` still
works on a bare checkout - it simply covers less. The event fan-out test skips
without Redis for the same reason.

## Connecting WhatsApp (Meta)

1. Create a Meta app with the **WhatsApp** product; note the token, phone
   number ID and app secret.
2. Expose your server publicly (in dev: `ngrok http 8000`).
3. In *WhatsApp -> Configuration*, set the callback URL to
   `https://<your-domain>/webhook` and the verify token to
   `WHATSAPP_VERIFY_TOKEN`.
4. Subscribe to the `messages` webhook field.

## Extension points

CRM integration (new module in `integrations/`), voice messages and image
understanding (extend `chat_service`), appointment booking (tool calling is
already wired into `integrations/openai.py`), semantic clustering of frequent
questions (the embedding infrastructure exists), per-operator accounts with an
operator id on replies and handoffs, a handoff SLA alert, and message templates
for replies outside the 24-hour window.
