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


class WorkOrderLogConfigurationTests(unittest.TestCase):
    """盲标工单号判定的三个默认值：轮询、实时窗口、单轮退池上限。"""

    def test_poll_interval_runs_inside_the_five_minute_gate_window(self):
        config = Settings(_env_file=None)

        # 建单到 worker 回写之间最多只隔 3~5 秒，300 秒的轮询让新号码必然「未知」。
        self.assertEqual(config.WORK_ORDER_LOG_POLL_SECONDS, 60.0)

    def test_tail_window_covers_the_write_rate_of_the_access_log(self):
        config = Settings(_env_file=None)

        # 实测 zzdy 访问日志平均 2.6 KB/s：8 MiB 约 53 分钟流量，1 MiB 约 6.5 分钟。
        self.assertEqual(config.WORK_ORDER_LOG_TAIL_BYTES, 8 * 1024 * 1024)

    def test_retire_limit_clears_the_known_backlog_in_one_pass(self):
        config = Settings(_env_file=None)

        # 2026-10-14 池内 285 张会话号工单，单轮上限必须一次覆盖。
        self.assertGreaterEqual(config.WORK_ORDER_LOG_RETIRE_LIMIT, 285)


if __name__ == "__main__":
    unittest.main()
