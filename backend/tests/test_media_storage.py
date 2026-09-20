import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi.responses import FileResponse, RedirectResponse

from app.services.media_storage import (
    LocalMediaStorage,
    MediaStorageError,
    RemoteMediaStorage,
    S3MediaStorage,
)
from app.routes.knowledge import _referenced_media_filenames


def s3_config(**overrides):
    values = {
        "S3_BUCKET": "knowledge-media",
        "S3_ENDPOINT_URL": "",
        "S3_REGION": "us-east-1",
        "S3_ACCESS_KEY_ID": "access-key",
        "S3_SECRET_ACCESS_KEY": "secret-key",
        "S3_SESSION_TOKEN": "",
        "S3_KEY_PREFIX": "knowledge-kb/prod/media",
        "S3_ADDRESSING_STYLE": "auto",
        "S3_PUBLIC_BASE_URL": "",
        "S3_PRESIGN_EXPIRES_SECONDS": 900,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def remote_config(**overrides):
    values = {
        "REMOTE_MEDIA_BASE_URL": "https://media.internal.example",
        "REMOTE_MEDIA_API_KEY": "test-internal-key-0123456789abcd",
        "REMOTE_MEDIA_PATH_PREFIX": "/internal/media",
        "REMOTE_MEDIA_TIMEOUT_SECONDS": 60.0,
        "REMOTE_MEDIA_CONNECT_TIMEOUT_SECONDS": 10.0,
        "REMOTE_MEDIA_READ_TIMEOUT_SECONDS": 300.0,
        "REMOTE_MEDIA_VERIFY_TLS": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class LocalMediaStorageTests(unittest.TestCase):
    def test_put_read_and_delete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = LocalMediaStorage(Path(temp_dir))
            storage_key = storage.put("image.png", b"image-bytes", "image/png")

            self.assertEqual(Path(storage_key).read_bytes(), b"image-bytes")
            response = storage.build_response(
                storage_key,
                "image.png",
                "image/png",
            )
            self.assertIsInstance(response, FileResponse)
            self.assertEqual(response.headers["accept-ranges"], "bytes")

            storage.delete(storage_key, "image.png")
            self.assertFalse(Path(storage_key).exists())

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = LocalMediaStorage(Path(temp_dir))
            with self.assertRaises(MediaStorageError):
                storage.put("../outside.png", b"x", "image/png")

    def test_empty_storage_key_deletes_by_filename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = LocalMediaStorage(Path(temp_dir))
            storage_key = storage.put(
                "staged.png",
                b"image-bytes",
                "image/png",
            )

            storage.delete("", "staged.png")

            self.assertFalse(Path(storage_key).exists())

    def test_build_response_supports_single_byte_range(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = LocalMediaStorage(Path(temp_dir))
            storage_key = storage.put(
                "video.mp4",
                b"0123456789",
                "video/mp4",
            )

            response = storage.build_response(
                storage_key,
                "video.mp4",
                "video/mp4",
                request_headers={"Range": "bytes=2-5"},
            )

            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.headers["content-range"], "bytes 2-5/10")
            self.assertEqual(response.headers["accept-ranges"], "bytes")
            self.assertEqual(response.headers["content-length"], "4")

            async def collect_body():
                return b"".join(
                    [chunk async for chunk in response.body_iterator]
                )

            self.assertEqual(asyncio.run(collect_body()), b"2345")

    def test_build_response_rejects_unsatisfied_range(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = LocalMediaStorage(Path(temp_dir))
            storage_key = storage.put(
                "video.mp4",
                b"0123456789",
                "video/mp4",
            )

            response = storage.build_response(
                storage_key,
                "video.mp4",
                "video/mp4",
                request_headers={"Range": "bytes=99-100"},
            )

            self.assertEqual(response.status_code, 416)
            self.assertEqual(response.headers["content-range"], "bytes */10")
            self.assertEqual(response.headers["accept-ranges"], "bytes")

    def test_build_response_supports_open_ended_and_suffix_ranges(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = LocalMediaStorage(Path(temp_dir))
            storage_key = storage.put(
                "video.mp4",
                b"0123456789",
                "video/mp4",
            )

            open_ended = storage.build_response(
                storage_key,
                "video.mp4",
                "video/mp4",
                request_headers={"Range": "bytes=7-"},
            )
            suffix = storage.build_response(
                storage_key,
                "video.mp4",
                "video/mp4",
                request_headers={"Range": "bytes=-3"},
            )

            async def collect(response):
                return b"".join([chunk async for chunk in response.body_iterator])

            self.assertEqual(open_ended.headers["content-range"], "bytes 7-9/10")
            self.assertEqual(asyncio.run(collect(open_ended)), b"789")
            self.assertEqual(suffix.headers["content-range"], "bytes 7-9/10")
            self.assertEqual(asyncio.run(collect(suffix)), b"789")

    def test_build_response_rejects_overlong_numeric_range(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = LocalMediaStorage(Path(temp_dir))
            storage_key = storage.put("video.mp4", b"0123456789", "video/mp4")
            response = storage.build_response(
                storage_key,
                "video.mp4",
                "video/mp4",
                request_headers={"Range": f"bytes={'9' * 5000}-"},
            )

            self.assertEqual(response.status_code, 416)
            self.assertEqual(response.headers["content-range"], "bytes */10")

    def test_referenced_media_normalizes_public_upload_path(self):
        self.assertEqual(
            _referenced_media_filenames(
                {
                    "blocks": [
                        {"type": "image", "media_id": "image.png"},
                        {"type": "video", "media_id": "/uploads/video.mp4"},
                        {"type": "text", "value": "正文"},
                    ]
                }
            ),
            {"image.png", "video.mp4"},
        )


class S3MediaStorageTests(unittest.TestCase):
    @patch("app.services.media_storage.boto3.client")
    def test_put_and_private_read_use_stable_object_key(self, client_factory):
        client = Mock()
        client.generate_presigned_url.return_value = "https://signed.example/object"
        body = Mock()
        body.read.return_value = b"knowledge-kb-storage-health"
        client.get_object.return_value = {"Body": body}
        client_factory.return_value = client
        storage = S3MediaStorage(s3_config())

        storage_key = storage.put("image.png", b"image-bytes", "image/png")
        response = storage.build_response(storage_key, "image.png", "image/png")

        self.assertEqual(storage_key, "knowledge-kb/prod/media/image.png")
        client.put_object.assert_called_once_with(
            Bucket="knowledge-media",
            Key=storage_key,
            Body=b"image-bytes",
            ContentType="image/png",
        )
        client.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={"Bucket": "knowledge-media", "Key": storage_key},
            ExpiresIn=900,
        )
        self.assertIsInstance(response, RedirectResponse)
        self.assertEqual(response.headers["location"], "https://signed.example/object")

        storage.check()
        client.get_object.assert_called_once()
        self.assertGreaterEqual(client.delete_object.call_count, 1)

    @patch("app.services.media_storage.boto3.client")
    def test_public_base_url_avoids_presigning(self, client_factory):
        client = Mock()
        client_factory.return_value = client
        storage = S3MediaStorage(
            s3_config(S3_PUBLIC_BASE_URL="https://cdn.example.com")
        )

        response = storage.build_response(
            "knowledge-kb/prod/media/image one.png",
            "image one.png",
            "image/png",
        )

        self.assertEqual(
            response.headers["location"],
            "https://cdn.example.com/knowledge-kb/prod/media/image%20one.png",
        )
        client.generate_presigned_url.assert_not_called()

    @patch("app.services.media_storage.boto3.client")
    def test_empty_storage_key_deletes_by_derived_object_key(
        self,
        client_factory,
    ):
        client = Mock()
        client_factory.return_value = client
        storage = S3MediaStorage(s3_config())

        storage.delete("", "staged.png")

        client.delete_object.assert_called_once_with(
            Bucket="knowledge-media",
            Key="knowledge-kb/prod/media/staged.png",
        )

    def test_requires_complete_static_credentials(self):
        with self.assertRaises(MediaStorageError):
            S3MediaStorage(s3_config(S3_SECRET_ACCESS_KEY=""))


class RemoteMediaStorageTests(unittest.TestCase):
    def _storage(self, handler):
        storage = RemoteMediaStorage(remote_config())
        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage.__dict__["client"] = client
        self.addCleanup(client.close)
        return storage

    def test_requires_private_endpoint_and_key(self):
        with self.assertRaises(MediaStorageError):
            RemoteMediaStorage(remote_config(REMOTE_MEDIA_BASE_URL=""))
        with self.assertRaises(MediaStorageError):
            RemoteMediaStorage(remote_config(REMOTE_MEDIA_API_KEY=""))
        with self.assertRaises(MediaStorageError):
            RemoteMediaStorage(
                remote_config(REMOTE_MEDIA_API_KEY="short-key")
            )
        with self.assertRaises(MediaStorageError):
            RemoteMediaStorage(
                remote_config(REMOTE_MEDIA_BASE_URL="https://media.example/?x=1")
            )
        with self.assertRaises(MediaStorageError):
            RemoteMediaStorage(
                remote_config(REMOTE_MEDIA_PATH_PREFIX="/internal/../media")
            )

    def test_put_and_delete_use_authenticated_filename_endpoint(self):
        calls = []

        def handler(request):
            calls.append(request)
            if request.method == "PUT":
                self.assertEqual(
                    str(request.url),
                    "https://media.internal.example/internal/media/image%20one.png",
                )
                self.assertEqual(
                    request.headers["x-internal-media-key"],
                    "test-internal-key-0123456789abcd",
                )
                self.assertEqual(request.headers["content-type"], "image/png")
                self.assertEqual(request.content, b"image-bytes")
                return httpx.Response(201, request=request)
            self.assertEqual(request.method, "DELETE")
            self.assertEqual(
                request.url.params.get("storage_key"),
                "/old/server/backend/uploads/image one.png",
            )
            return httpx.Response(204, request=request)

        storage = self._storage(handler)
        self.assertEqual(
            storage.put("image one.png", b"image-bytes", "image/png"),
            "image one.png",
        )
        storage.delete("/old/server/backend/uploads/image one.png", "image one.png")
        self.assertEqual([request.method for request in calls], ["PUT", "DELETE"])

    def test_build_response_forwards_range_and_response_headers(self):
        def handler(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(
                request.url.params.get("storage_key"),
                "legacy/local/path/video.mp4",
            )
            self.assertEqual(request.url.params.get("mime_type"), "video/mp4")
            self.assertEqual(request.headers["range"], "bytes=0-4")
            self.assertEqual(
                request.headers["x-internal-media-key"],
                "test-internal-key-0123456789abcd",
            )
            return httpx.Response(
                206,
                headers={
                    "content-type": "video/mp4",
                    "content-length": "5",
                    "content-range": "bytes 0-4/10",
                    "accept-ranges": "bytes",
                    "etag": '"video-etag"',
                    "x-unrelated": "must-not-leak",
                },
                content=b"12345",
                request=request,
            )

        storage = self._storage(handler)
        response = storage.build_response(
            "legacy/local/path/video.mp4",
            "video.mp4",
            "video/mp4",
            request_headers={"Range": "bytes=0-4", "Authorization": "browser-token"},
        )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 0-4/10")
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertNotIn("x-unrelated", response.headers)
        async def collect_body():
            return b"".join([chunk async for chunk in response.body_iterator])

        self.assertEqual(asyncio.run(collect_body()), b"12345")

    def test_missing_remote_media_maps_to_file_not_found(self):
        def handler(request):
            return httpx.Response(404, request=request)

        storage = self._storage(handler)
        with self.assertRaises(FileNotFoundError):
            storage.build_response("", "missing.png", "image/png")

    def test_delete_rejected_404_is_not_treated_as_success(self):
        def handler(request):
            self.assertEqual(request.method, "DELETE")
            return httpx.Response(404, request=request)

        storage = self._storage(handler)
        with self.assertRaises(MediaStorageError):
            storage.delete("legacy/local/path/missing.png", "missing.png")

    def test_unsatisfied_remote_range_is_forwarded_as_416(self):
        def handler(request):
            self.assertEqual(request.headers["range"], "bytes=99-100")
            return httpx.Response(
                416,
                headers={
                    "content-range": "bytes */10",
                    "accept-ranges": "bytes",
                },
                request=request,
            )

        storage = self._storage(handler)
        response = storage.build_response(
            "video.mp4",
            "video.mp4",
            "video/mp4",
            request_headers={"Range": "bytes=99-100"},
        )

        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["content-range"], "bytes */10")
        self.assertEqual(response.headers["accept-ranges"], "bytes")

    def test_check_round_trips_remote_object_and_deletes_it(self):
        calls = []

        def handler(request):
            calls.append(request.method)
            if request.method == "PUT":
                return httpx.Response(201, request=request)
            if request.method == "GET":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/plain"},
                    content=b"knowledge-kb-storage-health",
                    request=request,
                )
            return httpx.Response(204, request=request)

        storage = self._storage(handler)
        storage.check()
        self.assertEqual(calls, ["PUT", "GET", "DELETE"])

    def test_delete_does_not_swallow_gateway_404(self):
        def handler(request):
            return httpx.Response(404, request=request)

        storage = self._storage(handler)
        with self.assertRaises(MediaStorageError):
            storage.delete("video.mp4", "video.mp4")


if __name__ == "__main__":
    unittest.main()
