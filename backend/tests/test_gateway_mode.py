import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.responses import JSONResponse
from starlette.requests import Request

from app import main
from app.core.config import Settings


def request_for(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


class GatewayConfigurationTests(unittest.TestCase):
    def test_gateway_flags_default_to_normal_application_mode(self):
        config = Settings(_env_file=None)
        self.assertTrue(config.BACKGROUND_WORKERS_ENABLED)
        self.assertFalse(config.MEDIA_GATEWAY_ONLY)

    def test_gateway_mode_forces_workers_off(self):
        with patch.object(main.settings, "BACKGROUND_WORKERS_ENABLED", True), patch.object(
            main.settings, "MEDIA_GATEWAY_ONLY", True
        ):
            self.assertFalse(main._background_workers_should_start())

        with patch.object(main.settings, "BACKGROUND_WORKERS_ENABLED", False), patch.object(
            main.settings, "MEDIA_GATEWAY_ONLY", False
        ):
            self.assertFalse(main._background_workers_should_start())

    def test_gateway_path_allowlist(self):
        self.assertTrue(main._gateway_path_allowed("/health"))
        self.assertTrue(main._gateway_path_allowed("/ready"))
        self.assertTrue(main._gateway_path_allowed("/internal/media/video.mp4"))
        self.assertFalse(main._gateway_path_allowed("/app"))
        self.assertFalse(main._gateway_path_allowed("/api/v1/knowledge"))

        with patch.object(main.settings, "REMOTE_MEDIA_PATH_PREFIX", "/private/media"):
            self.assertTrue(main._gateway_path_allowed("/private/media/video.mp4"))
            self.assertFalse(main._gateway_path_allowed("/internal/media/video.mp4"))

    def test_gateway_middleware_blocks_non_gateway_routes(self):
        async def run():
            call_next = AsyncMock(return_value=JSONResponse({"ok": True}))
            with patch.object(main.settings, "MEDIA_GATEWAY_ONLY", True):
                response = await main.add_security_headers(request_for("/app"), call_next)
            return response, call_next

        response, call_next = asyncio.run(run())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        call_next.assert_not_awaited()

    def test_gateway_middleware_allows_health_routes(self):
        async def run():
            call_next = AsyncMock(return_value=JSONResponse({"ok": True}))
            with patch.object(main.settings, "MEDIA_GATEWAY_ONLY", True):
                response = await main.add_security_headers(request_for("/health"), call_next)
            return response, call_next

        response, call_next = asyncio.run(run())
        self.assertEqual(response.status_code, 200)
        call_next.assert_awaited_once()

    def test_gateway_ready_skips_embedding_probe(self):
        connection = Mock()
        with patch.object(main.settings, "MEDIA_GATEWAY_ONLY", True), patch.object(
            main.engine, "connect"
        ) as connect, patch.object(main.httpx, "get") as http_get:
            connect.return_value.__enter__.return_value = connection
            result = main.ready()

        self.assertEqual(result, {"status": "ready"})
        connection.execute.assert_called_once()
        http_get.assert_not_called()

    def test_gateway_lifespan_does_not_start_workers(self):
        async def run():
            with patch.object(main.settings, "MEDIA_GATEWAY_ONLY", True), patch.object(
                main.settings, "BACKGROUND_WORKERS_ENABLED", True
            ), patch.object(main, "run_media_deletion_worker") as media_worker, patch.object(
                main, "run_knowledge_import_worker"
            ) as import_worker, patch.object(main, "run_knowledge_vector_worker") as vector_worker:
                async with main.lifespan(None):
                    pass
                return media_worker, import_worker, vector_worker

        media_worker, import_worker, vector_worker = asyncio.run(run())
        media_worker.assert_not_called()
        import_worker.assert_not_called()
        vector_worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
