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
to defaults.

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

## 6. Rotation

1. Update the value in Vault, or rewrite the file in `./secrets/`.
2. `docker compose -f docker-compose.prod.yml up -d --force-recreate app worker`
   (Swarm: `docker service update --secret-rm/--secret-add`).
3. Revoke the old credential at the provider (Meta, OpenAI).

Rotate `ADMIN_API_KEY` and `WHATSAPP_VERIFY_TOKEN` with
`openssl rand -base64 36`.
