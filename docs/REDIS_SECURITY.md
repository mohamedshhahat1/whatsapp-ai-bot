# Redis security

Redis in this stack is not a cache. It holds:

- the **Celery queue** of unprocessed customer messages
- the **reply-idempotency keys** that stop a retried delivery being sent twice
- the **per-customer rate limits**
- the **daily spend and token counters** enforcing the cost ceiling
- the **dead-letter list**

Anyone who can reach an unauthenticated Redis can clear the spend counters and
remove the ceiling on your OpenAI bill, delete the idempotency keys and cause
duplicate replies to real customers, or drop the queue and lose messages
outright. None of that raises an application error.

---

## What is enforced

| Control | Where |
|---|---|
| `requirepass` | generated into `/run/redis/auth.conf` at start-up |
| ACL users (`default`, `exporter`) | same file, Redis 6+ |
| `protected-mode yes` | `redis/redis.conf` |
| `FLUSHALL`, `FLUSHDB`, `DEBUG`, `SHUTDOWN`, `REPLICAOF`, `SLAVEOF`, `MODULE` disabled | `redis/redis.conf` |
| `maxmemory-policy noeviction` | `redis/redis.conf` |
| Password never on the command line or in env | compose `command:` reads the secret |
| Every client authenticates | `app/config.py` validator |
| TLS when external | `REDIS_TLS=true` |
| Boot refused without auth in production | `app/config.py` validator |

Redis is **not** published to the host. It has no `ports:` entry and is
reachable only from the compose network.

---

## How the password reaches every client

One validator in `app/config.py`, not one change per call site:

```
secrets/redis_password  ->  /run/secrets/redis_password
                        ->  Settings.redis_password   (pydantic secrets_dir)
                        ->  apply_redis_credentials()
                        ->  Settings.redis_url
                        ->  broker_url / result_backend / quota / ratelimit
                            / events / idempotency
```

Every Redis consumer already derived its connection from `redis_url`, so this
authenticated all of them at once — and a consumer added later cannot forget
to authenticate, because it has nowhere else to get a URL from.

Credentials already present in `REDIS_URL` always win. A managed provider
hands you a single URL with the password inside it, and silently overwriting
that would be worse than either behaviour alone.

### Password encoding

The password is percent-encoded before being placed in the URL.
`openssl rand -base64` emits `/` and `+`; an unencoded `/` terminates the
authority section early, so the client connects to database 0 with a truncated
password instead of failing. Covered by
`tests/test_redis_security.py::test_password_is_percent_encoded`.

---

## Two accounts, not one

| User | Access |
|---|---|
| `default` | full access — the application, worker and Celery |
| `exporter` | `-@all` plus `INFO`, `PING`, `CLIENT`, `CONFIG GET`, `CLUSTER INFO`, `SLOWLOG GET`, `LATENCY LATEST`, `MEMORY STATS` |

The metrics exporter is a third-party container with network access to Redis.
Giving it the application password would mean a compromise there could delete
the quota, spend and idempotency keys. It gets a read-only account and its own
password instead.

---

## Where the password actually lives

It is generated into a **tmpfs** at container start and never touches disk:

```yaml
command: ["/bin/sh", "-c", "... > /run/redis/auth.conf; exec redis-server ..."]
tmpfs:
  - /run/redis:mode=700
```

The alternatives all leak it:

| Approach | Leaks to |
|---|---|
| `--requirepass "$PW"` | `ps` inside the container, `docker inspect` outside |
| `REDIS_PASSWORD` env var | `docker inspect`, `/proc/1/environ` |
| password written into `redis.conf` | git, the host filesystem |
| generated onto a bind mount | the host filesystem, and it survives the container |

`redis.conf` ends with `include /run/redis/auth.conf`. If that file is missing
Redis refuses to start — deliberately, because the alternative is a Redis that
starts up unauthenticated.

---

## Two deliberate exceptions

**`CONFIG` is left enabled.** `redis_exporter` issues `CONFIG GET`. Disabled,
the exporter still starts and still reports `redis_up`, so nothing looks
broken — it just quietly stops publishing part of the metric set the alerts
depend on. Authentication already keeps unauthorised clients out, and the
exporter's ACL user is limited to `CONFIG GET` specifically. If you drop the
exporter, disable it.

**`KEYS` is left enabled.** With authentication in place it is a performance
foot-gun rather than a security hole, and disabling it breaks ad-hoc debugging
for no attacker-facing gain.

---

## TLS

Off by default and correctly so: in-stack traffic never leaves the Docker
bridge, and TLS there is pure CPU overhead on every queue operation.

Turn it on when Redis is genuinely remote — a managed instance, or a separate
host:

```bash
REDIS_TLS=true
```

This switches `redis://` to `rediss://` everywhere, including the Celery broker
and result backend. The managed provider terminates TLS; the bundled
`redis:7-alpine` service is not configured as a TLS server, because a Redis
reached only over a private bridge does not need to be.

---

## Operating

**Connect manually:**

```bash
docker compose -f docker-compose.prod.yml exec redis sh -c \
  'REDISCLI_AUTH="$(cat /run/secrets/redis_password)" redis-cli'
```

Use `REDISCLI_AUTH`, not `-a`. `-a` prints a warning **containing the
password** to stderr on every invocation, which is how it ends up in your
container logs and your shell history.

**Rotate the password:**

```bash
openssl rand -base64 36 | tr -d '\n/+=' | cut -c1-40 > secrets/redis_password
chmod 600 secrets/redis_password
docker compose -f docker-compose.prod.yml up -d redis app worker
```

Restart Redis **first**, then the clients. In between, clients hold stale
credentials and retry; the queue is durable (AOF) so nothing is lost, but
replies pause for a few seconds. There is no zero-downtime rotation with
`requirepass` — ACL users can be updated live if you need one.

**Upgrading an existing deployment:**

```bash
./scripts/init-secrets.sh     # safe to re-run; adds only the new secrets
docker compose -f docker-compose.prod.yml up -d
```

The app refuses to boot in production without a Redis password. If you must
defer that deliberately, set `REDIS_AUTH_REQUIRED=false` — but understand that
you are running the spend ceiling and the duplicate-reply protection on an
open datastore.

Local development is unaffected: `docker-compose.yml` runs Redis without
authentication and the requirement only binds when `ENVIRONMENT=production`.

---

## Monitoring

`redis_exporter` authenticates as `exporter` and publishes `redis_up`, which
drives the `RedisDown` critical alert in `monitoring/alerts.yml`.

> `RedisDown` matters more than it looks. The quota layer **fails open** — when
> Redis is unreachable, messages are processed rather than rejected, so
> customers keep getting answers while the spend ceiling is not being enforced
> at all. The stack looks healthy from the outside for as long as it lasts.
