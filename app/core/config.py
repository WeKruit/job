from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # App
    app_name: str = "Job Service"
    app_env: str = "development"
    debug: bool = False

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5434/job_db"
    # None: auto-detect (sqlite => True, others => False). Set explicitly to override.
    database_auto_create: bool | None = None

    # Blob storage
    storage_provider: str = "none"
    supabase_storage_base_url: str | None = None
    supabase_storage_bucket: str | None = None
    supabase_storage_service_key: str | None = None
    storage_timeout_seconds: float = 20.0

    # Embeddings
    gemini_api_key: str | None = None
    embedding_provider: str = "gemini"  # openai, gemini, ollama, openrouter, deepseek
    embedding_api_key: str | None = None  # defaults to gemini_api_key if not set
    embedding_api_base: str | None = None
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 768
    embedding_batch_size: int = 32
    embedding_refresh_enabled: bool = True
    embedding_refresh_batch_size: int = 32

    # LLM (for JD parsing)
    llm_provider: str = "gemini"  # openai, anthropic, gemini, deepseek, ollama, openrouter
    llm_model: str = "gemini-2.5-flash-lite"
    llm_api_key: str | None = None  # defaults to gemini_api_key if not set
    llm_api_base: str | None = None

    # Firestore
    firestore_credentials_file: str | None = None  # path to service account JSON
    firestore_project_id: str | None = None  # auto-detected from credentials if omitted

    # Safety guards
    read_only_mode: bool = True  # Blocks all DB writes via API; set to false explicitly to allow
    ingest_max_sources: int = 5  # Hard cap on sources per ingest run to prevent accidental bulk loads

    # GeoNames
    geonames_data_dir: str = "data/geonames"
    geonames_username: str | None = None

    @field_validator("debug", mode="before")
    @classmethod
    def normalize_debug_flag(cls, value: object) -> object:
        """Accept common non-boolean environment values like 'release'."""
        if isinstance(value, str) and value.strip().lower() in {"release", "prod", "production"}:
            return False
        return value

    @field_validator("embedding_refresh_batch_size")
    @classmethod
    def validate_embedding_refresh_batch_size(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("embedding_refresh_batch_size must be > 0")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
