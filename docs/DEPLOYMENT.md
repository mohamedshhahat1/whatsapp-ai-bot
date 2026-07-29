# Deployment

Production runs from `docker-compose.prod.yml`: the API, a Celery worker,
Postgres (pgvector), Redis, nginx, and a one-shot `migrate` service.

## First deployment

```bash
git clone <repo> && cd whatsapp-ai-bot
./scripts/init-secrets.sh          # creates ./secrets/* interactively
docker compose -f docker-compose.prod.yml up -d
```

There is no manual migration step. See [docs/SECRETS.md](SECRETS.md) for what
`init-secrets.sh` writes and how to use Vault instead.

Then index your documents:

```bash
docker compose -f docker-compose.prod.yml exec app python scripts/ingest_knowledge.py
```

## What starts, and in what order

```
db  (healthy: pg_isready)
   └── migrate  (alembic upgrade head, runs once, must exit 0)
          ├── app     (healthy: GET /health/ready)
          │      └── nginx
          └── worker  (healthy: celery inspect ping)
redis (healthy: redis-cli ping) ── app, worker
```

`app` and `worker` declare `depends_on: migrate: service_completed_successfully`,
so neither can start against a schema that has not been upgraded. If a
migration fails, the deployment stops there instead of starting containers
that will fail at runtime in less obvious ways.

## Upgrades

```bash
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d --remove-orphans
```

The `migrate` service runs again on every `up`, applying any new revisions
before the new app containers start. Alembic is idempotent: with nothing to
apply it exits immediately.

CI does the same over SSH when `DEPLOY_ENABLED=true` is set as a repository
variable, with `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_PATH`
and optionally `DEPLOY_PORT` as secrets. Credentials are never shipped from
CI - the server holds its own.

## Migrations

| Revision | Contents |
| --- | --- |
| `0000_initial_schema` | `users`, `conversations`, `messages`, `ai_logs` and their indexes |
| `0001_knowledge_base` | `documents`, `document_chunks`, pgvector extension, HNSW index |
| `0002_model_pricing` | `model_pricing`, seeded from the epoch so no call is unpriced |
| `0003_search_and_concurrency` | `pg_trgm` + GIN index on `messages.content`; partial unique index `uq_active_conversation_per_user` |

Rules:

- **Never** run `alembic revision --autogenerate` on a server. Generate
  revisions locally, review the SQL, commit them.
- The database image must ship pgvector (`pgvector/pgvector:pg16`). A stock
  `postgres` image fails at `0001`.
- `alembic downgrade` is implemented but destructive; take a dump first.

## Rollback

Images are tagged with the commit sha, so rolling back the application is:

```bash
export APP_IMAGE=ghcr.io/<owner>/<repo>:sha-<previous>
docker compose -f docker-compose.prod.yml up -d
```

Rolling *back* a schema is not automatic. Prefer forward fixes; if a
downgrade is unavoidable, restore a dump taken before the upgrade.

## Health and probes

| Check | Used by | Fails when |
| --- | --- | --- |
| `GET /health` | liveness | the process is wedged |
| `GET /health/ready` | container health check, load balancer | database or Redis unreachable (`503`) |
| `celery inspect ping` | worker health check | the worker stopped consuming |

Liveness intentionally has no dependencies: making it check Redis would
restart every API container during a Redis blip, turning a degradation into
an outage.

## Monitoring

Prometheus scrapes `app:8000/metrics` and `worker:9100`
(`monitoring/prometheus.yml`). The Grafana dashboard in
`monitoring/grafana/dashboards/` covers message volume, OpenAI latency,
errors, token usage, HTTP traffic and dead-lettered webhooks.

`/metrics` is not public: in-cluster scrapes are allowed by peer address,
external requests need `ADMIN_API_KEY`, and nginx blocks the path as well.

The alert worth having first is `webhook_dead_letters_total > 0`: it means a
customer message was never answered.

## When a message is lost

Deliveries that exhaust all five Celery retries are pushed onto a capped Redis
list instead of vanishing:

```bash
# inspect
docker compose -f docker-compose.prod.yml exec redis \
  redis-cli lrange webhooks:dead-letter 0 -1

# count
docker compose -f docker-compose.prod.yml exec redis \
  redis-cli llen webhooks:dead-letter
```

Each entry holds the failure time, the exception, and the original payload.
Replaying is deliberately manual - a delivery usually lands there because of a
bug or an outage, and replaying blindly re-triggers it. Processing is
idempotent (deduplicated by `wa_message_id`), so a replay cannot double-answer
a customer.

## Backups

Postgres holds everything that matters: conversations, AI logs, price history
and the vector index. Redis holds only queue state.

```bash
docker compose -f docker-compose.prod.yml exec db \
  pg_dump -U postgres whatsapp_ai_bot | gzip > backup-$(date +%F).sql.gz
```

The knowledge base can be rebuilt from `knowledge/` with
`scripts/ingest_knowledge.py`, but that costs embedding calls - the dump is
cheaper.

## Scaling

```bash
docker compose -f docker-compose.prod.yml up -d --scale worker=3
```

Workers are stateless. Rate limiting is Redis-backed and shared, so multiple
API replicas enforce one global limit. The database is the first bottleneck;
the analytics queries scan the full period on each load, and beyond a few
hundred thousand `ai_logs` rows the answer is a nightly rollup table.

## Configuration checklist

- `ENVIRONMENT=production` - the app then refuses to boot on placeholder
  secrets and ignores `.env` entirely.
- `TRUSTED_PROXY_HOPS` must equal the number of proxies that append to
  `X-Forwarded-For`. Too high and clients can forge their rate-limit bucket.
- `DEBUG=false` - keeps `/docs` closed and dev CORS off.
- Port 8000 stays bound to `127.0.0.1`; only nginx is exposed.
