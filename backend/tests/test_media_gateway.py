import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.routes import media as media_routes
from app.services.media_storage import LocalMediaStorage


INTERNAL_MEDIA_KEY = "test-internal-media-key-0123456789abcd"


def _empty_db():
    """Return a DB double whose media/staging lookups both miss."""
    query = Mock()
    query.filter.return_value.first.return_value = None
    db = Mock()
    db.query.return_value = query
    return db


class InternalMediaGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = LocalMediaStorage(Path(self.temp_dir.name))
        self.db = _empty_db()

        app = FastAPI()
        app.include_router(media_routes.router)

        def override_get_db():
            yield self.db

        app.dependency_overrides[get_db] = override_get_db
        self.app = app
        self.client = TestClient(app)
        self.storage_patch = patch.object(media_routes, "media_storage", self.storage)
        self.key_patch = patch.object(
            media_routes.settings,
            "REMOTE_MEDIA_API_KEY",
            INTERNAL_MEDIA_KEY,
        )
        self.storage_patch.start()
        self.key_patch.start()

    def tearDown(self):
        self.key_patch.stop()
        self.storage_patch.stop()
        self.client.close()
        self.temp_dir.cleanup()

    def _headers(self, **extra):
        return {"X-Internal-Media-Key": INTERNAL_MEDIA_KEY, **extra}

    def test_missing_or_wrong_key_returns_404_for_all_gateway_operations(self):
        for headers in ({}, {"X-Internal-Media-Key": "wrong-key"}):
            put = self.client.put(
                "/internal/media/no-key.txt",
                headers={**headers, "Content-Type": "text/plain"},
                content=b"secret",
            )
            get = self.client.get("/internal/media/no-key.txt", headers=headers)
            delete = self.client.delete("/internal/media/no-key.txt", headers=headers)

            self.assertEqual(put.status_code, 404)
            self.assertEqual(get.status_code, 404)
            self.assertEqual(delete.status_code, 404)

    def test_put_get_range_and_delete_use_local_media_storage(self):
        payload = b"0123456789"
        put = self.client.put(
            "/internal/media/video.mp4",
            headers=self._headers(**{"Content-Type": "video/mp4"}),
            content=payload,
        )
        self.assertEqual(put.status_code, 201)
        put_body = put.json()
        self.assertEqual(put_body["filename"], "video.mp4")
        storage_key = put_body["storage_key"]

        full = self.client.get(
            "/internal/media/video.mp4",
            params={"storage_key": storage_key, "mime_type": "video/mp4"},
            headers=self._headers(),
        )
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.content, payload)
        self.assertEqual(full.headers["content-type"], "video/mp4")

        ranged = self.client.get(
            "/internal/media/video.mp4",
            params={"storage_key": storage_key, "mime_type": "video/mp4"},
            headers=self._headers(Range="bytes=2-5"),
        )
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.content, b"2345")
        self.assertEqual(ranged.headers["content-range"], "bytes 2-5/10")
        self.assertEqual(ranged.headers["accept-ranges"], "bytes")
        self.assertEqual(ranged.headers["content-length"], "4")

        deleted = self.client.delete(
            "/internal/media/video.mp4",
            params={"storage_key": storage_key},
            headers=self._headers(),
        )
        self.assertEqual(deleted.status_code, 204)

        missing = self.client.get(
            "/internal/media/video.mp4",
            params={"storage_key": storage_key, "mime_type": "video/mp4"},
            headers=self._headers(),
        )
        self.assertEqual(missing.status_code, 404)

    def test_invalid_range_returns_416_with_unsatisfied_content_range(self):
        storage_key = self.storage.put("clip.mp4", b"0123456789", "video/mp4")

        response = self.client.get(
            "/internal/media/clip.mp4",
            params={"storage_key": storage_key, "mime_type": "video/mp4"},
            headers=self._headers(Range="bytes=99-100"),
        )

        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["content-range"], "bytes */10")

    def test_path_separator_in_filename_is_rejected(self):
        response = self.client.put(
            "/internal/media/%5Coutside.txt",
            headers=self._headers(**{"Content-Type": "text/plain"}),
            content=b"should-not-be-written",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse((Path(self.temp_dir.name) / "outside.txt").exists())

    def test_upload_body_is_limited_before_writing(self):
        with patch.object(media_routes.settings, "UPLOAD_MAX_BYTES", 4):
            response = self.client.put(
                "/internal/media/too-large.txt",
                headers=self._headers(**{"Content-Type": "text/plain"}),
                content=b"12345",
            )

        self.assertEqual(response.status_code, 413)
        self.assertFalse((Path(self.temp_dir.name) / "too-large.txt").exists())

    def test_non_local_storage_backend_is_rejected_by_gateway(self):
        with patch.object(media_routes.media_storage, "backend", "s3"):
            for method, path, kwargs in (
                (self.client.put, "/internal/media/misconfigured.txt", {"content": b"x"}),
                (self.client.get, "/internal/media/misconfigured.txt", {}),
                (self.client.delete, "/internal/media/misconfigured.txt", {}),
            ):
                response = method(path, headers=self._headers(), **kwargs)
                self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
