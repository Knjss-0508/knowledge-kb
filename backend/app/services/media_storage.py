from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from functools import cached_property, lru_cache
from pathlib import Path
import re
from typing import Protocol
from urllib.parse import quote, urlencode, urlsplit

import boto3
from botocore.config import Config as BotoConfig
import httpx
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse

from app.core.config import Settings, settings


class MediaStorageError(RuntimeError):
    """Raised when a media object cannot be stored, read, or deleted."""


MIN_INTERNAL_MEDIA_KEY_LENGTH = 24


def _normalize_remote_media_path_prefix(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or "?" in raw or "#" in raw or "\x00" in raw:
        raise MediaStorageError(
            "REMOTE_MEDIA_PATH_PREFIX must be a non-empty URL path without query or fragment."
        )
    parts = raw.strip("/").split("/")
    if not parts or any(not part or part in {".", ".."} for part in parts):
        raise MediaStorageError("REMOTE_MEDIA_PATH_PREFIX contains an invalid path segment.")
    return "/" + "/".join(parts)


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
        request_headers: Mapping[str, str] | None = None,
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
        request_headers: Mapping[str, str] | None = None,
    ) -> Response:
        target = self._target(storage_key, filename)
        if not target.is_file():
            raise FileNotFoundError(filename)
        range_value = ""
        for name, value in (request_headers or {}).items():
            if str(name).lower() == "range":
                range_value = str(value or "").strip()
                break
        if range_value:
            return self._build_range_response(target, mime_type, range_value)
        # Advertise byte-range support even for an initial full response.  This
        # lets browsers safely switch to a Range request when a video is
        # seeked or resumed after the first bytes have loaded.
        return FileResponse(
            target,
            media_type=mime_type,
            headers={"Accept-Ranges": "bytes"},
        )

    @staticmethod
    def _build_range_response(target: Path, mime_type: str, range_value: str) -> Response:
        size = target.stat().st_size
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_value)
        if not match or size <= 0:
            return Response(
                status_code=416,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes */{size}",
                },
            )
        start_text, end_text = match.groups()
        if not start_text and not end_text:
            return Response(
                status_code=416,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes */{size}",
                },
            )
        try:
            if start_text:
                start = int(start_text)
                if start >= size:
                    return Response(
                        status_code=416,
                        headers={
                            "Accept-Ranges": "bytes",
                            "Content-Range": f"bytes */{size}",
                        },
                    )
            else:
                suffix_length = int(end_text)
                if suffix_length <= 0:
                    return Response(
                        status_code=416,
                        headers={
                            "Accept-Ranges": "bytes",
                            "Content-Range": f"bytes */{size}",
                        },
                    )
                start = max(0, size - suffix_length)
            if start_text:
                end = min(int(end_text), size - 1) if end_text else size - 1
            else:
                # For a suffix range (bytes=-N), the end is always the final
                # byte; end_text is the requested length, not an offset.
                end = size - 1
        except ValueError:
            return Response(
                status_code=416,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes */{size}",
                },
            )
        if end < start:
            return Response(
                status_code=416,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes */{size}",
                },
            )
        length = end - start + 1

        def body() -> Iterator[bytes]:
            with target.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            body(),
            status_code=206,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(length),
                "Content-Type": mime_type,
            },
        )

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
        request_headers: Mapping[str, str] | None = None,
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


class RemoteMediaStorage:
    """Proxy media operations to the server that owns the upload directory.

    The remote endpoint is private and authenticated.  ``put`` and ``delete``
    use the same filename that is exposed by the knowledge API, while
    ``build_response`` keeps the upstream response stream open so a caller can
    forward 200/206 responses and the Range-related headers to the browser.
    """

    backend = "remote"
    _forward_request_headers = frozenset(
        {
            "accept",
            "if-modified-since",
            "if-none-match",
            "if-range",
            "range",
        }
    )
    _forward_response_headers = frozenset(
        {
            "accept-ranges",
            "cache-control",
            "content-disposition",
            "content-length",
            "content-range",
            "content-type",
            "etag",
            "expires",
            "last-modified",
        }
    )

    def __init__(self, config: Settings):
        self.base_url = config.REMOTE_MEDIA_BASE_URL.strip().rstrip("/")
        if not self.base_url:
            raise MediaStorageError("REMOTE_MEDIA_BASE_URL is required for remote media storage.")
        parsed_base_url = urlsplit(self.base_url)
        if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.netloc:
            raise MediaStorageError("REMOTE_MEDIA_BASE_URL must use http:// or https://.")
        if (
            parsed_base_url.username
            or parsed_base_url.password
            or parsed_base_url.query
            or parsed_base_url.fragment
        ):
            raise MediaStorageError(
                "REMOTE_MEDIA_BASE_URL must not contain credentials, query, or fragment."
            )
        try:
            parsed_base_url.port
        except ValueError as exc:
            raise MediaStorageError("REMOTE_MEDIA_BASE_URL contains an invalid port.") from exc
        self.api_key = config.REMOTE_MEDIA_API_KEY.strip()
        if len(self.api_key) < MIN_INTERNAL_MEDIA_KEY_LENGTH:
            raise MediaStorageError(
                "REMOTE_MEDIA_API_KEY must contain at least 24 characters."
            )
        self.path_prefix = _normalize_remote_media_path_prefix(
            config.REMOTE_MEDIA_PATH_PREFIX
        )
        self.timeout_seconds = float(config.REMOTE_MEDIA_TIMEOUT_SECONDS)
        self.connect_timeout_seconds = float(config.REMOTE_MEDIA_CONNECT_TIMEOUT_SECONDS)
        self.read_timeout_seconds = float(config.REMOTE_MEDIA_READ_TIMEOUT_SECONDS)
        if self.timeout_seconds <= 0 or self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise MediaStorageError("Remote media timeouts must be positive.")
        self.verify_tls = bool(config.REMOTE_MEDIA_VERIFY_TLS)

    @cached_property
    def client(self) -> httpx.Client:
        timeout = httpx.Timeout(
            self.timeout_seconds,
            connect=self.connect_timeout_seconds,
            read=self.read_timeout_seconds,
        )
        return httpx.Client(
            timeout=timeout,
            verify=self.verify_tls,
            follow_redirects=False,
            # Media bytes and the internal key must stay on the private link;
            # never route this service-to-service traffic through ambient
            # HTTP(S)_PROXY settings.
            trust_env=False,
        )

    def _url(
        self,
        filename: str,
        *,
        storage_key: str | None = None,
        mime_type: str | None = None,
    ) -> str:
        safe_filename = _validate_filename(filename)
        encoded_filename = quote(safe_filename, safe="")
        url = f"{self.base_url}{self.path_prefix}/{encoded_filename}"
        query = {
            key: value
            for key, value in {
                "storage_key": (storage_key or "").strip(),
                "mime_type": (mime_type or "").strip(),
            }.items()
            if value
        }
        return f"{url}?{urlencode(query)}" if query else url

    def _auth_headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {"X-Internal-Media-Key": self.api_key}
        if extra:
            headers.update(extra)
        return headers

    @classmethod
    def _request_headers(cls, request_headers: Mapping[str, str] | None) -> dict[str, str]:
        if not request_headers:
            return {}
        return {
            str(name).lower(): str(value)
            for name, value in request_headers.items()
            if str(name).lower() in cls._forward_request_headers
        }

    @classmethod
    def _response_headers(cls, response: httpx.Response) -> dict[str, str]:
        return {
            name: value
            for name, value in response.headers.items()
            if name.lower() in cls._forward_response_headers
        }

    @staticmethod
    def _raise_remote_error(
        response: httpx.Response,
        operation: str,
        *,
        allow_range_not_satisfiable: bool = False,
        allow_not_modified: bool = False,
    ) -> None:
        status_code = response.status_code
        if 200 <= status_code < 300:
            return
        if allow_not_modified and status_code == 304:
            return
        if allow_range_not_satisfiable and status_code == 416:
            return
        if status_code == 404:
            raise FileNotFoundError(operation)
        raise MediaStorageError(
            f"Remote media {operation} failed with HTTP {status_code}."
        )

    def put(self, filename: str, content: bytes, mime_type: str) -> str:
        try:
            response = self.client.put(
                self._url(filename),
                content=content,
                headers=self._auth_headers({"Content-Type": mime_type}),
            )
            self._raise_remote_error(response, "upload")
        except FileNotFoundError as exc:
            raise MediaStorageError("Remote media upload endpoint was not found.") from exc
        except MediaStorageError:
            raise
        except httpx.HTTPError as exc:
            raise MediaStorageError("Remote media upload failed.") from exc
        return _validate_filename(filename)

    def delete(self, storage_key: str, filename: str) -> None:
        try:
            response = self.client.delete(
                self._url(filename, storage_key=storage_key),
                headers=self._auth_headers(),
            )
            # The gateway deliberately uses 404 for a missing/invalid key.  A
            # valid gateway DELETE is always 204, even when the local file was
            # already absent, so never swallow an arbitrary 404 here: doing so
            # would drop the outbox task while leaving an orphaned object.
            if response.status_code == 404:
                raise MediaStorageError(
                    "Remote media delete endpoint rejected the request."
                )
            self._raise_remote_error(response, "delete")
        except MediaStorageError:
            raise
        except httpx.HTTPError as exc:
            raise MediaStorageError("Remote media delete failed.") from exc

    def build_response(
        self,
        storage_key: str,
        filename: str,
        mime_type: str,
        request_headers: Mapping[str, str] | None = None,
    ) -> Response:
        forwarded_request_headers = self._request_headers(request_headers)
        request = self.client.build_request(
            "GET",
            self._url(
                filename,
                storage_key=storage_key,
                mime_type=mime_type,
            ),
            headers=self._auth_headers(forwarded_request_headers),
        )
        try:
            stream_context = self.client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise MediaStorageError("Remote media read failed.") from exc

        try:
            # A 416 is a valid media response for an unsatisfiable browser
            # Range request and must reach the caller unchanged.
            self._raise_remote_error(
                stream_context,
                "read",
                allow_range_not_satisfiable=("range" in forwarded_request_headers),
                allow_not_modified=True,
            )
        except Exception:
            stream_context.close()
            raise

        response_headers = self._response_headers(stream_context)
        response_headers.setdefault("accept-ranges", "bytes")

        def body() -> Iterator[bytes]:
            try:
                yield from stream_context.iter_bytes()
            finally:
                stream_context.close()

        return StreamingResponse(
            body(),
            status_code=stream_context.status_code,
            headers=response_headers,
            media_type=response_headers.get("content-type", mime_type),
        )

    def check(self) -> None:
        filename = f".storage-health-{uuid.uuid4().hex}.txt"
        payload = b"knowledge-kb-storage-health"
        self.put(filename, payload, "text/plain")
        try:
            response = self.client.get(
                self._url(filename),
                headers=self._auth_headers(),
            )
            self._raise_remote_error(response, "health read")
            if response.content != payload:
                raise MediaStorageError("Remote media storage returned unexpected data.")
        finally:
            self.delete("", filename)


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
    if backend == "remote":
        return RemoteMediaStorage(settings)
    raise MediaStorageError("MEDIA_STORAGE_BACKEND must be local, remote, or s3.")
