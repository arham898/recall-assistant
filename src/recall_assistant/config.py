"""Application settings, loaded from environment variables / .env file."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Postgres
    postgres_user: str = "recall"
    postgres_password: str = "recall"
    postgres_db: str = "recall_assistant"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # Ingest filters
    recall_type_filter: str = "V"
    min_model_year: int = 2018

    # Embeddings
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_batch_size: int = 256
    chunk_max_tokens: int = 400
    chunk_overlap_tokens: int = 50

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
