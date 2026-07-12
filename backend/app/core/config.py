from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PROJECT_NAME: str = "答疑中台知识库"
    VERSION: str = "0.1.0"
    API_V1_PREFIX: str = "/api/v1"

    # PostgreSQL
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "knowledge_admin"
    POSTGRES_PASSWORD: str = "knowledge_pass_2026"
    POSTGRES_DB: str = "knowledge_base"

    @property
    def DATABASE_URL(self) -> str:
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    # Redis
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0

    @property
    def REDIS_URL(self) -> str:
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    # Manhattan backend API. Put the logged-in browser Cookie in .env as NMHT_COOKIE.
    NMHT_BASE_URL: str = "https://nmht.zhuanspirit.com"
    NMHT_COOKIE: str = ""

    # Service-to-service authentication for automation ingestion endpoints.
    INTEGRATION_API_KEY: str = ""

    # OpenAI-compatible embedding service, normally the internal Qwen3 service.
    EMBEDDING_PROVIDER: str = "openai_compatible"
    EMBEDDING_BASE_URL: str = "http://embedding-qwen:80/v1"
    EMBEDDING_MODEL: str = "Qwen/Qwen3-Embedding-0.6B"
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_TIMEOUT_SECONDS: float = 30.0
    EMBEDDING_DIMENSIONS: int = 1024
    EMBEDDING_HEALTHCHECK_URL: str = ""

    # Knowledge deduplication thresholds. Scores are cosine similarities.
    # >= block threshold: reject as a likely duplicate.
    # >= review threshold: create a review item with duplicate evidence attached.
    DEDUP_BLOCK_THRESHOLD: float = 0.96
    DEDUP_REVIEW_THRESHOLD: float = 0.88
    DEDUP_MAX_CANDIDATES: int = 10
    DEDUP_MIN_SEMANTIC_CONTENT_CHARS: int = 8
    DEDUP_MIN_CONTAINMENT_CONTENT_CHARS: int = 12

    # Retrieval chunks are measured in characters because the source knowledge
    # is predominantly Chinese. Overlap preserves context across chunk borders.
    SEARCH_CHUNK_SIZE: int = 800
    SEARCH_CHUNK_OVERLAP: int = 120

    # Empty means <backend>/uploads. Containers override this with /app/uploads.
    UPLOAD_DIR: str = ""
    UPLOAD_MAX_BYTES: int = 20 * 1024 * 1024
    SESSION_TTL_HOURS: int = 24

    class Config:
        env_file = ".env"


settings = Settings()
