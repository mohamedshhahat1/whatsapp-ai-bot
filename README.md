# WhatsApp AI Bot

Production AI customer-support platform for **WhatsApp Business** and
**Facebook Messenger**, built with **Python 3.12**, **FastAPI**,
**PostgreSQL + pgvector**, **Redis**, **Celery** and the **OpenAI Responses
API**.

One AI engine serves every channel. A channel adapter normalises inbound
events into a single internal shape and sends replies back out the way they
came; nothing above the adapter layer knows which network a customer is on.

## Features

- **WhatsApp Cloud API integration** - webhook verification, inbound text/image/document messages, outbound replies, delivery status & read receipts, Meta signature verification (`X-Hub-Signature-256`).
- **Facebook Messenger** - a second inbound path at `/webhook/meta` that normalises Meta events into the same internal event the WhatsApp path produces, so prompts, RAG, handoff, sessions and analytics are shared rather than duplicated. Disabled by default; see [Channels](#channels).
- **Durable background processing** - webhook deliveries are enqueued to **Celery + Redis** with late acks and exponential-backoff retries. A delivery that exhausts every retry is parked on a **dead-letter list** rather than disappearing.
- **RAG over your own documents** - PDFs in `knowledge/` are chunked, embedded and searched with pgvector; retrieved passages are injected as clearly fenced reference material. See [docs/RAG.md](docs/RAG.md).
- **Prompt-injection resistant prompts** - retrieved documents are data, never instructions, and an empty retrieval makes the model decline rather than invent a price.
- **Reviewed persona with a guaranteed welcome** - the assistant's identity and the approved Arabic welcome live in version-controlled code, and the welcome is sent by the code exactly once per conversation rather than being requested in the prompt. See [docs/PERSONA.md](docs/PERSONA.md).
- **Session lifecycle** - conversations open, greet, go idle, close with a farewell and reopen inside a grace window, driven by a scheduled sweep rather than by the model. See [docs/SESSION_LIFECYCLE.md](docs/SESSION_LIFECYCLE.md).
- **Human handoff** - a customer who asks for a person gets one, and the bot goes completely silent on that conversation until an operator presses Resume AI. Detection is deterministic, so it does not depend on the model being up. See [docs/HANDOFF.md](docs/HANDOFF.md).
- **Named operator accounts and an append-only audit log** - operators sign in with their own credentials, and every state-changing admin action records who performed it in a table a database trigger refuses to let anyone update or delete. See [Operator accounts](#operator-accounts-and-the-audit-log).
- **Cost analytics with historical pricing** - every call is costed at the price that was in force when it was made, from the `model_pricing` table. See [docs/PRICING.md](docs/PRICING.md).
- **Spend and abuse protection** - per-customer minute/hour/day quotas, flood and duplicate detection, temporary blocks, and a daily spend breaker. Every check fails open: a cache blip must not stop the business answering customers.
- **Admin dashboard** - React + Vite SPA served at `/dashboard`: spend, usage, customers, transcripts, takeover, manual replies, search, knowledge base and price management. See [docs/DASHBOARD.md](docs/DASHBOARD.md).
- **Flutter operator app** - the same operations from a phone, with push notifications for new customer messages. See [docs/MOBILE.md](docs/MOBILE.md) and [docs/PUSH_NOTIFICATIONS.md](docs/PUSH_NOTIFICATIONS.md).
- **Live conversation stream** - new customer messages are pushed to the dashboard over a WebSocket (fanned out through Redis so the Celery worker and every API replica can reach it) and the conversation opens itself in front of the operator.
- **Rate limiting** - Redis-backed limits (slowapi) shared across replicas, with proxy-aware client IP resolution that cannot be forged.
- **Retry strategy** - tenacity with exponential backoff + jitter on all OpenAI and Meta Graph API calls; only transient failures are retried.
- **Clean architecture** - routers -> services -> repositories -> models, with dependency injection and environment-based configuration.
- **Secrets without .env in production** - Docker secrets, `<NAME>_FILE` variables or Vault. See [docs/SECRETS.md](docs/SECRETS.md).
- **Backups you can prove work** - daily verified `pg_dump`, encrypted off-site copies, and an automated restore drill that rebuilds the database from the newest backup on a schedule. See [docs/BACKUP_RESTORE.md](docs/BACKUP_RESTORE.md) and [docs/OFFSITE_BACKUP.md](docs/OFFSITE_BACKUP.md).
- **Observability** - structured JSON logs (structlog), Prometheus metrics (authenticated), Grafana dashboard, alert rules routed through Alertmanager, liveness and readiness probes. See [docs/ALERTING.md](docs/ALERTING.md).
- **Deployment** - Dockerfile, compose files for development and production, automatic migrations, container health checks, CI/CD. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Project structure

```
whatsapp-ai-bot/
├── app/
│   ├── channels/        # channel adapters, registry, normalised events, channel config
│   ├── core/            # logging, security, rate limiting, retries, exceptions, metrics,
│   │                    #   secrets, chunking, events, quota, idempotency, push & inbound config
│   ├── db/              # engine, session, declarative base
│   ├── models/          # customers, conversations, messages, AI logs, pricing, documents,
│   │                    #   device tokens, operators, operator sessions, audit logs
│   ├── repositories/    # data access layer (repository pattern)
│   ├── services/        # chat, conversation, persona, prompts, retrieval, ingestion, handoff,
│   │                    #   admin, analytics, pricing, reply, auth, audit, push
│   ├── schemas/         # Pydantic request/response models
│   ├── integrations/    # WhatsApp, Messenger, OpenAI, embeddings, FCM clients
│   ├── routers/         # webhook, meta webhook, admin, auth, health, metrics, events (WebSocket)
│   ├── workers/         # Celery app + tasks (durable queue, dead-letter, scheduled sweeps)
│   ├── middleware/      # request logging, metrics
│   ├── dependencies/    # FastAPI dependency wiring
│   ├── utils/           # tiktoken token counting & history trimming
│   ├── cli.py           # operator bootstrap (`python -m app.cli create-admin`)
│   ├── main.py          # app factory
│   └── config.py        # pydantic-settings configuration
├── alembic/versions/    # 0000 baseline -> 0012 audit retention (see "Database & migrations")
├── dashboard/           # React + Vite admin SPA
├── mobile/              # Flutter operator app
├── docs/                # fifteen guides (see "Documentation")
├── knowledge/           # your PDFs (gitignored)
├── knowledge_templates/ # starting points for the knowledge base
├── monitoring/          # Prometheus config, alert rules, Grafana dashboard
├── nginx/               # TLS termination, security headers, WebSocket proxying
├── redis/               # Redis configuration
├── scripts/             # thirteen operational scripts (see "Documentation")
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

A one-shot `migrate` service runs `alembic upgrade head` before the app and
worker start, so a fresh database is ready with no manual step. **Never run
`alembic revision --autogenerate` as part of a deployment** - the schema is
owned by the checked-in migrations.

The API is at `http://localhost:8000`, the dashboard at
`http://localhost:8000/dashboard`, and Swagger at `/docs` when `DEBUG=true`.

To answer from your own documents, drop PDFs into `knowledge/` and index them:

```bash
docker compose exec app python scripts/ingest_knowledge.py
```

Then create your first operator account:

```bash
docker compose exec app python -m app.cli create-admin
```

The command prompts for a username and password and never accepts the password
as an argument, so run it from an interactive terminal. Usernames are
lowercase and match `^[a-z0-9][a-z0-9._-]*$`; passwords need at least 12
characters and 5 distinct ones, and may not contain the username.

You can skip this and sign in with `ADMIN_API_KEY` instead - see
[Operator accounts](#operator-accounts-and-the-audit-log) for what that costs
you.

## Local development

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
# in a second terminal (or set USE_TASK_QUEUE=false to skip the worker):
celery -A app.workers.celery_app.celery_app worker --loglevel=info -Q webhooks
# in a third: the scheduler, which closes idle sessions and applies retention
celery -A app.workers.celery_app.celery_app beat --loglevel=info
```

`USE_TASK_QUEUE=false` removes the need for the worker, but nothing removes the
need for the scheduler. Without `beat`, conversations are opened and greeted
and then stay open forever: no closing message is ever sent, retention never
runs, and every health check stays green while it happens. Run **exactly one**
replica of it - two schedulers means two farewells.

## Channels

Inbound events from every network are normalised by a channel adapter before
anything else sees them:

```
Incoming webhook -> channel adapter -> conversation service -> prompt builder
                 -> RAG -> OpenAI -> channel adapter -> customer
```

Only the adapter changes between channels. The AI receives a normalised
request and has no idea what transport it arrived on, which is what keeps a
second channel from becoming a second copy of the business logic.

Everything except WhatsApp is **off by default**. A channel that switched
itself on at deploy time would start answering customers on a page whose copy
nobody had reviewed.

```bash
ENABLE_WHATSAPP=true
ENABLE_MESSENGER=false
FACEBOOK_PAGE_ID=
FACEBOOK_PAGE_ACCESS_TOKEN=
FACEBOOK_VERIFY_TOKEN=          # falls back to WHATSAPP_VERIFY_TOKEN
META_APP_SECRET=                # falls back to WHATSAPP_APP_SECRET
META_API_VERSION=v21.0
```

Messenger uses `/webhook/meta` for both verification and delivery, and its
signature is verified with the same helpers the WhatsApp path uses. Instagram
DMs and the two comment channels have configuration flags reserved but no
adapters behind them yet.

## Operator accounts and the audit log

Every state-changing admin action - delete, takeover, resume, manual reply, AI
toggle, unblock, pricing create and delete - writes a row to `audit_logs`
recording who did it. A `BEFORE UPDATE OR DELETE` trigger raises on any attempt
to rewrite history, so the log is append-only at the database level rather than
by convention.

```bash
curl -sS -X POST https://$DOMAIN/admin/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username": "you", "password": "..."}'
```

The response carries a bearer token valid for 12 hours. Only a SHA-256 of it is
stored, so a database dump does not hand over live sessions, and the raw token
is returned exactly once. Passwords are hashed with `hashlib.scrypt` from the
standard library - no new dependency, no extra audit surface.

**`ADMIN_API_KEY` still works everywhere it used to.** The Flutter client sends
`X-API-Key` and has no login screen, so shared-key requests are attributed to a
reserved, non-interactive operator named `legacy-api-key`. Its password hash is
`"!"`, a value no real scrypt hash can equal, so the account can never be
logged into. This keeps `audit_logs.operator_id` `NOT NULL` without pretending
an anonymous action had an author.

The practical consequence: until you run `create-admin` and switch clients over
to tokens, the audit log will faithfully record that every single action was
performed by `legacy-api-key`, which is a true statement and a useless one.

Operators with history cannot be deleted - the foreign key is `ON DELETE
RESTRICT`, because removing a staff account should not silently unpick the
record of what they did. Deactivate them with `is_active` instead.

## Structured prompts

Each AI generation composes its instructions in layers
(`app/services/prompt_builder.py`):

```
Persona                        # app/services/persona.py, or SYSTEM_PROMPT
+ Company information          # COMPANY_INFO env var
+ Retrieved knowledge (RAG)    # fenced, labelled as reference material
+ Conversation context         # customer name, channel, current time
+ First message                # only on the opening turn: the welcome is done
+ Response rules               # language, chat style, price honesty
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

**The welcome is not part of the prompt.** It is approved copy that the code
prepends to the first reply of a conversation, decided by counting the
customer's messages in the database. A prompt cannot promise "exactly once".
See [docs/PERSONA.md](docs/PERSONA.md), which also states plainly what the bot
cannot do: it cannot see images or read attachments, and the persona is written
so that it never pretends otherwise.

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

Webhook deliveries are validated, ACKed immediately, and enqueued to the
`webhooks` Celery queue backed by Redis (AOF persistence enabled).

- `task_acks_late` + `task_reject_on_worker_lost` - a delivery is acknowledged
  only after successful processing; if a worker dies mid-task it is requeued.
- Automatic retries with exponential backoff and jitter, up to 5 attempts.
- Retries are idempotent: messages are deduplicated by provider message id.
- **Dead-letter queue**: after the last retry the raw payload is pushed onto
  the capped Redis list `DEAD_LETTER_KEY` and `webhook_dead_letters_total` is
  incremented, so a lost message is visible and replayable instead of silent.
- Each task runs its own event loop and closes everything it opened - OpenAI
  client, channel clients and database engine - so long-running workers do not
  leak connection pools.
- Once a turn is committed, the worker publishes a dashboard event to Redis so
  connected operators see it immediately.
- Deliveries older than `INBOUND_MAX_AGE_MINUTES` are dropped. Meta retries
  undelivered webhooks for up to seven days, and answering a week-old "hello"
  at three in the morning is worse than not answering it.
- For development, `USE_TASK_QUEUE=false` falls back to in-process
  `BackgroundTasks` (no worker, no durability).

Scale workers independently: `docker compose up -d --scale worker=3`.

Inspect the dead-letter list:

```bash
docker compose exec redis redis-cli lrange webhooks:dead-letter 0 -1
```

## Rate limiting

Redis-backed fixed-window limits (slowapi), shared across all replicas:

- `GET/POST /webhook` and `/webhook/meta` - `RATE_LIMIT_WEBHOOK` (default
  `6000/minute`) per client.
- `/admin/*` - `RATE_LIMIT_ADMIN` (default `60/minute`) per client.
- `POST /admin/auth/login` - `10/minute`, deliberately tighter, because it is
  the only anonymous-reachable admin route.
- Exceeding a limit returns `429`.
- The `/ws/events` upgrade is **not** limited - slowapi covers HTTP routes
  only. See the security note in [docs/DASHBOARD.md](docs/DASHBOARD.md).

The webhook limit looks enormous because it is one shared bucket: every
delivery arrives from Meta, so the endpoint cannot tell customers apart and
lowering it to a number that *looks* prudent just drops real messages at the
edge. Per-customer quotas are enforced later, in the worker, where the sender
is actually known.

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

`.env.example` is the authoritative and fully commented list. The table below
covers the settings you are most likely to change.

| Variable | Description | Default |
|---|---|---|
| `ENVIRONMENT` | `development` / `production` | `development` |
| `DEBUG` | Enables `/docs`, debug logging and dev CORS | `false` |
| `DATABASE_URL` | Async SQLAlchemy URL (`postgresql+asyncpg://...`) | local |
| `REDIS_URL` | Redis connection URL | local |
| `USE_TASK_QUEUE` | Celery (`true`) or in-process (`false`) | `true` |
| `CELERY_BROKER_URL` | Celery broker (defaults to `REDIS_URL`) | empty |
| `CELERY_RESULT_BACKEND` | Celery result backend (defaults to `REDIS_URL`) | empty |
| `CELERY_TASK_TIME_LIMIT` | Hard task timeout; must stay below the worker's `stop_grace_period` | `300` |
| `RATE_LIMIT_ENABLED` | Enable Redis-backed rate limiting | `true` |
| `RATE_LIMIT_WEBHOOK` | Limit for the webhook endpoints (one shared bucket) | `6000/minute` |
| `RATE_LIMIT_ADMIN` | Limit for `/admin/*` | `60/minute` |
| `TRUSTED_PROXY_HOPS` | Proxies that append to `X-Forwarded-For` | `1` |
| `RETRY_MAX_ATTEMPTS` | Max attempts per outbound call | `3` |
| `RETRY_BACKOFF_MAX_SECONDS` | Backoff cap between attempts | `8` |
| `DEAD_LETTER_KEY` | Redis list holding exhausted deliveries | `webhooks:dead-letter` |
| `DEAD_LETTER_MAX_ENTRIES` | Cap on that list | `1000` |
| `REJECT_STALE_INBOUND` | Drop webhook deliveries that are too old | `true` |
| `INBOUND_MAX_AGE_MINUTES` | Staleness threshold; keep below `CONVERSATION_REOPEN_WINDOW_MINUTES` | `10` |
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
| `SYSTEM_PROMPT` | Replaces the packaged persona entirely (see [docs/PERSONA.md](docs/PERSONA.md)) | empty = use the persona |
| `COMPANY_INFO` | Company facts injected into the prompt | empty |
| `COMPANY_NAME` / `SALES_PHONE` | Used by the persona and lead handoff copy | empty |
| `MAX_OUTPUT_TOKENS` | Max tokens per reply | `512` |
| `MAX_CONTEXT_MESSAGES` | Max history messages | `20` |
| `MAX_CONTEXT_TOKENS` | Token budget for history | `6000` |
| `ENABLE_CONVERSATION_SESSION` | Session lifecycle (see [docs/SESSION_LIFECYCLE.md](docs/SESSION_LIFECYCLE.md)) | `true` |
| `CONVERSATION_IDLE_TIMEOUT_MINUTES` | Idle before the scheduler closes a session | `5` |
| `CONVERSATION_REOPEN_WINDOW_MINUTES` | Grace period in which a customer rejoins the same session | `30` |
| `CUSTOMER_RATE_LIMIT_ENABLED` | Per-customer quotas, enforced in the worker | `true` |
| `SPEND_GUARD_ENABLED` | Daily spend breaker | `true` |
| `DAILY_SPEND_LIMIT_USD` | Ceiling before automated replies stop | `25.0` |
| `WHATSAPP_TOKEN` | Cloud API access token | empty |
| `WHATSAPP_PHONE_NUMBER_ID` | Sender phone number ID | empty |
| `WHATSAPP_VERIFY_TOKEN` | Webhook verification token | `change-me` |
| `WHATSAPP_APP_SECRET` | Verifies Meta signatures | empty |
| `WHATSAPP_API_VERSION` | Graph API version | `v21.0` |
| `ENABLE_MESSENGER` | Facebook Messenger channel (see [Channels](#channels)) | `false` |
| `FACEBOOK_PAGE_ID` / `FACEBOOK_PAGE_ACCESS_TOKEN` | Messenger page credentials | empty |
| `ADMIN_API_KEY` | Shared key for `/admin/*`, `/ws/events` and external `/metrics` | `change-me` |
| `AUDIT_RETENTION_DAYS` | Days of audit history to keep; `0` keeps everything | `365` |
| `PUSH_ENABLED` | Push notifications to the Flutter app | `false` |

In production the credentials listed in `REQUIRED_IN_PRODUCTION` must come from
a real secret backend; the app refuses to boot with placeholder values and
ignores `.env` entirely.

The live dashboard stream needs no configuration of its own: it uses
`REDIS_URL` and the admin credentials. Handoff and the persona have no settings
either - the wording and the phrases that trigger a handoff are code, reviewed
like code.

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness probe |
| `GET` | `/health/ready` | Readiness probe (database + Redis) |
| `GET` | `/metrics` | Prometheus metrics (in-cluster or admin key) |
| `GET` | `/webhook` | Meta webhook verification (WhatsApp) |
| `POST` | `/webhook` | Inbound WhatsApp messages & status updates |
| `GET` | `/webhook/meta` | Meta webhook verification (Messenger) |
| `POST` | `/webhook/meta` | Inbound Messenger events |
| `POST` | `/admin/auth/login` | Exchange credentials for a 12-hour bearer token (`10/minute`) |
| `POST` | `/admin/auth/logout` | Revoke the current session |
| `GET` | `/admin/auth/me` | Who am I, and did I authenticate with the shared key? |
| `GET` | `/admin/users` | List customers |
| `GET` | `/admin/conversations` | List conversations |
| `GET` | `/admin/conversations/{id}` | Conversation with messages |
| `DELETE` | `/admin/conversations/{id}` | Delete a conversation |
| `POST` | `/admin/conversations/{id}/reply` | Manual operator reply (`409` outside the 24h window) |
| `POST` | `/admin/conversations/{id}/takeover` | Take over: the bot stops answering this conversation |
| `POST` | `/admin/conversations/{id}/resume-ai` | Hand the conversation back to the bot |
| `GET` | `/admin/stats` | Usage statistics |
| `GET` | `/admin/search?q=` | Message body search |
| `GET` | `/admin/quota` | Where the day stands against the spend limit |
| `POST` | `/admin/ai-toggle` | Stop or resume all automated replies (survives restarts) |
| `POST` | `/admin/customers/{wa_id}/unblock` | Lift an abuse block |
| `POST` | `/admin/device-token` | Register a device for push notifications |
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

Admin endpoints accept either a bearer token from `/admin/auth/login` or
`X-API-Key: <ADMIN_API_KEY>`.

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
0005_conversation_tag        tag + index, for sales leads
0006_reply_idempotency       messages.reply_to_wa_message_id + unique constraint
0007_conversation_session_lifecycle
                             session open/close/reopen timestamps and counters
0008_device_tokens           device_tokens, for push notifications
0009_channel_identity        channel + external id on customers and
                             conversations, so a second network can share
                             the schema
0010_operator_accounts       operators, operator_sessions, audit_logs; seeds
                             the reserved legacy-api-key operator and installs
                             the audit immutability trigger
0011_operator_attribution    messages.operator_id and
                             conversations.assigned_operator_id (ON DELETE
                             SET NULL - deleting a staff account must not
                             destroy the transcripts they handled)
0012_audit_retention         supporting index for the retention sweep
```

`alembic upgrade head` on an empty database produces the complete schema.

Revision **ids** are not always the filename: `0008_device_tokens.py` declares
`down_revision = "0007_session_lifecycle"` while the file on disk is
`0007_conversation_session_lifecycle.py`. Read the `revision` string, not the
filename, when chaining a new migration.

## Documentation

| Guide | Covers |
|---|---|
| [ALERTING.md](docs/ALERTING.md) | Alert rules, Alertmanager routing, receivers |
| [BACKUP_RESTORE.md](docs/BACKUP_RESTORE.md) | Daily dumps, verification, restore, drills |
| [DASHBOARD.md](docs/DASHBOARD.md) | The React SPA and its security model |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | Production compose, TLS, upgrades, rollback, scaling |
| [HANDOFF.md](docs/HANDOFF.md) | Human takeover: detection, limits, API |
| [MOBILE.md](docs/MOBILE.md) | The Flutter operator app |
| [OFFSITE_BACKUP.md](docs/OFFSITE_BACKUP.md) | Encrypted off-site copies and retention |
| [PERSONA.md](docs/PERSONA.md) | The reviewed persona and what the bot cannot do |
| [PRICING.md](docs/PRICING.md) | Historical token pricing and cost attribution |
| [PRICING_POLICY.md](docs/PRICING_POLICY.md) | What the bot may say about prices |
| [PUSH_NOTIFICATIONS.md](docs/PUSH_NOTIFICATIONS.md) | FCM setup and delivery |
| [RAG.md](docs/RAG.md) | Chunking, embedding, retrieval, injection |
| [REDIS_SECURITY.md](docs/REDIS_SECURITY.md) | Redis authentication and exposure |
| [SECRETS.md](docs/SECRETS.md) | Docker secrets, `<NAME>_FILE`, Vault |
| [SESSION_LIFECYCLE.md](docs/SESSION_LIFECYCLE.md) | Open, greet, idle, close, reopen |

Operational scripts live in `scripts/`: `ingest_knowledge.py` for the knowledge
base, `init-secrets.sh` and `init-letsencrypt.sh` for first deployment,
`backup.sh` and its `backup-*.sh` companions for dumps, upload, metrics and
health, and `restore.sh`, `restore-drill.sh` and `verify-restore.sh` for
getting the data back and proving that you can.

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

For Messenger, add the **Messenger** product to the same app, point its
callback at `https://<your-domain>/webhook/meta`, subscribe to `messages` and
`messaging_postbacks`, and set `ENABLE_MESSENGER=true`.

## Extension points

CRM integration (new module in `integrations/`), voice transcription, **image
understanding and reading customer attachments** (today the model is told only
that a file arrived - see [docs/PERSONA.md](docs/PERSONA.md) for what is
missing), appointment booking (tool calling is already wired into the OpenAI
integration), semantic clustering of frequent questions (the embedding
infrastructure exists), Instagram DMs and comment channels (the configuration
flags exist, the adapters do not), a handoff SLA alert, and message templates
for replies outside the 24-hour window.
