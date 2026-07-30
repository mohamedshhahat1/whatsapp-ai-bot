"""Application configuration loaded from environment variables (12-factor).

Production never relies on a ``.env`` file. Credentials come from Docker
secrets, ``<FIELD>_FILE`` files, Vault or the process environment (GitHub
Actions secrets) -- see ``app/core/secrets.py`` and ``docs/SECRETS.md``.
"""

import os
from functools import lru_cache

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


def _running_in_production() -> bool:
    return os.environ.get("ENVIRONMENT", "development").strip().lower() == "production"


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

    # Background queue (Celery). Broker/backend default to redis_url when empty.
    use_task_queue: bool = True
    celery_broker_url: str = ""
    celery_result_backend: str = ""

    # Rate limiting (limit strings use the `limits` notation, e.g. "60/minute")
    rate_limit_enabled: bool = True
    rate_limit_webhook: str = "600/minute"
    rate_limit_admin: str = "60/minute"
    # Number of reverse proxies in front of the app that append to
    # X-Forwarded-For. Only the last N entries are trustworthy; everything to
    # the left of them is client-supplied. Set to 0 when the app is exposed
    # directly with no proxy.
    trusted_proxy_hops: int = 1

    # Outbound retries (tenacity, exponential backoff with jitter)
    retry_max_attempts: int = 3
    retry_backoff_max_seconds: float = 8.0

    # Observability
    metrics_enabled: bool = True
    worker_metrics_port: int = 9100
    # USD prices per 1M tokens. FALLBACK ONLY: spend is computed from the
    # model_pricing table so historical figures stay correct across price
    # changes. These apply only to a model with no pricing row at all.
    openai_input_price_per_1m: float = 0.40
    openai_output_price_per_1m: float = 1.60

    # Reliability: deliveries that exhaust their Celery retries are pushed onto
    # this Redis list instead of vanishing (see app/workers/tasks.py).
    dead_letter_key: str = "webhooks:dead-letter"
    dead_letter_max_entries: int = 1000

    # Secret backends (see app/core/secrets.py)
    secrets_dir: str = "/run/secrets"
    vault_enabled: bool = False
    vault_addr: str = ""
    vault_kv_mount: str = "secret"
    vault_secret_path: str = ""

    # Knowledge base / RAG (see docs/RAG.md).
    # Consumers: services/ingestion.py, services/retrieval.py,
    # integrations/embeddings.py, core/chunking.py, scripts/ingest_knowledge.py.
    rag_enabled: bool = True
    knowledge_dir: str = "knowledge"
    # Ingestion and retrieval must use the SAME embedding model, and
    # embedding_dimensions must match the vector column width created by
    # migration 0001_knowledge_base.
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
    # Used only in fixed copy the bot sends without a model call -- currently
    # the out-of-scope redirect (see app/services/intent.py). Unset is safe:
    # the copy falls back to "our services" rather than printing a blank.
    company_name: str = ""
    max_output_tokens: int = 512
    max_context_messages: int = 20
    max_context_tokens: int = 6000

    # Sales (see app/services/price_policy.py and docs/PRICING_POLICY.md).
    # The bot never states a price. Every pricing question is redirected to a
    # person, and this is the number it offers. Leave it empty and the bot asks
    # the customer for their number instead -- it will never invent one.
    # This is contact information, not a credential: it is safe in .env and is
    # deliberately not listed in REQUIRED_IN_PRODUCTION, because an unset
    # number degrades gracefully rather than breaking the conversation.
    sales_phone: str = ""

    # Admin API
    admin_api_key: str = "change-me"

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


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
