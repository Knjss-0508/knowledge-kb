from __future__ import annotations

from dataclasses import asdict, dataclass
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import base64
import socket


ALLOWED_TYPES = {
    "image/jpeg": b"\xff\xd8\xff",
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/webp": b"RIFF",
}


@dataclass
class ImageEvidence:
    url: str
    status: str
    mime_type: str = ""
    byte_size: int = 0
    error: str = ""
    data_url: str = ""

    def metadata(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("data_url", None)
        return value


def _is_public_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    try:
        addresses = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return False
    for address in addresses:
        candidate = ip_address(address[4][0])
        if candidate.is_private or candidate.is_loopback or candidate.is_link_local or candidate.is_reserved:
            return False
    return True


def _detect_mime(data: bytes, header_type: str) -> str:
    if data.startswith(ALLOWED_TYPES["image/jpeg"]):
        return "image/jpeg"
    if data.startswith(ALLOWED_TYPES["image/png"]):
        return "image/png"
    if data.startswith(ALLOWED_TYPES["image/webp"]) and data[8:12] == b"WEBP":
        return "image/webp"
    return header_type.split(";", 1)[0].strip().lower()


def split_image_urls(value: str) -> list[str]:
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


class ImageDownloader:
    def __init__(self, max_images: int = 4, max_bytes: int = 5 * 1024 * 1024, timeout_seconds: int = 10) -> None:
        self.max_images = max_images
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds

    def fetch(self, image_links: str) -> list[ImageEvidence]:
        results: list[ImageEvidence] = []
        for url in split_image_urls(image_links)[: self.max_images]:
            results.append(self._fetch_one(url))
        return results

    def _fetch_one(self, url: str) -> ImageEvidence:
        if not _is_public_url(url):
            return ImageEvidence(url=url, status="blocked", error="图片链接不是可访问的公网地址")
        try:
            request = Request(url, headers={"User-Agent": "AnswerHubPhoneMVP/1.0"})
            with urlopen(request, timeout=self.timeout_seconds) as response:
                header_type = response.headers.get("Content-Type", "")
                data = response.read(self.max_bytes + 1)
            if len(data) > self.max_bytes:
                return ImageEvidence(url=url, status="rejected", error="图片超过 5MB 限制")
            mime_type = _detect_mime(data, header_type)
            if mime_type not in ALLOWED_TYPES:
                return ImageEvidence(url=url, status="rejected", mime_type=mime_type, error="仅支持 JPEG、PNG、WebP")
            data_url = f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"
            return ImageEvidence(
                url=url,
                status="ready",
                mime_type=mime_type,
                byte_size=len(data),
                data_url=data_url,
            )
        except Exception as exc:
            return ImageEvidence(url=url, status="failed", error=str(exc)[:240])
