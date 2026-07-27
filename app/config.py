"""Application configuration loaded from environment variables (12-factor)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central, environment-based application settings."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
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

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    system_prompt: str = "You are a helpful WhatsApp assistant for our business."
    max_output_tokens: int = 512
    max_context_messages: int = 20
    max_context_tokens: int = 6000

    # WhatsApp Cloud API
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = "change-me"
    whatsapp_app_secret: str = ""
    whatsapp_api_version: str = "v21.0"

    # Admin API
    admin_api_key: str = "change-me"

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
