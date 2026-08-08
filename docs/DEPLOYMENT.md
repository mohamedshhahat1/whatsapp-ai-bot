# Deployment

Production runs from `docker-compose.prod.yml`: the API, a Celery worker,
Postgres (pgvector), Redis, nginx with TLS, certbot, a backup scheduler, the
Prometheus/Alertmanager/Grafana stack with its exporters, and a one-shot
`migrate` service.

> **It does not run a Celery scheduler.** See
> [No scheduler in production](#no-scheduler-in-production) before you rely on
> anything time-based.

## First deployment

```bash
git clone <repo> && cd whatsapp-ai-bot
./scripts/init-secrets.sh          # creates ./secrets/* interactively

export DOMAIN=bot.example.com      # required: nginx will not start without it
export CERTBOT_EMAIL=ops@example.com

docker compose -f docker-compose.prod.yml up -d
./scripts/init-letsencrypt.sh      # once per domain, see "TLS" below
```

There is no manual migration step. See [docs/SECRETS.md](SECRETS.md) for what
`init-secrets.sh` writes and how to use Vault instead.

Then index your documents:

```bash
docker compose -f docker-compose.prod.yml exec app python scripts/ingest_knowledge.py
```

And create the first operator account:

```bash
docker compose -f docker-compose.prod.yml exec app python -m app.cli create-admin
```

Run that from an interactive terminal - it prompts for the password and will
not take it as an argument, so it cannot end up in your shell history or in a
CI log. Until at least one real operator exists, every admin action is
authenticated with the shared `ADMIN_API_KEY` and attributed in the audit log
to the reserved `legacy-api-key` account.

## What starts, and in what order

```
db  (healthy: pg_isready)
   ├── migrate  (alembic upgrade head, runs once, must exit 0)
   │      ├── app     (healthy: GET /health/ready)
   │      │      └── nginx  ── certbot (renewal loop)
   │      └── worker  (healthy: celery inspect ping)
   └── backup   (healthy: a successful dump within the last 30h)
redis (healthy: redis-cli ping) ── app, worker
```

`app` and `worker` declare `depends_on: migrate: service_completed_successfully`,
so neither can start against a schema that has not been upgraded. If a
migration fails, the deployment stops there instead of starting containers
that will fail at runtime in less obvious ways.

Monitoring starts alongside: `prometheus` (after `prometheus-targets-init`
renders the public probe target), `alertmanager` (after `alertmanager-config`
renders its config with envsubst), `grafana`, and the node, Postgres, Redis and
blackbox exporters.

<a name="no-scheduler-in-production"></a>
### No scheduler in production

**`docker-compose.prod.yml` does not define a `beat` service.** The development
compose file does; production does not. Everything on the periodic schedule
therefore does not run:

| What does not happen | Consequence |
| --- | --- |
| Idle sessions are never closed | `CONVERSATION_IDLE_TIMEOUT_MINUTES` and `CONVERSATION_CLOSE_AFTER_IDLE` are read and ignored; conversations stay open indefinitely |
| No closing message is ever sent | `ENABLE_CONVERSATION_CLOSING_MESSAGE` has no effect in production |
| Expired `operator_sessions` are never removed | Rows accumulate; expiry is still enforced at check time, so this is hygiene rather than a security hole |
| `AUDIT_RETENTION_DAYS` never purges | The append-only audit log grows without bound |

Nothing surfaces this. Every container is healthy, no request fails, no alert
fires, and the dashboard looks correct - conversations simply never reach the
`closed` state. It is the most expensive kind of bug: the system reports
success while doing less than it claims.

Until a `beat` service is added to the production stack, run the scheduler
yourself:

```bash
docker compose -f docker-compose.prod.yml run -d --name beat \
  --entrypoint celery app \
  -A app.workers.celery_app.celery_app beat --loglevel=info \
  --schedule=/tmp/celerybeat-schedule
```

Run **exactly one**. Two schedulers means every idle conversation receives two
farewell messages.

---

## TLS

The application is never served over plain HTTP. Port 80 serves exactly one
thing -- the ACME challenge path -- and 301s everything else to HTTPS.

`DOMAIN` is mandatory and has no default. Compose declares it as
`${DOMAIN:?...}`, so a deployment that forgets it fails immediately with a
clear message rather than starting nginx with an empty `server_name` and
serving the default certificate to Meta.

### Bootstrap

```bash
DOMAIN=bot.example.com CERTBOT_EMAIL=ops@example.com ./scripts/init-letsencrypt.sh
```

There is a chicken-and-egg problem the script exists to solve: nginx will not
start without a certificate file to open, and certbot cannot obtain one
without an nginx already answering the challenge on port 80. It plants a
throwaway self-signed certificate, starts nginx, swaps it for a real one, and
reloads.

**Point DNS at the host before running it.** The most common failure by a wide
margin is running this while the A record still points somewhere else. Let's
Encrypt rate-limits failed issuance at five attempts per hostname per hour, so
a few impatient retries lock you out for an hour with no way to shorten it.
Use `STAGING=1` while fighting DNS or firewall rules -- the certificate is
untrusted but the rate limits are far looser. Re-run without it once the
challenge succeeds.

The script refuses to overwrite a working certificate unless you pass
`FORCE=1`.

### Renewal

Automatic and needs no cron on the host. The `certbot` service wakes every 12
hours and renews anything within 30 days of expiry; nginx reloads every 6
hours to pick up a new certificate. Both loops are inside containers, so they
survive a host reboot along with everything else.

Renewal failing is silent by nature -- nothing errors until the certificate
actually expires 30 days later, at which point Meta stops delivering webhooks.
The `CERTBOT_EMAIL` expiry warning is the backstop; use a mailbox someone
reads.

```bash
# check expiry
docker compose -f docker-compose.prod.yml run --rm --entrypoint certbot certbot certificates

# dry-run a renewal without touching rate limits
docker compose -f docker-compose.prod.yml run --rm --entrypoint certbot certbot renew --dry-run
```

### What is enforced

| | |
| --- | --- |
| Protocols | TLS 1.2 and 1.3 only. 1.0, 1.1, SSLv2 and SSLv3 are off |
| Ciphers | AEAD suites only (ECDHE/DHE + AES-GCM or ChaCha20-Poly1305) |
| Forward secrecy | Every offered suite is ephemeral-DH |
| HSTS | `max-age=63072000; includeSubDomains; preload` |
| Session tickets | Off -- they undermine forward secrecy when keys are not rotated |
| OCSP | Stapled |
| `server_tokens` | Off |

`ssl_prefer_server_ciphers` is deliberately **off**. With a modern AEAD-only
list, the client is better placed than we are to know whether it has AES
hardware acceleration.

### Security headers

All six required headers are in `nginx/security-headers.conf`, included both at
server level and inside every `location` block. Nginx's `add_header` does not
inherit into a block that declares its own, so a single location adding one
header would silently drop all the others -- hence the repetition.

Every header is marked `always`, which is what makes it apply to 4xx and 5xx
responses too. Without it, error pages -- the ones where clickjacking and MIME
sniffing protections matter most -- go out bare.

### Meta and WebSockets

- Meta requires an `https://` callback and validates the full chain. A staging
  certificate is rejected. Point the WhatsApp webhook at
  `https://<DOMAIN>/webhook` and, if Messenger is enabled, the Messenger
  webhook at `https://<DOMAIN>/webhook/meta`.
- Meta does not send `X-Forwarded-For`, so `TRUSTED_PROXY_HOPS=1` (the bundled
  nginx) is correct and unaffected by TLS.
- The dashboard stream is proxied at `/ws/` with `Upgrade`/`Connection`
  headers and a 3600s read timeout. Over TLS the browser negotiates `wss://`
  automatically. The 60s default would drop an idle stream roughly every
  minute and the UI would flap.
- The CSP allows `connect-src 'self' wss:` for exactly this reason.

### Verifying

```bash
curl -sSI https://$DOMAIN/health | head -n 1
curl -sSI http://$DOMAIN/health | head -n 1          # expect 301
curl -sSI https://$DOMAIN/ | grep -i strict-transport
openssl s_client -connect $DOMAIN:443 -tls1_1 </dev/null   # expect failure
```

<a name="webhook-not-receiving"></a>
### When webhooks stop arriving

Nothing errors in this failure mode -- deliveries simply stop, which is why
the `NoInboundMessages` alert exists. Check in this order:

1. Certificate expiry (`certbot certificates`). By far the most likely cause.
2. `docker compose -f docker-compose.prod.yml logs nginx --tail=50`
3. Meta App Dashboard → WhatsApp → Configuration: is the callback still
   subscribed, and does the URL match the current domain?
4. `curl -sSI https://$DOMAIN/webhook` from outside the host.

One more, easy to miss: deliveries older than `INBOUND_MAX_AGE_MINUTES` are
dropped on purpose. After an outage, Meta replays everything it could not
deliver -- for hours -- and those replays are discarded rather than answered.
Messages arriving but not being answered right after a recovery is the
expected behaviour, not a bug.

---

## Backups

Fully covered in [docs/BACKUP_RESTORE.md](BACKUP_RESTORE.md) and
[docs/OFFSITE_BACKUP.md](OFFSITE_BACKUP.md). In short:

- The `backup` service takes a compressed `pg_dump -Fc` every day at 02:00 UTC,
  promotes Sunday's to weekly and the 1st of the month's to monthly by hard
  link, and prunes by count (14 daily, 8 weekly, 12 monthly).
- Every dump is verified at write time -- a file only gets its real name after
  `pg_restore --list` parses it and the expected tables are present. A backup
  that is only checked at restore time is not a backup.
- Encrypted off-site copies are uploaded to S3, B2, GCS or Azure, with their
  own retention schedule and their own staleness check. Set
  `BACKUP_REMOTE_PROVIDER` -- it ships as `none`, and `backup.sh` logs a
  warning on every run rather than failing, so a fresh deployment keeps every
  copy on the same host as the database it protects until you configure this.
- The container's healthcheck measures **output, not liveness**: it goes
  unhealthy when no successful backup has completed in 30 hours. A backup
  process running happily while producing nothing is the failure that matters.
- `RESTORE_DRILL_ENABLED` runs a scheduled restore drill that rebuilds the
  database from the newest backup into a throwaway instance and checks the
  expected tables came back. An untested restore path is a hypothesis, not a
  recovery plan. This one is driven by the backup scheduler, not by Celery, so
  it is unaffected by the missing `beat` service.
- Restore anything with one command: `./scripts/restore.sh latest`, or
  `./scripts/restore.sh --list` to see what is available.

RPO is 24 hours, RTO around 5 minutes.

---

## Upgrades

```bash
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d --remove-orphans
```

The `migrate` service runs again on every `up`, applying any new revisions
before the new app containers start. Alembic is idempotent: with nothing to
apply it exits immediately.

The worker has `stop_grace_period: 330s`, so a deploy waits up to five and a
half minutes for in-flight messages to finish before Docker kills it. This
must stay above `CELERY_TASK_TIME_LIMIT` (300s). If it drops below, deploys
SIGKILL running tasks; because tasks are acknowledged late, those are then
redelivered -- and a redelivery that lands after a WhatsApp send is a duplicate
reply and a second OpenAI charge. `tests/test_reliability_config.py` fails if
the two ever cross.

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
| `0004_conversation_handoff` | `mode`, `assigned_operator` for human takeover |
| `0005_conversation_tag` | `tag` + `ix_conversations_tag`, for sales leads |
| `0006_reply_idempotency` | `messages.reply_to_wa_message_id` + unique constraint |
| `0007_conversation_session_lifecycle` | Session open/close/reopen timestamps and counters |
| `0008_device_tokens` | `device_tokens`, for push notifications to the mobile app |
| `0009_channel_identity` | `channel` and external id on customers and conversations, so a second network shares the schema instead of forking it |
| `0010_operator_accounts` | `operators`, `operator_sessions`, `audit_logs`; seeds the reserved `legacy-api-key` operator and installs the audit immutability trigger |
| `0011_operator_attribution` | `messages.operator_id`, `conversations.assigned_operator_id` -- nullable, `ON DELETE SET NULL` |
| `0012_audit_retention` | Supporting index for the retention sweep |

Rules:

- **Never** run `alembic revision --autogenerate` on a server. Generate
  revisions locally, review the SQL, commit them.
- The database image must ship pgvector (`pgvector/pgvector:pg16`). A stock
  `postgres` image fails at `0001`.
- `alembic downgrade` is implemented but destructive; take a dump first.
- Revision ids are not always the filename. `0008_device_tokens.py` declares
  `down_revision = "0007_session_lifecycle"` while the file is
  `0007_conversation_session_lifecycle.py`. Chain from the `revision` string.

## Audit log and operator accounts

Every state-changing admin action writes a row to `audit_logs` after the action
succeeds. A `BEFORE UPDATE OR DELETE` trigger raises on any attempt to rewrite
it, so the log is append-only at the database level rather than by convention.

Three consequences worth knowing before an incident rather than during one:

- `audit_logs.operator_id` is `ON DELETE RESTRICT`. An operator with history
  cannot be deleted; deactivate them with `is_active` instead. Deleting the
  record of who did something is not a routine administrative act.
- `TRUNCATE` bypasses row-level triggers. That is deliberate -- it is the
  escape hatch for retention and for resetting a test database -- but it means
  the immutability guarantee is about ordinary `UPDATE` and `DELETE`, not about
  a determined superuser.
- `AUDIT_RETENTION_DAYS` (default `365`) bounds the growth, but the purge runs
  on the Celery schedule, which the production stack does not currently start.
  See [No scheduler in production](#no-scheduler-in-production). Set `0` to
  disable the purge explicitly rather than by accident.

## Rollback

Images are tagged with the commit sha, so rolling back the application is:

```bash
export APP_IMAGE=ghcr.io/<owner>/<repo>:sha-<previous>
docker compose -f docker-compose.prod.yml up -d
```

Rolling *back* a schema is not automatic. Prefer forward fixes; if a
downgrade is unavoidable, restore a backup taken before the upgrade with
`./scripts/restore.sh`.

## Health and probes

| Check | Used by | Fails when |
| --- | --- | --- |
| `GET /health` | liveness | the process is wedged |
| `GET /health/ready` | container health check, load balancer | database or Redis unreachable (`503`) |
| `celery inspect ping` | worker health check | the worker stopped consuming |
| `scripts/backup-healthcheck.sh` | backup container | no successful backup in 30h |

Liveness intentionally has no dependencies: making it check Redis would
restart every API container during a Redis blip, turning a degradation into
an outage.

Nothing here checks the **scheduler**, because production does not run one. If
you start one by hand, note that it gets no healthcheck and no alert either: a
dead scheduler produces no errors, no failed requests and no unhealthy
service. `AIDisabled` and `NoInboundMessages` are the closest existing
coverage, and neither is really about this.

## Monitoring

Prometheus scrapes `app:8000/metrics` and the node, Postgres, Redis and
blackbox exporters (`monitoring/prometheus.yml`). The blackbox exporter probes
the **public** hostname from outside the app containers - every other target is
scraped inside the compose network and would keep passing while DNS, nginx or
the certificate were broken, which is exactly the combination that stops Meta
delivering webhooks. The Grafana dashboard in
`monitoring/grafana/dashboards/` covers message volume, OpenAI latency,
errors, token usage, HTTP traffic and dead-lettered webhooks.

`/metrics` is not public: in-cluster scrapes are allowed by peer address,
external requests need `ADMIN_API_KEY`, and nginx returns 404 for the path.

Grafana and Prometheus are bound to `127.0.0.1`, as is Alertmanager - its
silence UI has no authentication of its own, and an exposed Alertmanager lets
anyone silence every alert you have.

### Alerting

Rules live in `monitoring/alerts.yml` and are routed through Alertmanager to
the receivers configured in [docs/ALERTING.md](ALERTING.md) -- Telegram, Slack
or email. Configure at least one receiver: most of these alerts exist precisely
for failures that are otherwise invisible, and an unrouted alert is a log line
nobody reads.

Alerting credentials are Docker secrets like everything else:
`alert_smtp_password`, `alert_slack_webhook_url` and
`alert_telegram_bot_token`, mounted on the `alertmanager` service and read by
Alertmanager through the `*_file` form of each field. `init-secrets.sh` creates
all three as empty placeholders; fill in the ones you use. Only the non-secret
half -- SMTP host and port, sender and recipient, channel names, the Telegram
chat id -- is substituted into the rendered config from the environment.

The three alerts that matter most:

| Alert | Why it exists |
| --- | --- |
| `AIDisabled` | The assistant answering nobody looks like perfect health on every other dashboard: no errors, no latency, no failed requests |
| `NoInboundMessages` | The closest thing to a synthetic check for the whole webhook path -- expired certificate, broken subscription, wrong `server_name` all fail this way |
| `ReplyReservationsUnconfirmed` | Workers dying between the WhatsApp send and the confirming commit; each one may be an unanswered customer |

`SpendApproachingLimit` hard-codes 20 USD to match the default
`DAILY_SPEND_LIMIT_USD` of 25. Prometheus cannot read the application's
configuration, so if you change that setting you must change the rule too.

## Cost and abuse protection

Per-customer quotas and the daily spend breaker are enforced in the worker,
keyed by the customer's channel identity -- the earliest point at which the
sender is actually known. The endpoint limit on the webhook routes is a
separate, much cruder thing: every delivery arrives from Meta, so it is one
shared bucket and cannot distinguish customers. It is set to 6000/minute for
that reason. Lowering it to something that looks prudent means dropping real
customers' messages at the edge.

```bash
# where the day stands
curl -sS -H "X-API-Key: $ADMIN_API_KEY" https://$DOMAIN/admin/quota

# stop all automated replies immediately (survives restarts)
curl -sS -X POST -H "X-API-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"disabled": true}' https://$DOMAIN/admin/ai-toggle

# lift an abuse block on a customer who did not deserve it
curl -sS -X POST -H "X-API-Key: $ADMIN_API_KEY" \
  https://$DOMAIN/admin/customers/201001234567/unblock
```

Every quota check **fails open**. If Redis is unreachable, customers are served
and the failure is logged. A protection that stops the business answering
customers when its cache blips is worse than the abuse it prevents -- but it
does mean a Redis outage removes the spend ceiling, so treat Redis alerts as
cost alerts too.

When the ceiling trips, customers are not ignored: they get a message that says
someone will follow up, and every message is still stored and shown to
operators. We decline to answer automatically; we never decline to listen.

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
idempotent (the inbound insert is an atomic claim on the provider message id,
and the reply is reserved before it is sent), so a replay cannot double-answer
a customer.

One deliberate tradeoff to know about: if a worker dies **between** the
WhatsApp send and the commit that confirms it, the retry finds an existing
reservation and declines to resend. One possibly-unanswered customer is
preferred over a duplicate reply and a double charge. The status callback from
Meta usually resolves the row on its own; the `ReplyReservationsUnconfirmed`
alert tells you when it is happening at all.

## Scaling

```bash
docker compose -f docker-compose.prod.yml up -d --scale worker=3
```

Workers are stateless. Rate limiting, quotas and the spend breaker are all
Redis-backed and shared, so multiple replicas enforce one global limit. The
database is the first bottleneck; the analytics queries scan the full period on
each load, and beyond a few hundred thousand `ai_logs` rows the answer is a
nightly rollup table.

The scheduler, if you run one, is the exception: **exactly one**, always. It is
not a bottleneck and it does not shard. Two schedulers means every idle
conversation gets two farewell messages.

## Configuration checklist

- `DOMAIN` - required. Compose refuses to start without it.
- `ENVIRONMENT=production` - the app then refuses to boot on placeholder
  secrets and ignores `.env` entirely.
- `TRUSTED_PROXY_HOPS` must equal the number of proxies that append to
  `X-Forwarded-For`. Too high and clients can forge their rate-limit bucket.
- `DEBUG=false` - keeps `/docs` closed and dev CORS off.
- `DAILY_SPEND_LIMIT_USD` - set it to an amount you would be content to lose to
  a scripted flood before anyone notices.
- `SALES_PHONE` and `COMPANY_NAME` - the bot offers to connect customers to
  sales; leave the number empty and it prints nothing where the number goes.
- `AUDIT_RETENTION_DAYS` - how long the audit log is kept. `0` keeps
  everything, which is the behaviour every deployment had before the log
  existed.
- `BACKUP_REMOTE_PROVIDER` - ships as `none`. Until it is set, every backup
  lives on the same host as the database it protects.
- Configure at least one Alertmanager receiver. Unrouted alerts are log lines.
- **Decide what to do about the scheduler.** Session closing, farewells and
  audit retention all depend on it, the production stack does not start one,
  and nothing alerts when it is missing. See
  [No scheduler in production](#no-scheduler-in-production).
- Create at least one real operator with `python -m app.cli create-admin`.
  Until you do, the audit log records every action as `legacy-api-key`, which
  is accurate and tells you nothing.
- Port 8000 stays bound to `127.0.0.1`; only nginx is exposed.
- Redis requires a password, rendered into a tmpfs file from the
  `redis_password` secret so it never appears in `ps` or `docker inspect`, and
  is not published outside the compose network. The metrics exporter uses a
  separate read-only ACL account. See
  [docs/REDIS_SECURITY.md](REDIS_SECURITY.md).
