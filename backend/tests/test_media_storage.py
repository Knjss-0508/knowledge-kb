import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.responses import FileResponse, RedirectResponse

from app.services.media_storage import (
    LocalMediaStorage,
    MediaStorageError,
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


if __name__ == "__main__":
    unittest.main()
