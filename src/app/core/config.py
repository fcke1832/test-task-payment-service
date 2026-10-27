from pathlib import Path
from urllib.parse import quote_plus

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR: Path = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Настройки приложения, читаются из переменных окружения и `.env`."""

    # PostgreSQL
    POSTGRES_USER: str = "app_user"
    POSTGRES_PASSWORD: str = "app_password"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "payment_db"
    POSTGRES_ECHO: bool = False

    # RabbitMQ
    RABBITMQ_URL: str = "amqp://guest:guest@localhost:5672/"

    # API
    API_KEY: str = Field(default="test-api-key-123", min_length=1)
    APP_ENV: str = "development"

    # Outbox relay
    OUTBOX_POLL_INTERVAL: float = Field(default=1.0, gt=0)
    OUTBOX_BATCH_SIZE: int = Field(default=100, ge=1, le=1000)
    OUTBOX_MAX_BACKOFF_SECONDS: float = Field(default=60.0, gt=0)

    # Consumer
    MAX_PROCESSING_ATTEMPTS: int = Field(default=3, ge=1, le=10)
    RETRY_BASE_DELAY_SECONDS: float = Field(default=1.0, gt=0)
    PAYMENT_SUCCESS_RATE: float = Field(default=0.9, ge=0, le=1)
    PROCESSING_MIN_SECONDS: float = Field(default=2.0, ge=0)
    PROCESSING_MAX_SECONDS: float = Field(default=5.0, ge=0)
    WEBHOOK_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0)

    @property
    def database_url(self) -> str:
        """DSN для SQLAlchemy/asyncpg с корректным URL-кодированием пароля."""
        user = quote_plus(self.POSTGRES_USER)
        password = quote_plus(self.POSTGRES_PASSWORD)
        return (
            f"postgresql+asyncpg://{user}:{password}@"
            f"{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @model_validator(mode="after")
    def validate_processing_window(self) -> Settings:
        if self.PROCESSING_MAX_SECONDS < self.PROCESSING_MIN_SECONDS:
            msg = "PROCESSING_MAX_SECONDS must be >= PROCESSING_MIN_SECONDS"
            raise ValueError(msg)
        return self

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


settings = Settings()
