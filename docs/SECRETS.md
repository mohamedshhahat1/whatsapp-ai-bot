# Secrets management

The app reads secrets from several backends so that **production never depends
on a `.env` file**. `.env` is a local development convenience only: when
`ENVIRONMENT=production`, the dotenv source is not registered at all
(`app/config.py`), and startup fails fast if any required credential is
missing or still a placeholder.

## Resolution order

Highest priority first (`Settings.settings_customise_sources`):

| # | Source | Typical use |
|---|--------|-------------|
| 1 | Values passed to `Settings(...)` | tests |
| 2 | Environment variables | GitHub Actions secrets, systemd, Kubernetes `env` |
| 3 | `<FIELD>_FILE` env vars | Kubernetes projected volumes, Vault Agent templates |
| 4 | HashiCorp Vault (KV v2) | central secret store with rotation & audit |
| 5 | Docker secrets in `SECRETS_DIR` (`/run/secrets`) | Docker Compose / Swarm |
| 6 | `.env` file | **development only** — skipped in production |

Required in production: `OPENAI_API_KEY`, `WHATSAPP_TOKEN`,
`WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET`,
`ADMIN_API_KEY`. Requiring `WHATSAPP_APP_SECRET` also guarantees Meta webhook
signature verification is never silently disabled in production.

## 1. Docker secrets (default for self-hosted deploys)

```bash
./scripts/init-secrets.sh                       # generates ./secrets/* (0600)
docker compose -f docker-compose.prod.yml up -d
```

Each file is mounted read-only at `/run/secrets/<name>`; the file name matches
the settings field (`openai_api_key`, `database_url`, ...), so no code change
is needed to consume it. Postgres reads its own password via
`POSTGRES_PASSWORD_FILE`, Grafana via `GF_SECURITY_ADMIN_PASSWORD__FILE`.

### Optional credentials are empty placeholders, not missing files

Docker refuses to start a stack when the file backing a declared secret does
not exist. Optional credentials are therefore created **empty** rather than
left out: an empty file disables the one feature it belongs to, while a missing
file takes the entire deployment down. `init-secrets.sh` writes these empty and
leaves them alone on a re-run:

| Secret | Enables | Value comes from |
|---|---|---|
| `backup_s3_access_key_id`, `backup_s3_secret_access_key` | off-site backups (S3, B2) | your storage provider |
| `backup_gcs_credentials` | off-site backups (GCS) | a service-account JSON file |
| `backup_azure_sas_token` | off-site backups (Azure) | a container SAS |
| `fcm_credentials` | mobile push notifications | a Firebase service-account JSON file |
| `alert_smtp_password` | email alerts | the sending mail account |
| `alert_slack_webhook_url` | Slack alerts | a Slack incoming webhook |
| `alert_telegram_bot_token` | Telegram alerts | `@BotFather` |

Fill one in with `printf`, never `echo` — a trailing newline becomes part of
the credential and the authentication failure that follows does not say so:

```bash
printf '%s' "$THE_VALUE" > ./secrets/alert_slack_webhook_url
chmod 600 ./secrets/alert_slack_webhook_url
docker compose -f docker-compose.prod.yml up -d alertmanager
```

### Alertmanager reads its own secrets

The three `alert_*` files are the only secrets the **application** never reads.
Alertmanager reads them itself, through the `*_file` form of each field in
`monitoring/alertmanager.yml.tmpl`:

| Secret | Alertmanager field |
|---|---|
| `alert_smtp_password` | `smtp_auth_password_file` |
| `alert_slack_webhook_url` | `api_url_file`, on every Slack receiver |
| `alert_telegram_bot_token` | `bot_token_file`, on every Telegram receiver |

They are mounted on the `alertmanager` service only. The `alertmanager-config`
init container renders the template and is deliberately given none of them, so
`docker inspect` on it discloses nothing and the rendered config on the shared
volume holds only `/run/secrets/...` paths.

This replaces an earlier arrangement in which the values were substituted into
the rendered config from `ALERT_SMTP_PASSWORD`, `ALERT_SLACK_WEBHOOK_URL` and
`ALERT_TELEGRAM_BOT_TOKEN`. Those variables are no longer read by anything and
should be removed from any env file that still sets them. The non-secret half
of alerting — SMTP host and port, sender and recipient, channel names, the
Telegram chat id — stays in the deployment environment; it is configuration,
not credentials. See [docs/ALERTING.md](ALERTING.md).

### Swarm

On Docker Swarm, switch the `secrets:` block to external secrets:

```bash
printf '%s' "$OPENAI_API_KEY" | docker secret create openai_api_key -
```

```yaml
secrets:
  openai_api_key:
    external: true
```

## 2. `<FIELD>_FILE` variables (Kubernetes, CI, Vault Agent)

Any setting can be pointed at a file:

```bash
OPENAI_API_KEY_FILE=/var/run/secrets/openai/key
DATABASE_URL_FILE=/var/run/secrets/db/url
```

This keeps values out of the process environment, where they leak into `/proc`,
child processes and crash dumps.

## 3. HashiCorp Vault (KV v2)

```bash
vault kv put secret/whatsapp-ai-bot \
  OPENAI_API_KEY=sk-... \
  WHATSAPP_TOKEN=EAAG... \
  WHATSAPP_APP_SECRET=... \
  ADMIN_API_KEY=...
```

```bash
VAULT_ENABLED=true
VAULT_ADDR=https://vault.internal:8200
VAULT_KV_MOUNT=secret
VAULT_SECRET_PATH=whatsapp-ai-bot
# Auth: a token...
VAULT_TOKEN_FILE=/run/secrets/vault_token
# ...or AppRole
VAULT_ROLE_ID=...
VAULT_SECRET_ID=...
```

Keys are matched to settings fields case-insensitively. If Vault is enabled but
unreachable, startup raises `SecretLoadError` instead of silently falling back
to defaults. Alertmanager does not use Vault: it reads the three `alert_*`
Docker secrets directly.

## 4. GitHub Secrets (CI/CD)

The pipeline never stores credentials in the repository:

| Kind | Name | Purpose |
|---|---|---|
| Secret (automatic) | `GITHUB_TOKEN` | push the image to GHCR |
| Secret | `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_PATH`, `DEPLOY_PORT` | SSH deployment |
| Variable | `DEPLOY_ENABLED`, `DEPLOY_URL` | enable deploy + health-check URL |

Test jobs use obviously fake values (`test-key`, `test-admin-key`). Production
secrets live only on the server (Docker secrets) or in Vault — the deploy job
never transports them.

## 5. Leak prevention

- CI runs **gitleaks** on every push (`secret-scan` job); `.gitleaks.toml`
  allowlists documentation placeholders and test fixtures.
- `.gitignore` excludes `.env`, `.env.*` (except `.env.example`) and
  `secrets/`.
- Secrets are never logged: structlog only records field names, and
  `app/core/metrics.py` labels contain no credentials.
- Nothing rendered at start-up contains a credential. The Alertmanager config
  is generated into a named volume from a template that substitutes only
  non-secret values, so the generated file is safe to read during an incident.

## 6. Rotation

1. Update the value in Vault, or rewrite the file in `./secrets/`.
2. `docker compose -f docker-compose.prod.yml up -d --force-recreate app worker`
   (Swarm: `docker service update --secret-rm/--secret-add`).
3. Revoke the old credential at the provider (Meta, OpenAI).

Rotate `ADMIN_API_KEY` and `WHATSAPP_VERIFY_TOKEN` with
`openssl rand -base64 36`.

The `alert_*` credentials rotate the same way but recreate a different service,
because the application never reads them:

```bash
docker compose -f docker-compose.prod.yml up -d --force-recreate alertmanager
```
