from urllib.parse import quote

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
    DATABASE_URL: str = ""
    DB_CONNECT_RETRIES: int = 60
    DB_CONNECT_RETRY_SECONDS: float = 2.0
    DB_CONNECT_TIMEOUT_SECONDS: int = 10
    DB_POOL_RECYCLE_SECONDS: int = 1800

    @property
    def SQLALCHEMY_DATABASE_URL(self) -> str:
        configured_url = self.DATABASE_URL.strip()
        if configured_url:
            if configured_url.startswith("postgres://"):
                return "postgresql://" + configured_url[len("postgres://"):]
            return configured_url
        return (
            f"postgresql://{quote(self.POSTGRES_USER, safe='')}:{quote(self.POSTGRES_PASSWORD, safe='')}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{quote(self.POSTGRES_DB, safe='')}"
        )

    # Optional first-run administrator for a new cloud database.
    INITIAL_ADMIN_USERNAME: str = ""
    INITIAL_ADMIN_PASSWORD: str = ""
    INITIAL_ADMIN_FORCE_RESET: bool = False
    ALLOW_INSECURE_DEFAULT_ADMIN: bool = False

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
    MEDIA_STORAGE_BACKEND: str = "local"
    S3_BUCKET: str = ""
    S3_ENDPOINT_URL: str = ""
    S3_REGION: str = "us-east-1"
    S3_ACCESS_KEY_ID: str = ""
    S3_SECRET_ACCESS_KEY: str = ""
    S3_SESSION_TOKEN: str = ""
    S3_KEY_PREFIX: str = "knowledge-kb/media"
    S3_ADDRESSING_STYLE: str = "auto"
    S3_PUBLIC_BASE_URL: str = ""
    S3_PRESIGN_EXPIRES_SECONDS: int = 900
    MEDIA_UPLOAD_ACTIVE_TTL_SECONDS: int = 3600
    MEDIA_UPLOAD_STAGING_TTL_SECONDS: int = 900
    MEDIA_DELETION_POLL_SECONDS: float = 15.0
    MEDIA_DELETION_BATCH_SIZE: int = 50
    MEDIA_DELETION_RETRY_BASE_SECONDS: int = 5
    MEDIA_DELETION_RETRY_MAX_SECONDS: int = 3600
    SESSION_TTL_HOURS: int = 24

    class Config:
        env_file = ".env"


settings = Settings()
