"""Application configuration loaded from environment variables (12-factor).

Production never relies on a ``.env`` file. Credentials come from Docker
secrets, ``<FIELD>_FILE`` files, Vault or the process environment (GitHub
Actions secrets) -- see ``app/core/secrets.py`` and ``docs/SECRETS.md``.
"""

import os
from datetime import timedelta
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
    # Company website and portfolio, injected into the prompt so the bot can
    # answer "can I see your work?" with a link instead of an apology (see
    # app/services/prompt_builder.py).
    #
    # Both are optional and degrade in that order: an unset portfolio URL
    # falls back to the website via the portfolio_url property below, and an
    # unset website omits the whole prompt section rather than emitting a
    # broken link. No domain is hardcoded anywhere in the codebase, so
    # reusing this bot for another business is a .env change.
    company_website: str = ""
    company_portfolio_url: str = ""
    max_output_tokens: int = 512
    max_context_messages: int = 20
    max_context_tokens: int = 6000

    # Conversation session lifecycle ------------------------------------------
    # A session is one complete visit: welcome, questions, goodbye. See
    # app/services/session_service.py and docs/SESSION_LIFECYCLE.md.
    #
    # The master switch. Off, nothing in the lifecycle runs: no welcome is
    # tracked, no session is ever closed and the sweeper returns immediately.
    # Conversations then behave exactly as they did before the feature
    # existed -- one endless thread per customer -- which is what makes this
    # a safe way to disable the whole thing in a hurry without a rollback.
    enable_conversation_session: bool = True
    #
    # A session goes idle after this many minutes with nothing happening in
    # EITHER direction -- not merely nothing from the customer. A reply the
    # bot is still composing is activity, and closing a conversation out from
    # under an answer in flight would be absurd.
    #
    # Five minutes fits WhatsApp, where people read on a phone and answer
    # within a minute or two. Raising it holds sessions open across longer
    # gaps and keeps more context; below about two minutes the bot starts
    # saying goodbye to customers who are still typing.
    conversation_idle_timeout_minutes: int = 5
    # Whether going idle ends the session at all. Off, the idle timer still
    # runs and is still readable, but nothing is ever closed and no goodbye is
    # sent -- sessions end only when a customer stops coming back. Distinct
    # from the switch below: this one governs CLOSING, that one only governs
    # the MESSAGE.
    conversation_close_after_idle: bool = True
    # Whether an idle session is given a goodbye at all. Turning this off
    # still closes the session on schedule -- so the next message starts a
    # fresh one and is greeted normally -- it just closes it silently.
    enable_conversation_closing_message: bool = True
    # The grace period after a session closes. A customer who writes back
    # within this many minutes resumes the SAME conversation -- same history,
    # no second welcome -- because a goodbye followed thirty seconds later by
    # "sorry, one more thing" is one conversation, and greeting them again
    # would read as though the bot had forgotten them.
    #
    # Set to 0 to disable resuming entirely, so every closed session is final
    # and any later message starts a new one.
    conversation_reopen_window_minutes: int = 30
    # Past this many hours a returning customer always starts a genuinely new
    # session, whatever the reopen window says. This is the outer bound on how
    # stale a resumed conversation may be: without it, a long reopen window
    # would let someone returning next week land in last week's thread, and
    # the model would answer them in the context of a finished job.
    new_session_after_hours: int = 24
    # Whether a new session greets its customer at all. Off, the welcome is
    # never sent by the lifecycle and conversations simply begin with an
    # answer to whatever was asked.
    enable_welcome_on_new_session: bool = True
    # Whether a returning customer whose previous session was closed is
    # greeted again. Off means the welcome is sent once per customer ever,
    # however long they have been away.
    enable_repeat_welcome_after_new_session: bool = True
    # The duplicate guards. Both default on and both should stay on: they are
    # the flags that make "exactly one welcome and one goodbye per session"
    # true, and turning either off permits a redelivered webhook or an
    # overlapping sweep to message the customer twice. Present because the
    # spec asks for them to be configurable, not because there is a good
    # reason to change them.
    prevent_duplicate_welcome: bool = True
    prevent_duplicate_closing: bool = True
    # Whether outgoing messages reset the idle timer as well as incoming ones.
    # On -- the default, and strongly recommended -- the timer measures
    # silence in the conversation. Off, it measures silence FROM THE CUSTOMER
    # only, so a slow completion or a long operator reply can be overtaken by
    # the sweeper and followed by a goodbye the customer never earned.
    reset_idle_timer_on_outgoing_message: bool = True
    # Overrides the approved Arabic closing copy in app/services/persona.py.
    # Empty means use that copy, exactly as system_prompt defers to the
    # persona: the default is multi-line text a real customer reads, which
    # wants version control and review far more than it wants a .env entry.
    conversation_closing_message: str = ""

    # Sales (see app/services/price_policy.py and docs/PRICING_POLICY.md).
    # The bot never states a price. Every pricing question is redirected to a
    # person, and this is the number it offers. Leave it empty and the bot asks
    # the customer for their number instead -- it will never invent one.
    # This is contact information, not a credential: it is safe in .env and is
    # deliberately not listed in REQUIRED_IN_PRODUCTION, because an unset
    # number degrades gracefully rather than breaking the conversation.
    sales_phone: str = ""

    # WhatsApp Cloud API (Meta for Developers -> your app -> WhatsApp) ---------
    # Consumers: routers/webhook.py uses whatsapp_verify_token for the
    # subscription handshake and whatsapp_app_secret for the
    # X-Hub-Signature-256 check; integrations/whatsapp.py uses whatsapp_token
    # as the bearer credential and whatsapp_phone_number_id to address the
    # messages endpoint.
    #
    # All four are listed in REQUIRED_IN_PRODUCTION, so production refuses to
    # boot while any of them is still empty or a .env.example placeholder.
    # They default to "" rather than to those placeholders so that a
    # development stack starts, and only the endpoints that actually need a
    # credential fail.
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""
    # Graph API version, pinned on purpose. Meta ships breaking changes between
    # versions and keeps old ones serving, so this is bumped deliberately after
    # reading the changelog rather than tracking whatever is current. Not a
    # credential, and not required in production: the default works.
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

    @property
    def portfolio_url(self) -> str:
        """Portfolio URL, falling back to the company website when unset.

        Returning "" when neither is configured is deliberate: the prompt
        builder tests this for truthiness and omits the portfolio section
        entirely rather than sending the customer a broken link.
        """
        return self.company_portfolio_url.strip() or self.company_website.strip()

    @property
    def conversation_idle_timeout(self) -> timedelta:
        """The idle timeout as a timedelta, floored at one minute.

        The floor matters because the sweeper cannot run more often than its
        Beat interval. A timeout of zero -- or a negative one from a typo in
        .env -- would mark every conversation idle the instant it was
        created, including the one whose reply is still being generated, and
        the customer would be told goodbye before they were answered.
        """
        return timedelta(minutes=max(1, self.conversation_idle_timeout_minutes))

    @property
    def conversation_reopen_window(self) -> timedelta:
        """How long a closed session stays resumable.

        Zero is a meaningful value here, unlike the idle timeout: it means
        "never resume", and every closed session is final. So this floors at
        zero rather than at one minute, and a negative value from a typo is
        read as the same deliberate "off".
        """
        return timedelta(minutes=max(0, self.conversation_reopen_window_minutes))

    @property
    def new_session_after(self) -> timedelta:
        """The age past which a returning customer always starts fresh.

        Never shorter than the reopen window. The two settings can be
        configured to contradict each other -- a 60-minute reopen window with
        a 0-hour new-session bound -- and rather than let the resolution
        depend on which check happens to run first, the wider one is clamped
        to the stricter. Starting a new session is the safe direction to err
        in: the cost is one extra welcome, where the other direction answers
        someone in the context of a conversation they consider finished.
        """
        return max(
            timedelta(hours=max(0, self.new_session_after_hours)),
            self.conversation_reopen_window,
        )


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
