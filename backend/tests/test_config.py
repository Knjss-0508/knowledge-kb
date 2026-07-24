import unittest

from app.core.config import Settings


class DatabaseConfigurationTests(unittest.TestCase):
    def test_database_url_takes_precedence_and_normalizes_legacy_scheme(self):
        config = Settings(
            _env_file=None,
            DATABASE_URL="postgres://cloud_user:secret@db.example.com/knowledge",
        )

        self.assertEqual(
            config.SQLALCHEMY_DATABASE_URL,
            "postgresql://cloud_user:secret@db.example.com/knowledge",
        )

    def test_component_database_settings_escape_credentials(self):
        config = Settings(
            _env_file=None,
            DATABASE_URL="",
            POSTGRES_USER="knowledge user",
            POSTGRES_PASSWORD="p@ss/word",
            POSTGRES_HOST="postgres",
            POSTGRES_PORT=5432,
            POSTGRES_DB="knowledge base",
        )

        self.assertEqual(
            config.SQLALCHEMY_DATABASE_URL,
            "postgresql://knowledge%20user:p%40ss%2Fword@postgres:5432/knowledge%20base",
        )

    def test_active_media_upload_lease_outlasts_ready_staging_ttl(self):
        config = Settings(_env_file=None)

        self.assertGreater(
            config.MEDIA_UPLOAD_ACTIVE_TTL_SECONDS,
            config.MEDIA_UPLOAD_STAGING_TTL_SECONDS,
        )


if __name__ == "__main__":
    unittest.main()
