from __future__ import annotations

import uuid
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import boto3
from botocore.config import Config as BotoConfig
from fastapi.responses import FileResponse, RedirectResponse, Response

from app.core.config import Settings, settings


class MediaStorageError(RuntimeError):
    """Raised when a media object cannot be stored, read, or deleted."""


class MediaStorage(Protocol):
    backend: str

    def put(self, filename: str, content: bytes, mime_type: str) -> str:
        ...

    def delete(self, storage_key: str, filename: str) -> None:
        ...

    def build_response(
        self,
        storage_key: str,
        filename: str,
        mime_type: str,
    ) -> Response:
        ...

    def check(self) -> None:
        ...


def _validate_filename(filename: str) -> str:
    value = filename.strip()
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise MediaStorageError("Invalid media filename.")
    return value


class LocalMediaStorage:
    backend = "local"

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _target(self, storage_key: str, filename: str) -> Path:
        safe_filename = _validate_filename(filename)
        candidate = Path(storage_key) if storage_key else self.root / safe_filename
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise MediaStorageError("Media path is outside the upload directory.")
        return resolved

    def put(self, filename: str, content: bytes, mime_type: str) -> str:
        target = self._target("", filename)
        try:
            target.write_bytes(content)
        except OSError as exc:
            raise MediaStorageError("Failed to save media file.") from exc
        return str(target)

    def delete(self, storage_key: str, filename: str) -> None:
        target = self._target(storage_key, filename)
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            raise MediaStorageError("Failed to delete media file.") from exc

    def build_response(
        self,
        storage_key: str,
        filename: str,
        mime_type: str,
    ) -> Response:
        target = self._target(storage_key, filename)
        if not target.is_file():
            raise FileNotFoundError(filename)
        return FileResponse(target, media_type=mime_type)

    def check(self) -> None:
        filename = f".storage-health-{uuid.uuid4().hex}.txt"
        storage_key = self.put(filename, b"knowledge-kb-storage-health", "text/plain")
        try:
            if self._target(storage_key, filename).read_bytes() != b"knowledge-kb-storage-health":
                raise MediaStorageError("Local media storage returned unexpected data.")
        finally:
            self.delete(storage_key, filename)


class S3MediaStorage:
    backend = "s3"

    def __init__(self, config: Settings):
        self.bucket = config.S3_BUCKET.strip()
        if not self.bucket:
            raise MediaStorageError("S3_BUCKET is required for S3 media storage.")
        self.endpoint_url = config.S3_ENDPOINT_URL.strip() or None
        self.region = config.S3_REGION.strip() or None
        self.access_key_id = config.S3_ACCESS_KEY_ID.strip() or None
        self.secret_access_key = config.S3_SECRET_ACCESS_KEY.strip() or None
        self.session_token = config.S3_SESSION_TOKEN.strip() or None
        self.key_prefix = config.S3_KEY_PREFIX.strip().strip("/")
        self.addressing_style = config.S3_ADDRESSING_STYLE.strip() or "auto"
        self.public_base_url = config.S3_PUBLIC_BASE_URL.strip().rstrip("/")
        self.presign_expires = config.S3_PRESIGN_EXPIRES_SECONDS

        if bool(self.access_key_id) != bool(self.secret_access_key):
            raise MediaStorageError(
                "S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY must be configured together."
            )
        if self.addressing_style not in {"auto", "virtual", "path"}:
            raise MediaStorageError(
                "S3_ADDRESSING_STYLE must be auto, virtual, or path."
            )
        if self.presign_expires <= 0:
            raise MediaStorageError("S3_PRESIGN_EXPIRES_SECONDS must be positive.")

    @cached_property
    def client(self):
        kwargs: dict[str, object] = {
            "config": BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": self.addressing_style},
            )
        }
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        if self.region:
            kwargs["region_name"] = self.region
        if self.access_key_id:
            kwargs["aws_access_key_id"] = self.access_key_id
            kwargs["aws_secret_access_key"] = self.secret_access_key
        if self.session_token:
            kwargs["aws_session_token"] = self.session_token
        return boto3.client("s3", **kwargs)

    def _key_for_filename(self, filename: str) -> str:
        safe_filename = _validate_filename(filename)
        return f"{self.key_prefix}/{safe_filename}" if self.key_prefix else safe_filename

    def _stored_key(self, storage_key: str, filename: str) -> str:
        value = storage_key.strip().lstrip("/")
        if not value or "\\" in value or ":" in value:
            return self._key_for_filename(filename)
        return value

    def put(self, filename: str, content: bytes, mime_type: str) -> str:
        key = self._key_for_filename(filename)
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=content,
                ContentType=mime_type,
            )
        except Exception as exc:
            raise MediaStorageError("Failed to upload media object.") from exc
        return key

    def delete(self, storage_key: str, filename: str) -> None:
        key = self._stored_key(storage_key, filename)
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise MediaStorageError("Failed to delete media object.") from exc

    def build_response(
        self,
        storage_key: str,
        filename: str,
        mime_type: str,
    ) -> Response:
        key = self._stored_key(storage_key, filename)
        try:
            if self.public_base_url:
                url = f"{self.public_base_url}/{quote(key, safe='/')}"
            else:
                url = self.client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self.bucket, "Key": key},
                    ExpiresIn=self.presign_expires,
                )
        except Exception as exc:
            raise MediaStorageError("Failed to create media access URL.") from exc
        return RedirectResponse(url=url, status_code=307)

    def check(self) -> None:
        filename = f".storage-health-{uuid.uuid4().hex}.txt"
        key = self._key_for_filename(filename)
        payload = b"knowledge-kb-storage-health"
        uploaded = False
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=payload,
                ContentType="text/plain",
            )
            uploaded = True
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            if response["Body"].read() != payload:
                raise MediaStorageError("S3 media storage returned unexpected data.")
        except MediaStorageError:
            raise
        except Exception as exc:
            raise MediaStorageError(
                "S3 media storage read/write validation failed."
            ) from exc
        finally:
            if uploaded:
                try:
                    self.client.delete_object(Bucket=self.bucket, Key=key)
                except Exception as exc:
                    raise MediaStorageError(
                        "S3 media storage delete validation failed."
                    ) from exc


@lru_cache(maxsize=1)
def get_media_storage() -> MediaStorage:
    backend = settings.MEDIA_STORAGE_BACKEND.strip().lower()
    if backend == "local":
        root = (
            Path(settings.UPLOAD_DIR)
            if settings.UPLOAD_DIR
            else Path(__file__).resolve().parents[2] / "uploads"
        )
        return LocalMediaStorage(root)
    if backend == "s3":
        return S3MediaStorage(settings)
    raise MediaStorageError("MEDIA_STORAGE_BACKEND must be local or s3.")
