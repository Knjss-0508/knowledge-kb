from __future__ import annotations

import time
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.models.user import User
from app.routes.auth import hash_password, verify_password


EXPECTED_EMBEDDING_DIMENSIONS = 1024
LOCAL_DEFAULT_ADMIN_USERNAME = "Weichizhuo"
LOCAL_DEFAULT_ADMIN_PASSWORD = "123456"


def _alembic_config() -> AlembicConfig:
    path = Path(__file__).resolve().parents[2] / "alembic.ini"
    return AlembicConfig(str(path))


def run_migrations() -> None:
    attempts = max(settings.DB_CONNECT_RETRIES, 1)
    delay = max(settings.DB_CONNECT_RETRY_SECONDS, 0.1)
    for attempt in range(1, attempts + 1):
        try:
            command.upgrade(_alembic_config(), "head")
            return
        except OperationalError:
            engine.dispose()
            if attempt >= attempts:
                raise
            print(
                f"Database is not ready; retrying migration "
                f"({attempt}/{attempts})..."
            )
            time.sleep(delay)


def bootstrap_admin() -> None:
    username = settings.INITIAL_ADMIN_USERNAME.strip()
    password = settings.INITIAL_ADMIN_PASSWORD
    using_insecure_local_default = False
    with SessionLocal() as db:
        active_admin_exists = (
            db.query(User.id)
            .filter(User.role == "super_admin", User.is_active.is_(True))
            .first()
            is not None
        )
        if not username and not password:
            if active_admin_exists:
                print("Active administrator already exists; bootstrap skipped.")
                return
            if settings.ALLOW_INSECURE_DEFAULT_ADMIN:
                username = LOCAL_DEFAULT_ADMIN_USERNAME
                password = LOCAL_DEFAULT_ADMIN_PASSWORD
                using_insecure_local_default = True
                print(
                    "WARNING: creating the local-only insecure default "
                    "administrator."
                )
            else:
                raise RuntimeError(
                    "No active administrator exists. Set INITIAL_ADMIN_USERNAME "
                    "and INITIAL_ADMIN_PASSWORD for the first deployment."
                )
    if not username or not password:
        raise RuntimeError(
            "INITIAL_ADMIN_USERNAME and INITIAL_ADMIN_PASSWORD must be set together."
        )
    if len(username) > 64:
        raise RuntimeError("INITIAL_ADMIN_USERNAME must be at most 64 characters.")
    if len(password) < 12 and not using_insecure_local_default:
        raise RuntimeError("INITIAL_ADMIN_PASSWORD must contain at least 12 characters.")

    with SessionLocal() as db:
        user = db.query(User).filter(User.username == username).first()
        if not user:
            user = User(
                id=(
                    "super-admin"
                    if using_insecure_local_default
                    else str(uuid.uuid4())
                ),
                username=username,
                password_hash=hash_password(password),
                role="super_admin",
                is_active=True,
            )
            db.add(user)
            print(f"Created initial administrator: {username}")
        elif settings.INITIAL_ADMIN_FORCE_RESET:
            user.role = "super_admin"
            user.is_active = True
            user.password_hash = hash_password(password)
            print(f"Reset administrator credentials: {username}")
        else:
            if user.role != "super_admin" or not user.is_active:
                raise RuntimeError(
                    "The configured initial administrator exists but is not an "
                    "active super administrator. Set INITIAL_ADMIN_FORCE_RESET=true "
                    "to change it explicitly."
                )
            print(f"Administrator already exists; credentials unchanged: {username}")

        legacy = (
            db.query(User)
            .filter(User.id == "super-admin", User.username == "Weichizhuo")
            .first()
        )
        if (
            legacy
            and legacy.username != username
            and verify_password("123456", legacy.password_hash)
        ):
            legacy.is_active = False
            print("Disabled the legacy default administrator.")
        db.commit()


def validate_schema() -> None:
    expected_vector_type = f"vector({EXPECTED_EMBEDDING_DIMENSIONS})"
    required_tables = {
        "alembic_version",
        "categories",
        "integration_ingestions",
        "knowledge_change_logs",
        "knowledge_deduplication_feedback",
        "knowledge_embeddings",
        "knowledge_items",
        "knowledge_media",
        "knowledge_search_embeddings",
        "knowledge_tags",
        "media_deletion_tasks",
        "media_upload_staging",
        "retrieval_quality_events",
        "tag_dimensions",
        "tag_values",
        "usage_stats",
        "user_sessions",
        "users",
    }
    required_constraints = {
        "alembic_version_pkc",
        "categories_pkey",
        "categories_parent_id_fkey",
        "ck_media_upload_staging_status",
        "integration_ingestions_pkey",
        "knowledge_change_logs_pkey",
        "knowledge_change_logs_knowledge_id_fkey",
        "knowledge_deduplication_feedback_pkey",
        "knowledge_deduplication_feedback_knowledge_id_fkey",
        "knowledge_deduplication_feedback_matched_knowledge_id_fkey",
        "knowledge_embeddings_pkey",
        "knowledge_embeddings_knowledge_id_fkey",
        "knowledge_items_pkey",
        "knowledge_items_category_id_fkey",
        "knowledge_media_pkey",
        "knowledge_media_knowledge_id_fkey",
        "knowledge_search_embeddings_pkey",
        "knowledge_search_embeddings_knowledge_id_fkey",
        "knowledge_tags_pkey",
        "knowledge_tags_knowledge_id_fkey",
        "knowledge_tags_tag_value_id_fkey",
        "media_deletion_tasks_pkey",
        "media_upload_staging_pkey",
        "retrieval_quality_events_pkey",
        "tag_dimensions_name_key",
        "tag_dimensions_pkey",
        "tag_values_pkey",
        "tag_values_dimension_id_fkey",
        "uq_category_name_parent",
        "uq_dedup_feedback_submitter",
        "uq_knowledge_embedding_model",
        "uq_knowledge_media_filename",
        "uq_knowledge_search_embedding",
        "uq_knowledge_tag",
        "uq_media_upload_staging_filename",
        "uq_tag_value_per_dim",
        "usage_stats_knowledge_id_key",
        "usage_stats_pkey",
        "usage_stats_knowledge_id_fkey",
        "user_sessions_pkey",
        "user_sessions_user_id_fkey",
        "users_pkey",
    }
    required_indexes = {
        "ix_knowledge_embeddings_vector_hnsw",
        "ix_knowledge_search_embeddings_vector_hnsw",
        "ix_knowledge_items_applicable_categories_gin",
        "ix_knowledge_items_applicable_brands_gin",
        "ix_knowledge_items_applicable_models_gin",
        "ix_media_deletion_tasks_next_attempt_at",
        "ix_media_deletion_tasks_storage_backend",
        "ix_media_upload_staging_expires_at",
        "ix_media_upload_staging_username",
    }
    required_categories = {
        "cat-qc-standard",
        "cat-qc-process",
        "cat-case-analysis",
        "cat-extra-knowledge",
    }
    vector_columns = {
        ("knowledge_embeddings", "embedding_vector"),
        ("knowledge_embeddings", "title_embedding_vector"),
        ("knowledge_embeddings", "content_embedding_vector"),
        ("knowledge_search_embeddings", "embedding_vector"),
    }
    config = _alembic_config()
    expected_revision = ScriptDirectory.from_config(config).get_current_head()

    with engine.connect() as connection:
        extension = connection.execute(
            text(
                """
                SELECT extension.extversion, namespace.nspname
                FROM pg_extension AS extension
                JOIN pg_namespace AS namespace
                  ON namespace.oid = extension.extnamespace
                WHERE extension.extname = 'vector'
                """
            )
        ).one_or_none()
        if not extension:
            raise RuntimeError("The PostgreSQL vector extension is not available.")
        extension_version, extension_schema = extension
        if extension_schema != "public":
            raise RuntimeError(
                "The vector extension must be installed in the public schema."
            )

        current_revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
        if current_revision != expected_revision:
            raise RuntimeError(
                f"Database revision mismatch: expected {expected_revision}, "
                f"got {current_revision or 'none'}."
            )

        actual_tables = set(
            connection.execute(
                text(
                    """
                    SELECT tablename
                    FROM pg_tables
                    WHERE schemaname = 'public'
                    """
                )
            ).scalars()
        )
        missing_tables = sorted(required_tables - actual_tables)
        if missing_tables:
            raise RuntimeError(
                "Required database tables are missing: " + ", ".join(missing_tables)
            )

        actual_constraints = set(
            connection.execute(
                text(
                    """
                    SELECT constraint_name
                    FROM information_schema.table_constraints
                    WHERE constraint_schema = 'public'
                    """
                )
            ).scalars()
        )
        missing_constraints = sorted(required_constraints - actual_constraints)
        if missing_constraints:
            raise RuntimeError(
                "Required database constraints are missing: "
                + ", ".join(missing_constraints)
            )

        actual_indexes = set(
            connection.execute(
                text(
                    """
                    SELECT indexname
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                    """
                )
            ).scalars()
        )
        missing_indexes = sorted(required_indexes - actual_indexes)
        if missing_indexes:
            raise RuntimeError(
                "Required database indexes are missing: "
                + ", ".join(missing_indexes)
            )

        actual_categories = set(
            connection.execute(text("SELECT id FROM categories")).scalars()
        )
        missing_categories = sorted(required_categories - actual_categories)
        if missing_categories:
            raise RuntimeError(
                "Required knowledge categories are missing: "
                + ", ".join(missing_categories)
            )

        rows = connection.execute(
            text(
                """
                SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod)
                FROM pg_attribute AS a
                JOIN pg_class AS c ON c.oid = a.attrelid
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND (c.relname, a.attname) IN (
                    ('knowledge_embeddings', 'embedding_vector'),
                    ('knowledge_embeddings', 'title_embedding_vector'),
                    ('knowledge_embeddings', 'content_embedding_vector'),
                    ('knowledge_search_embeddings', 'embedding_vector')
                  )
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                """
            )
        ).all()
        actual = {(table, column): data_type for table, column, data_type in rows}
        missing_or_invalid = [
            f"{table}.{column}={actual.get((table, column), 'missing')}"
            for table, column in sorted(vector_columns)
            if actual.get((table, column)) != expected_vector_type
        ]
        if missing_or_invalid:
            raise RuntimeError(
                "Invalid vector schema: " + ", ".join(missing_or_invalid)
            )

    print(
        f"Database schema is ready: revision={expected_revision}, "
        f"vector={extension_version}, dimensions={EXPECTED_EMBEDDING_DIMENSIONS}"
    )


def main() -> None:
    if settings.EMBEDDING_DIMENSIONS != EXPECTED_EMBEDDING_DIMENSIONS:
        raise RuntimeError(
            "This deployment requires EMBEDDING_DIMENSIONS="
            f"{EXPECTED_EMBEDDING_DIMENSIONS}; got "
            f"{settings.EMBEDDING_DIMENSIONS}."
        )
    run_migrations()
    validate_schema()
    bootstrap_admin()


if __name__ == "__main__":
    main()
