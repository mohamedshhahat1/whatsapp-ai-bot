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
    #
    # Supplied as a Docker secret (/run/secrets/redis_password), a
    # REDIS_PASSWORD_FILE path, or the REDIS_PASSWORD environment variable.
    # It is merged into redis_url -- and into the Celery broker and backend
    # URLs when those are set explicitly -- by the validator below.
    redis_password: str = ""
    # Redis 6 ACL user. Empty means the `default` user, which is what the
    # application uses; the metrics exporter has its own restricted account.
    redis_username: str = ""
    # Switches redis:// to rediss://. Only needed when Redis is reached across
    # a network the compose bridge does not cover -- a managed instance or a
    # separate host. Pointless in-stack overhead otherwise.
    redis_tls: bool = False
    # Fail closed. A production stack whose Redis has no password is holding
    # the rate limits, spend counters and reply-idempotency keys in the open,
    # and the failure is invisible until someone reaches the port. Set
    # REDIS_AUTH_REQUIRED=false to accept that deliberately.
    redis_auth_required: bool = True

    # Background queue (Celery). Broker/backend default to redis_url when empty.
    use_task_queue: bool = True
    celery_broker_url: str = ""
    celery_result_backend: str = ""

    # How long the broker waits for an ack before handing a delivery to another
    # worker. Must exceed the slowest realistic task: an inbound message costs
    # an embedding call plus a completion plus two WhatsApp calls, and a
    # redelivery while the first attempt is still running is how one customer
    # message becomes two replies. 15 minutes is deliberately generous --
    # celery_task_time_limit below kills a genuinely stuck task long before it.
    celery_visibility_timeout: int = 900
    # Raises SoftTimeLimitExceeded inside the task, so cleanup still runs.
    celery_task_soft_time_limit: int = 240
    # Hard kill. The gap between the two is the cleanup budget.
    celery_task_time_limit: int = 300

    # How long a generated completion stays replayable so a retried delivery
    # is not billed twice (app/core/idempotency.py). A day comfortably outlaps
    # Meta's redelivery window.
    reply_idempotency_ttl_seconds: int = 86400

    # Rate limiting (limit strings use the `limits` notation, e.g. "60/minute")
    rate_limit_enabled: bool = True
    # A flood ceiling for the webhook endpoint as a whole, NOT per customer.
    # Every delivery arrives from Meta's addresses, so this is one shared
    # bucket by construction and can only ever be a crude DoS backstop. Real
    # isolation is per-wa_id, below. Keep it well above peak: throttling here
    # rejects Meta's POST and drops other customers' messages with it.
    rate_limit_webhook: str = "6000/minute"
    rate_limit_admin: str = "60/minute"
    # Number of reverse proxies in front of the app that append to
    # X-Forwarded-For. Only the last N entries are trustworthy; everything to
    # the left of them is client-supplied. Set to 0 when the app is exposed
    # directly with no proxy.
    trusted_proxy_hops: int = 1

    # Per-customer quotas (app/core/quota.py) ---------------------------------
    # Keyed on wa_id, enforced in the worker before any paid call. This is the
    # limit that actually isolates one customer from another.
    customer_rate_limit_enabled: bool = True
    # A person types a handful of messages a minute. Well above human pace so
    # an excited customer sending four short lines in a row is never touched.
    customer_limit_per_minute: int = 12
    customer_limit_per_hour: int = 120
    customer_limit_per_day: int = 400

    # Flood protection: a burst tighter than any human can type.
    flood_burst_messages: int = 5
    flood_burst_seconds: int = 10

    # Spam: the same text over and over. A frustrated customer repeating
    # themselves twice is normal; five identical messages is a script or a
    # stuck client.
    duplicate_message_limit: int = 5
    duplicate_message_window_seconds: int = 300

    # How long a customer stays blocked after tripping flood or spam
    # detection. Long enough to break a loop, short enough that a real person
    # who got carried away is not locked out of the business for the day.
    abuse_block_seconds: int = 900

    # OpenAI spend protection -------------------------------------------------
    # The circuit breaker that stops a runaway bill. Checked before every
    # completion; past the ceiling the model is off for everyone and customers
    # get approved copy pointing at a human.
    spend_guard_enabled: bool = True
    daily_spend_limit_usd: float = 25.0
    daily_token_limit: int = 5_000_000
    # Warn once at this fraction of either ceiling, so there is time to react
    # before the bot goes quiet.
    spend_alert_threshold: float = 0.8

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

    @model_validator(mode="after")
    def _apply_redis_credentials(self) -> "Settings":
        """Merge the Redis password and TLS setting into every Redis URL.

        Applied to the Celery broker and backend as well, because those are
        separate settings when set explicitly and would otherwise connect
        unauthenticated while ``redis_url`` looked correct.
        """
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
        """Refuse to boot production against an unauthenticated Redis.

        Redis holds the per-customer rate limits, the spend counters and the
        reply-idempotency keys. Reachable without a password, all three can be
        cleared by anyone who can open the port -- and nothing in the
        application would report a problem.
        """
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


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
