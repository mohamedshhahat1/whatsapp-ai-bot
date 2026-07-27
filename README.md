# WhatsApp AI Bot

Production-ready AI-powered WhatsApp Business chatbot built with **Python 3.12**, **FastAPI**, **PostgreSQL**, **Redis**, and the **OpenAI Responses API**.

## Features

- **WhatsApp Cloud API integration** — webhook verification, inbound text/image/document messages, outbound replies, delivery status & read receipts, Meta signature verification (`X-Hub-Signature-256`).
- **OpenAI Responses API** — conversation memory, configurable model & system prompt, tool-calling-ready client, full AI usage logging.
- **Clean Architecture** — routers → services → repositories → models, with dependency injection and environment-based configuration.
- **Conversation management** — every message persisted, history reloaded per user, context window trimming and token budgeting.
- **Admin REST API** — users, conversations, statistics, protected by API key.
- **Structured JSON logging** via structlog, request logging middleware, centralized exception handling.
- **Deployment-ready** — Dockerfile, docker-compose (app + Postgres + Redis + optional Nginx), Alembic migrations.

## Project structure

```
whatsapp-ai-bot/
├── app/
│   ├── core/            # logging, security, exceptions
│   ├── db/              # engine, session, declarative base
│   ├── models/          # SQLAlchemy 2.0 models (User, Conversation, Message, AILog, ChatSession)
│   ├── repositories/    # data access layer (repository pattern)
│   ├── services/        # business logic (chat, conversation, admin)
│   ├── schemas/         # Pydantic response/request models
│   ├── integrations/    # whatsapp.py (Cloud API), openai.py (Responses API)
│   ├── routers/         # HTTP API layer (webhook, admin, health)
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
docker compose up --build -d
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
```

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
| `POST` | `/webhook` | Inbound messages & status updates |
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
