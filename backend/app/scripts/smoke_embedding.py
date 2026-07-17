from app.core.config import settings
from app.main import ready
from app.services.embedding import embed_texts


def main() -> None:
    vectors = embed_texts(["部署健康检查"])
    if len(vectors) != 1 or len(vectors[0]) != settings.EMBEDDING_DIMENSIONS:
        raise RuntimeError(
            "Embedding smoke test failed: expected one vector with "
            f"{settings.EMBEDDING_DIMENSIONS} dimensions."
        )
    ready()
    print(f"embedding smoke test passed: {len(vectors[0])} dimensions")


if __name__ == "__main__":
    main()
