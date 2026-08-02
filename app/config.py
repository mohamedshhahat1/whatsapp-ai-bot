"""Application configuration loaded from environment variables (12-factor).

Production never relies on a ``.env`` file. Credentials come from Docker
secrets, ``<FIELD>_FILE`` files, Vault or the process environment (GitHub
Actions secrets) -- see ``app/core/secrets.py`` and ``docs/SECRETS.md``.
"""

import os
from functools import lru_cache
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from app.core.secrets import (
    FileEnvSecretsSource,
    VaultSettingsSource,
    resolve_secrets_dir,
)

# Secrets that must be provided by a real backend before the app may boot in
# production.
REQUIRED_IN_PRODUCTION = (
    "openai_api_key",
    "whatsapp_token",
    "whatsapp_phone_number_id",
    "whatsapp_verify_token",
    "whatsapp_app_secret",
    "admin_api_key",
)

# Values that mean "not configured" -- copied from .env.example or defaults.
PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "change-me",
        "sk-your-key",
        "choose-a-random-string",
        "choose-a-strong-random-key",
        "your-meta-app-secret",
        "EAAG-your-permanent-token",
    }
)

_REDIS_SCHEMES = frozenset({"redis", "rediss"})


def _running_in_production() -> bool:
    return os.environ.get("ENVIRONMENT", "development").strip().lower() == "production"


def _redis_url_has_password(url: str) -> bool:
    """True when the URL already carries credentials of its own."""
    if not url:
        return False
    try:
        return bool(urlsplit(url).password)
    except ValueError:
        return False


def apply_redis_credentials(
    url: str,
    *,
    username: str = "",
    password: str = "",
    tls: bool = False,
) -> str:
    """Return ``url`` with credentials and TLS applied.

    Injecting here rather than at each call site is the whole point: every
    Redis consumer in the app derives its URL from ``redis_url``, so one
    change authenticates all of them and a future consumer cannot forget to.

    A password already present in the URL always wins -- an operator who wrote
    credentials into ``REDIS_URL`` explicitly meant them, and silently
    overwriting them would be worse than either behaviour on its own.

    The password is percent-encoded. Redis passwords are generated with
    ``openssl rand -base64``, which emits ``+`` and ``/``; unencoded, ``/``
    truncates the URL at the database number and the client connects to the
    wrong database with a mangled password.
    """
    if not url:
        return url

    try:
        parts = urlsplit(url)
    except ValueError:
        # Not parseable -- hand it back untouched rather than corrupting it.
        return url

    if parts.scheme not in _REDIS_SCHEMES:
        # Someone is pointing Celery at RabbitMQ or similar. Leave it alone.
        return url

    scheme = "rediss" if tls else parts.scheme

    if parts.password:
        netloc = parts.netloc
    elif password:
        host = parts.hostname or "localhost"
        if ":" in host:
            # IPv6 literal.
            host = f"[{host}]"
        authority = f"{host}:{parts.port}" if parts.port else host
        netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{authority}"
    else:
        netloc = parts.netloc

    return urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))


class Settings(BaseSettings):
    """Central, environment-based application settings."""

    model_config = SettingsConfigDict(
        # The .env file is a local development convenience only.
        env_file=None if _running_in_production() else ".env",
        env_file_encoding="utf-8",
        # Docker secrets: /run/secrets/<field_name> (when the directory exists).
        secrets_dir=resolve_secrets_dir(),
        extra="ignore",
    )

    # Application
    app_name: str = "whatsapp-ai-bot"
    environment: str = "development"
    debug: bool = False

    # Infrastructure
    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/whatsapp_ai_bot"
    )
    redis_url: str = "redis://localhost:6379/0"

    # Redis authentication (see docs/REDIS_SECURITY.md).
    redis_password: str = ""
    redis_username: str = ""
    redis_tls: bool = False
    redis_auth_required: bool = True

    # Background queue (Celery). Broker/backend default to redis_url when empty.
    use_task_queue: bool = True
    celery_broker_url: str = ""
    celery_result_backend: str = ""
    celery_visibility_timeout: int = 900
    celery_task_soft_time_limit: int = 240
    celery_task_time_limit: int = 300
    reply_idempotency_ttl_seconds: int = 86400

    # Rate limiting
    rate_limit_enabled: bool = True
    rate_limit_webhook: str = "6000/minute"
    rate_limit_admin: str = "60/minute"
    trusted_proxy_hops: int = 1

    # Per-customer quotas
    customer_rate_limit_enabled: bool = True
    customer_limit_per_minute: int = 12
    customer_limit_per_hour: int = 120
    customer_limit_per_day: int = 400
    flood_burst_messages: int = 5
    flood_burst_seconds: int = 10
    duplicate_message_limit: int = 5
    duplicate_message_window_seconds: int = 300
    abuse_block_seconds: int = 900

    # OpenAI spend protection
    spend_guard_enabled: bool = True
    daily_spend_limit_usd: float = 25.0
    daily_token_limit: int = 5_000_000
    spend_alert_threshold: float = 0.8

    # Outbound retries
    retry_max_attempts: int = 3
    retry_backoff_max_seconds: float = 8.0

    # Observability
    metrics_enabled: bool = True
    worker_metrics_port: int = 9100
    openai_input_price_per_1m: float = 0.40
    openai_output_price_per_1m: float = 1.60

    # Reliability
    dead_letter_key: str = "webhooks:dead-letter"
    dead_letter_max_entries: int = 1000

    # Secret backends
    secrets_dir: str = "/run/secrets"
    vault_enabled: bool = False
    vault_addr: str = ""
    vault_kv_mount: str = "secret"
    vault_secret_path: str = ""

    # Knowledge base / RAG
    rag_enabled: bool = True
    knowledge_dir: str = "knowledge"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_batch_size: int = 64
    chunk_max_tokens: int = 400
    chunk_overlap_tokens: int = 60
    rag_top_k: int = 5
    rag_min_score: float = 0.25
    rag_max_context_chars: int = 6000

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    system_prompt: str = "You are a helpful WhatsApp assistant for our business."
    company_info: str = ""
    company_name: str = ""
    # Company website and portfolio URLs. When configured, the bot directs
    # customers asking about designs, examples or previous work to these
    # links instead of saying it has none. If company_portfolio_url is empty,
    # the bot falls back to company_website. If both are empty, the portfolio
    # section is omitted from the prompt entirely.
    company_website: str = ""
    company_portfolio_url: str = ""
    max_output_tokens: int = 512
    max_context_messages: int = 20
    max_context_tokens: int = 6000

    # Sales
    sales_phone: str = ""

    # WhatsApp Cloud API
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_api_version: str = "v21.0"

    # Admin API
    admin_api_key: str = "change-me"

    # TLS / Let's Encrypt
    domain: str = ""
    certbot_email: str = ""

    # Backup retention (local)
    backup_hour: int = 2
    retain_daily: int = 14
    retain_weekly: int = 8
    retain_monthly: int = 12
    backup_max_age_hours: int = 30

    # Backup remote storage
    backup_remote_provider: str = "none"
    backup_remote_bucket: str = ""
    backup_remote_prefix: str = "whatsapp-ai-bot"
    retain_remote_daily: int = 30
    retain_remote_weekly: int = 12
    retain_remote_monthly: int = 24
    backup_remote_retry_attempts: int = 5
    backup_remote_retry_base_seconds: int = 3
    backup_remote_retry_max_seconds: int = 60
    backup_remote_max_age_hours: int = 30

    # Backup encryption
    backup_encryption_passphrase: str = ""
    backup_age_recipient: str = ""
    backup_age_identity_file: str = ""
    backup_compress_before_encrypt: bool = False

    # Backup S3
    backup_s3_access_key_id: str = ""
    backup_s3_secret_access_key: str = ""
    backup_s3_region: str = "us-east-1"
    backup_s3_endpoint: str = ""
    backup_s3_storage_class: str = ""

    # Backup GCS
    backup_gcs_credentials_file: str = ""
    backup_gcs_project: str = ""

    # Backup Azure
    backup_azure_account: str = ""
    backup_azure_sas_token: str = ""
    backup_azure_account_key: str = ""

    # Backup misc
    backup_metrics_dir: str = "/backups/metrics"
    backup_download_dir: str = ""

    # Restore drill
    restore_drill_enabled: bool = True
    restore_drill_days: int = 7
    restore_drill_db: str = "restore_drill"
    restore_drill_image: str = "pgvector/pgvector:pg16"
    restore_drill_port: int = 55432
    restore_drill_app_port: int = 58000
    restore_drill_password: str = "drillpass"
    restore_drill_app_image: str = ""
    restore_drill_tables: str = (
        "users conversations messages ai_logs documents model_pricing"
    )

    # Alerting: Telegram
    alert_telegram_bot_token: str = ""
    alert_telegram_chat_id: str = ""

    # Alerting: Slack
    alert_slack_webhook_url: str = ""
    alert_slack_channel: str = "#alerts"
    alert_slack_channel_backup: str = ""
    alert_slack_channel_cost: str = ""

    # Alerting: Email (SMTP)
    alert_smtp_host: str = ""
    alert_smtp_port: int = 587
    alert_smtp_user: str = ""
    alert_smtp_password: str = ""
    alert_email_from: str = ""
    alert_email_to: str = ""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Highest priority first: env > *_FILE > Vault > Docker secrets > .env."""
        return (
            init_settings,
            env_settings,
            FileEnvSecretsSource(settings_cls),
            VaultSettingsSource(settings_cls),
            file_secret_settings,
            dotenv_settings,
        )

    @model_validator(mode="after")
    def _require_production_secrets(self) -> "Settings":
        """Fail fast when production boots without real credentials."""
        if self.environment.strip().lower() != "production":
            return self
        missing = [
            name
            for name in REQUIRED_IN_PRODUCTION
            if str(getattr(self, name)).strip() in PLACEHOLDER_VALUES
        ]
        if missing:
            raise ValueError(
                "Missing production secrets: "
                + ", ".join(missing)
                + ". Provide them through Docker secrets (/run/secrets/<name>), "
                "a <NAME>_FILE variable, Vault, or the process environment. "
                "The .env file is not read when ENVIRONMENT=production."
            )
        return self

    @model_validator(mode="after")
    def _apply_redis_credentials(self) -> "Settings":
        """Merge the Redis password and TLS setting into every Redis URL."""
        self.redis_url = apply_redis_credentials(
            self.redis_url,
            username=self.redis_username,
            password=self.redis_password,
            tls=self.redis_tls,
        )
        self.celery_broker_url = apply_redis_credentials(
            self.celery_broker_url,
            username=self.redis_username,
            password=self.redis_password,
            tls=self.redis_tls,
        )
        self.celery_result_backend = apply_redis_credentials(
            self.celery_result_backend,
            username=self.redis_username,
            password=self.redis_password,
            tls=self.redis_tls,
        )
        return self

    @model_validator(mode="after")
    def _require_redis_auth(self) -> "Settings":
        """Refuse to boot production against an unauthenticated Redis."""
        if self.environment.strip().lower() != "production":
            return self
        if not self.redis_auth_required:
            return self
        if self.redis_password.strip() or _redis_url_has_password(self.redis_url):
            return self
        raise ValueError(
            "Redis authentication is not configured. Provide REDIS_PASSWORD "
            "through a Docker secret (/run/secrets/redis_password), a "
            "REDIS_PASSWORD_FILE path, Vault, or the environment -- or embed "
            "credentials in REDIS_URL. Run ./scripts/init-secrets.sh to "
            "generate one. Set REDIS_AUTH_REQUIRED=false to accept an "
            "unauthenticated Redis deliberately."
        )

    @property
    def database_url_sync(self) -> str:
        """Synchronous database URL used by Alembic migrations."""
        return self.database_url.replace("+asyncpg", "+psycopg")

    @property
    def broker_url(self) -> str:
        """Celery broker URL (falls back to Redis)."""
        return self.celery_broker_url or self.redis_url

    @property
    def result_backend(self) -> str:
        """Celery result backend URL (falls back to Redis)."""
        return self.celery_result_backend or self.redis_url

    @property
    def portfolio_url(self) -> str:
        """Portfolio URL, falling back to the company website if not set."""
        return self.company_portfolio_url.strip() or self.company_website.strip()


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
