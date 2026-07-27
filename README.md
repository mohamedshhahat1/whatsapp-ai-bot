# WhatsApp AI Bot

Production-ready AI-powered WhatsApp Business chatbot built with **Python 3.12**, **FastAPI**, **PostgreSQL**, **Redis**, **Celery**, and the **OpenAI Responses API**.

## Features

- **WhatsApp Cloud API integration** — webhook verification, inbound text/image/document messages, outbound replies, delivery status & read receipts, Meta signature verification (`X-Hub-Signature-256`).
- **Durable background processing** — webhook deliveries are enqueued to **Celery + Redis** with late acks and exponential-backoff retries, so messages are not lost if a worker crashes mid-processing. Message deduplication makes retries safe.
- **Rate limiting** — Redis-backed limits (slowapi) on the webhook and Admin API, shared across all replicas, proxy-aware client IP detection.
- **Retry strategy** — tenacity with exponential backoff + jitter on all OpenAI and Meta Graph API calls; only transient failures (network errors, timeouts, 429, 5xx) are retried.
- **OpenAI Responses API** — conversation memory, configurable model & system prompt, tool-calling-ready client, full AI usage logging.
- **Clean Architecture** — routers → services → repositories → models, with dependency injection and environment-based configuration.
- **Conversation management** — every message persisted, history reloaded per user, context window trimming and token budgeting.
- **Admin REST API** — users, conversations, statistics, protected by API key.
- **Structured JSON logging** via structlog, request logging middleware, centralized exception handling.
- **Deployment-ready** — Dockerfile, docker-compose (app + worker + Postgres + Redis + optional Nginx), Alembic migrations.

## Project structure

```
whatsapp-ai-bot/
├── app/
│   ├── core/            # logging, security, rate limiting, retries, exceptions
│   ├── db/              # engine, session, declarative base
│   ├── models/          # SQLAlchemy 2.0 models (User, Conversation, Message, AILog, ChatSession)
│   ├── repositories/    # data access layer (repository pattern)
│   ├── services/        # business logic (chat, conversation, webhook processing, admin)
│   ├── schemas/         # Pydantic response/request models
│   ├── integrations/    # whatsapp.py (Cloud API), openai.py (Responses API)
│   ├── routers/         # HTTP API layer (webhook, admin, health)
│   ├── workers/         # Celery app + tasks (durable queue)
│   ├── middleware/      # request logging
│   ├── dependencies/    # FastAPI dependency wiring
│   ├── utils/           # token estimation & history trimming
│   ├── main.py          # app factory
│   └── config.py        # pydantic-settings configuration
├── alembic/             # migrations
├── tests/
├── docker-compose.yml
├── Dockerfile
├── nginx/nginx.conf
├── requirements.txt
└── .env.example
```

## Quick start (Docker)

```bash
cp .env.example .env      # fill in your keys
docker compose up --build -d   # starts app + celery worker + postgres + redis
docker compose exec app alembic revision --autogenerate -m "initial schema"
docker compose exec app alembic upgrade head
```

The API is now at `http://localhost:8000` (`/docs` for Swagger UI when `DEBUG=true`).

## Local development

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
# in a second terminal (or set USE_TASK_QUEUE=false to skip the worker):
celery -A app.workers.celery_app.celery_app worker --loglevel=info -Q webhooks
```

## Background processing

Webhook `POST /webhook` deliveries are validated, ACKed immediately, and enqueued to the `webhooks` Celery queue backed by Redis (AOF persistence enabled in compose).

- `task_acks_late` + `task_reject_on_worker_lost` — a delivery is only acknowledged after successful processing; if a worker dies mid-task the message is re-queued.
- Automatic retries with exponential backoff and jitter (max 5 attempts).
- Retries are idempotent: messages are deduplicated by `wa_message_id`.
- For development you can set `USE_TASK_QUEUE=false` to fall back to in-process FastAPI `BackgroundTasks` (no worker needed, no durability guarantees).

Scale workers independently of the API: `docker compose up -d --scale worker=3`.

## Retry strategy

All outbound calls to OpenAI and the Meta Graph API are wrapped with **tenacity**:

- Exponential backoff with jitter (initial 0.5s, capped at `RETRY_BACKOFF_MAX_SECONDS`), up to `RETRY_MAX_ATTEMPTS` attempts.
- Only transient failures are retried: network/timeout errors, `429 Too Many Requests`, and `5xx`. Permanent errors (400/401/403) fail fast.
- Every retry is logged with attempt number and wait time (structured logs).
- The OpenAI SDK's built-in retries are disabled (`max_retries=0`) so tenacity is the single source of truth and retry counts don't multiply.
- Layered with Celery: tenacity handles short blips inside a task; if all attempts are exhausted, the Celery task itself retries later with longer backoff.

## Rate limiting

Redis-backed fixed-window limits (slowapi), shared across all app replicas:

- `POST/GET /webhook` — `RATE_LIMIT_WEBHOOK` (default `600/minute`) per client IP.
- `/admin/*` — `RATE_LIMIT_ADMIN` (default `60/minute`) per client IP.
- Client IP honors `X-Forwarded-For` set by the Nginx reverse proxy.
- Exceeding a limit returns `429 Too Many Requests`.
- Disable entirely with `RATE_LIMIT_ENABLED=false` (tests/local development).

## Connecting WhatsApp (Meta)

1. Create a Meta app with the **WhatsApp** product and grab the token, phone number ID, and app secret.
2. Expose your server publicly (in dev: `ngrok http 8000`).
3. In *WhatsApp → Configuration*, set the callback URL to `https://<your-domain>/webhook` and the verify token to `WHATSAPP_VERIFY_TOKEN`.
4. Subscribe to the `messages` webhook field.

## Environment variables

| Variable | Description | Default |
|---|---|---|
| `ENVIRONMENT` | `development` / `production` | `development` |
| `DEBUG` | Enables `/docs` and debug logging | `false` |
| `DATABASE_URL` | Async SQLAlchemy URL (`postgresql+asyncpg://…`) | local |
| `REDIS_URL` | Redis connection URL | local |
| `USE_TASK_QUEUE` | Process webhooks via Celery (`true`) or in-process (`false`) | `true` |
| `CELERY_BROKER_URL` | Celery broker (defaults to `REDIS_URL`) | — |
| `CELERY_RESULT_BACKEND` | Celery result backend (defaults to `REDIS_URL`) | — |
| `RATE_LIMIT_ENABLED` | Enable Redis-backed rate limiting | `true` |
| `RATE_LIMIT_WEBHOOK` | Limit for `/webhook` endpoints | `600/minute` |
| `RATE_LIMIT_ADMIN` | Limit for `/admin/*` endpoints | `60/minute` |
| `RETRY_MAX_ATTEMPTS` | Max attempts per outbound OpenAI/Meta call | `3` |
| `RETRY_BACKOFF_MAX_SECONDS` | Backoff cap between attempts | `8` |
| `OPENAI_API_KEY` | OpenAI API key | — |
| `OPENAI_MODEL` | Model used by the Responses API | `gpt-4.1-mini` |
| `SYSTEM_PROMPT` | Assistant persona/instructions | generic |
| `MAX_OUTPUT_TOKENS` | Max tokens per AI reply | `512` |
| `MAX_CONTEXT_MESSAGES` | Max history messages sent to the model | `20` |
| `MAX_CONTEXT_TOKENS` | Approx. token budget for history | `6000` |
| `WHATSAPP_TOKEN` | Cloud API access token | — |
| `WHATSAPP_PHONE_NUMBER_ID` | Sender phone number ID | — |
| `WHATSAPP_VERIFY_TOKEN` | Webhook verification token | — |
| `WHATSAPP_APP_SECRET` | Used to verify Meta signatures | — |
| `WHATSAPP_API_VERSION` | Graph API version | `v21.0` |
| `ADMIN_API_KEY` | Key for `/admin/*` endpoints (`X-API-Key` header) | — |

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness probe |
| `GET` | `/webhook` | Meta webhook verification |
| `POST` | `/webhook` | Inbound messages & status updates (enqueued) |
| `GET` | `/admin/users` | List users |
| `GET` | `/admin/conversations` | List conversations |
| `GET` | `/admin/conversations/{id}` | Conversation with messages |
| `DELETE` | `/admin/conversations/{id}` | Delete a conversation |
| `GET` | `/admin/stats` | Usage statistics |

Admin endpoints require the `X-API-Key: <ADMIN_API_KEY>` header.

## Tests

```bash
pytest
```

## Roadmap / extension points

The architecture is designed so you can add: RAG (knowledge base retrieval in `services/`), CRM integration (new module in `integrations/`), voice messages & image understanding (extend `chat_service`), appointment booking (tool calling in `integrations/openai.py`), and analytics dashboards on top of `ai_logs`.
