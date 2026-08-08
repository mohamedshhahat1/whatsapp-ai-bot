# Alerting

Prometheus decides *that* something is wrong. Alertmanager decides *who finds
out and how*. Rules without Alertmanager are a dashboard nobody is watching at
3am.

```
exporters + app  -->  Prometheus  -->  Alertmanager  -->  Slack
                      (evaluates)      (routes,           Telegram
                                        groups,           Email
                                        silences,
                                        inhibits)
```

---

## Setup

Alerting has two halves, kept deliberately apart:

| | Where it lives | Examples |
|---|---|---|
| **Credentials** | Docker secrets, read by Alertmanager itself | SMTP password, Slack webhook URL, Telegram bot token |
| **Everything else** | Environment variables, substituted into the config at start-up | hosts, ports, sender and recipient addresses, channel names, chat id |

There are three credentials:

| Secret | Local file | Read by Alertmanager as |
|---|---|---|
| `alert_smtp_password` | `./secrets/alert_smtp_password` | `smtp_auth_password_file` |
| `alert_slack_webhook_url` | `./secrets/alert_slack_webhook_url` | `api_url_file`, on every Slack receiver |
| `alert_telegram_bot_token` | `./secrets/alert_telegram_bot_token` | `bot_token_file`, on every Telegram receiver |

`./scripts/init-secrets.sh` creates all three as **empty placeholder files**
with mode `0600`. They have to exist even when unused: Docker refuses to start
a stack whose declared secret file is missing, so an absent file would take the
whole deployment down rather than leave one notification channel unconfigured.
Re-running the script never overwrites them.

`docker-compose.prod.yml` declares each one as `file: ./secrets/<name>` and
mounts all three on the **`alertmanager`** service only — not on
`alertmanager-config`, which renders the config template and has no need to see
a credential in any form. Alertmanager reads each file at notification time, so
the rendered `alertmanager.yml` contains paths and never a value.

> Alerting credentials used to be the one exception to the secrets rule: they
> were substituted into the rendered config by envsubst from shell environment
> variables, which meant `docker inspect` on the init container disclosed them.
> `ALERT_SMTP_PASSWORD`, `ALERT_SLACK_WEBHOOK_URL` and
> `ALERT_TELEGRAM_BOT_TOKEN` are no longer read by anything. If a sourced env
> file still sets them, delete those lines — they have no effect and are only
> one more copy of a credential.

All channels are opt-in. A channel whose secret file is still empty is simply
not configured. **Configure at least one** — the pipeline runs happily with
zero receivers and delivers nothing, which looks identical to having no alerts.

### Writing a credential

```bash
printf '%s' "$THE_VALUE" > ./secrets/alert_slack_webhook_url
chmod 600 ./secrets/alert_slack_webhook_url
docker compose -f docker-compose.prod.yml up -d alertmanager
```

Use `printf`, never `echo`. A trailing newline becomes part of the credential
and the authentication failure that follows does not say so.

### Slack

Create an Incoming Webhook in your Slack workspace and write the URL into
`./secrets/alert_slack_webhook_url` as above. The rest is configuration:

```bash
ALERT_SLACK_CHANNEL=#alerts
ALERT_SLACK_CHANNEL_BACKUP=#alerts-backups   # optional, defaults to ALERT_SLACK_CHANNEL
ALERT_SLACK_CHANNEL_COST=#alerts-cost        # optional, defaults to ALERT_SLACK_CHANNEL
```

### Telegram

Create a bot via `@BotFather`, add it to a group, then get the chat ID by
messaging the group and reading `getUpdates` from the Bot API. The bot token
goes into `./secrets/alert_telegram_bot_token`; the chat ID is configuration,
not a credential:

```bash
ALERT_TELEGRAM_CHAT_ID=-1001234567890
```

Group chat IDs are negative. Supergroup IDs start `-100`. Getting this wrong
fails silently — Telegram returns an error Alertmanager logs and nothing
else happens.

### Email

The SMTP password goes into `./secrets/alert_smtp_password`. Everything else:

```bash
ALERT_SMTP_HOST=smtp.example.com
ALERT_SMTP_PORT=587
ALERT_SMTP_USER=alerts@example.com
ALERT_EMAIL_FROM=alerts@example.com
ALERT_EMAIL_TO=oncall@example.com
```

Use an app-specific password. Gmail and most providers reject account
passwords over SMTP.

### Verify it works

Do this **before** you need it:

```bash
docker compose -f docker-compose.prod.yml up -d alertmanager

# Fire a fake alert straight at Alertmanager.
docker compose -f docker-compose.prod.yml exec alertmanager \
  wget -qO- --post-data='[{"labels":{"alertname":"TestAlert","severity":"critical","category":"availability","service":"api","environment":"production"},"annotations":{"summary":"Test","description":"Delivery test"}}]' \
  --header='Content-Type: application/json' \
  localhost:9093/api/v2/alerts
```

A message should appear in every configured channel within ~10 seconds. If it
does not, check `docker compose logs alertmanager`.

Read those logs on the first deploy even if you are not testing delivery yet.
All three secrets are mounted whether or not anything has been written into
them, and an empty file is the expected state for a channel you have not
configured — but confirm Alertmanager started clean rather than assuming it
did.

---

## Severity

| Severity | Means | Goes to |
|---|---|---|
| `critical` | Customers are affected now, or data is at risk | Slack + Telegram + Email, repeats hourly |
| `warning` | Will become critical if ignored | Slack + Telegram, repeats every 4h |
| `info` | Worth knowing, needs no action tonight | Slack only, repeats daily |

---

## Grouping, routing, silencing

**Grouping** is by `alertname`, `severity` and `service` — deliberately *not*
by instance. When a host dies, grouping by instance sends one notification per
alert per target; this collapses it into a single message.

**Routing** matches the `category` label that every rule carries. Adding a rule
to an existing category needs no routing change. Routing on alertname instead
would mean the one rule somebody forgets to route goes to the default receiver,
which in practice is nowhere anyone looks.

**Inhibition** suppresses consequences so you get the cause:

| When this fires | These are suppressed |
|---|---|
| `HostDown` | Everything else |
| `PostgresDown` | `HighErrorRate`, `HighLatency`, `ApiDown` |
| `RedisDown` | `WorkerDown`, `HighErrorRate` |
| `SpendGuardTripped` | `AIDisabled` |
| `BackupFailed` | `BackupMissing` |
| any `critical` | the `warning` of the same alert + service |

**Silencing** is done in the Alertmanager UI (`127.0.0.1:9093` — bound to
localhost, reach it over an SSH tunnel) or the API:

```bash
ssh -L 9093:127.0.0.1:9093 user@server
```

> Always set an expiry on a silence. An indefinite silence during an incident
> is how an alert gets disabled permanently by accident, and nobody discovers
> it until the thing it watched fails unnoticed.

---

## Every alert

### Availability

| Alert | Fires when | Severity | What to do |
|---|---|---|---|
| `ApiDown` | `up{job="api"} == 0` for 2m | critical | Meta retries webhooks for a limited window then drops them permanently. Check `docker compose ps`, then app logs. |
| `WorkerDown` | `up{job="worker"} == 0` for 2m | critical | Webhooks still queue, so nothing looks broken externally — but nobody is being answered. Check for OOM kills. |
| `WorkerCrashLooping` | >3 restarts in 30m | critical | `up` cannot see this; the worker is back before the next scrape. Usually OOM or an import-time exception. |
| `HostDown` | `up{job="node"} == 0` | critical | Alone: the exporter died. With everything else: the machine is gone. |
| `PostgresDown` | `pg_up == 0` | critical | Total outage. No message can be stored. |
| `RedisDown` | `redis_up == 0` | critical | Broker + idempotency + quotas. **The quota layer fails open, so the spend ceiling is not enforced while this is true.** |
| `AIDisabled` | `ai_disabled == 1` for 2m | critical | Business outage with zero errors, zero latency, zero failed requests. No other alert catches it. |
| `NoInboundMessages` | 0 inbound in 30m after >10 in 6h | critical | Broken Meta subscription, expired cert, or nginx misconfigured. |
| `PublicEndpointDown` | `probe_success == 0` for 5m | critical | The only check exercising DNS + nginx + TLS together. |

### Reliability

| Alert | Fires when | Severity | What to do |
|---|---|---|---|
| `HighErrorRate` | >5% 5xx for 10m | critical | `clamp_min` keeps the ratio sane at low traffic. |
| `HighLatency` | p95 > 2s for 10m | warning | Meta retries slow deliveries, adding load to an already-slow service. |
| `ReplyReservationsUnconfirmed` | any in 15m | warning | Workers dying *after* a send but *before* the confirming commit. Retries deliberately do not resend, so customers may have got nothing. Check `stop_grace_period` > `CELERY_TASK_TIME_LIMIT`. |
| `WebhookDeadLetters` | any in 1h | critical | A delivery exhausted all five retries. Customer got no reply at all. |
| `DeadLetterQueueNotEmpty` | any in 24h, for 30m | warning | Drain and investigate. See the note below on why this counts arrivals rather than queue depth. |
| `TaskTimeouts` | >3 in 15m | warning | Usually a hung external call. Check OpenAI and Meta status before raising the limit. |
| `HighAbuseBlockRate` | >5 blocks in 15m | warning | Genuine attack, or thresholds too tight — several photos in a row trips it. |
| `SustainedRateLimiting` | >50 in 1h | info | If these are real customers, `CUSTOMER_LIMIT_*` is too low. |

### Integrations

| Alert | Fires when | Severity | What to do |
|---|---|---|---|
| `OpenAIErrorRate` | >20% failing for 10m | warning | Customers getting the fallback reply. |
| `OpenAIDown` | 100% failing for 5m | critical | Key revoked, no credit, or org rate limited. |
| `WhatsAppApiFailures` | >5 errors in 15m | critical | Replies generated then failing to send — costs tokens, delivers nothing. |
| `WhatsAppTokenLikelyExpired` | >20 send errors in 1h *while inbound still arrives* | critical | That asymmetry is the signature of a revoked token. Rotate `WHATSAPP_TOKEN`. |

### Cost

| Alert | Fires when | Severity | What to do |
|---|---|---|---|
| `SpendApproachingLimit` | spend ≥ 80% of ceiling | warning | Derived from `openai_spend_guard_limit_usd`, so it follows the setting automatically. |
| `DailySpendExceeded` | spend ≥ ceiling | critical | Circuit breaker open. Nobody is being answered until midnight UTC. |
| `SpendGuardTripped` | any trip in 1h | critical | Everyone routed to a human. |
| `HighTokenUsage` | tokens ≥ 80% of daily ceiling | warning | Tokens and dollars diverge — a cheaper model burns more tokens for the same money. |
| `TokenBurstAnomaly` | 5× the 6h baseline | warning | Catches a runaway prompt long before either daily ceiling notices. A retrieval bug stuffing the whole knowledge base into every request looks normal per-request. |

### Resources

| Alert | Fires when | Severity | What to do |
|---|---|---|---|
| `DiskAlmostFull` | <20% free for 15m | warning | Check `backup_local_bytes_total` before assuming it is the database. |
| `DiskCriticallyFull` | <5% free for 5m | critical | Postgres stops accepting writes when it cannot extend a file. Fires faster than the warning because 5% can vanish in an hour. |
| `MemoryPressure` | <10% available for 10m | warning | `MemAvailable`, not `MemFree` — low free memory is normal on Linux. The OOM killer usually takes the worker mid-send. |
| `CpuOverload` | >90% for 15m | warning | The nightly `pg_dump -Z9` is expected to saturate a core for a few minutes. Fifteen is not that. |
| `SSLCertExpiringSoon` | <21 days | warning | Let's Encrypt renews at 30 days, so 21 means renewal already failed once. |
| `SSLCertExpiringCritical` | <7 days | critical | Renewal is definitively broken. When the cert expires Meta stops delivering webhooks entirely. |

### Data protection

All age-based, because the failure that actually happens is a backup container
running happily while producing nothing — and liveness checks cannot see that.

| Alert | Fires when | Severity | What to do |
|---|---|---|---|
| `BackupFailed` | `backup_last_status == 0` | critical | See `/backups/state/last_result.json`. |
| `BackupMissing` | no success in 30h | critical | **The important one.** Also fires on a timestamp of 0, which is what a deployment that has never backed up looks like. |
| `OffsiteUploadMissing` | nothing uploaded in 36h | critical | Local backups may be fine, but the only copies are on the same machine as the database. Expired Azure SAS or rotated S3 key fails exactly like this. |
| `OffsiteVerificationFailed` | `backup_remote_verify_status == 0` | critical | Remote object would not download or did not match its checksum. |
| `OffsiteVerificationStale` | not verified in 3 days | warning | The verification job itself stopped. Unverified backups are assumptions. |
| `RestoreDrillFailed` | `restore_drill_status == 0` | critical | A backup that will not restore is not a backup. Drill environment is preserved — see `/backups/drills/`. |
| `RestoreDrillStale` | no passing drill in 10 days | warning | Drills run weekly. Ten days means it stopped, quietly returning you to untested backups. |

---

## Two deliberate design decisions

**Dead-letter alerting counts arrivals, not queue depth.** A depth gauge would
need refreshing from Redis on a schedule. One updated only on write stays stuck
above zero forever once the queue is drained by hand — a permanent alert that
gets muted and then hides the next real incident. Counting arrivals over 24h is
a weaker signal but an honest one: it clears on its own.

**Cost thresholds are derived, not restated.** The previous rule hard-coded
20 USD with a comment saying it had to be hand-synced with
`DAILY_SPEND_LIMIT_USD`. Anything requiring manual synchronisation eventually
is not synchronised. The limits are now exported as
`openai_spend_guard_limit_usd` and `openai_daily_token_limit`.

---

## Known gap

**Nothing alerts on Alertmanager itself being down.** Prometheus scrapes it and
`up{job="alertmanager"} == 0` is queryable, but an alert about the alerting
pipeline cannot be delivered by that same pipeline. Closing this properly needs
a second, independent monitoring system or an external dead-man's-switch
service that expects a periodic heartbeat and shouts when it stops arriving.
That is not deployed here.
