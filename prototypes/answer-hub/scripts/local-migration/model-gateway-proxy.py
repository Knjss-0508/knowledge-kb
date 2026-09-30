# -*- coding: utf-8 -*-
"""Local model-gateway proxy for the knowledge-kb backend.

Why this exists
---------------
The production server cannot reach the corporate model gateway
(``tokenhub.zhuanspirit.com:443`` is blocked from that network), while this
developer machine can.  The backend only needs an OpenAI-compatible
``/v1/chat/completions`` endpoint, so this small stdlib-only server runs on the
developer machine and forwards those calls to the real gateway over HTTPS.

Design decisions
----------------
* The real gateway API key lives ONLY on this machine, read at runtime from the
  ``GROUP_LLM_API_KEY`` environment variable (or the per-user environment in the
  registry as a fallback).  It is never written to disk, never logged, and never
  sent to the server.
* Because the reverse tunnel publishes this proxy, every data request must carry
  a shared token.  Two tokens are accepted:
    - MODEL_GATEWAY_ACCESS_TOKEN  administrative token, used for manual probes
    - MODEL_GATEWAY_CLIENT_TOKEN  the value the server-side backend sends; this
      machine verifies it and then substitutes the real gateway credential
  Both live only in this machine's user environment, so nothing worth stealing
  is stored on the server and the real gateway key never leaves this machine.
  Token comparison is constant-time.
* The proxy is plain HTTP towards the tunnel (no TLS/SNI games needed) and HTTPS
  towards the real gateway.
* Only request metadata is logged (path, status, latency).  Prompt and response
  bodies are never logged.
"""

from __future__ import annotations

import hmac
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GATEWAY_BASE = os.environ.get(
    "MODEL_GATEWAY_UPSTREAM_BASE", "https://tokenhub.zhuanspirit.com/codex/v1"
).rstrip("/")
LISTEN_HOST = os.environ.get("MODEL_GATEWAY_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("MODEL_GATEWAY_LISTEN_PORT", "19000"))
DEFAULT_MODEL = os.environ.get("MODEL_GATEWAY_DEFAULT_MODEL", "deepseek-flash")
UPSTREAM_TIMEOUT = float(os.environ.get("MODEL_GATEWAY_TIMEOUT_SECONDS", "90"))
LOG_PATH = os.environ.get(
    "MODEL_GATEWAY_LOG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "model-gateway-proxy.log"),
)
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 2

ACCESS_TOKEN_NAME = "MODEL_GATEWAY_ACCESS_TOKEN"
CLIENT_TOKEN_NAME = "MODEL_GATEWAY_CLIENT_TOKEN"
KEY_NAME = "GROUP_LLM_API_KEY"

_log_lock = threading.Lock()


def _read_user_env(name: str) -> str:
    """Read a per-user environment variable straight from the registry."""
    try:
        import winreg  # type: ignore
    except Exception:
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            value, _ = winreg.QueryValueEx(handle, name)
            return str(value or "")
    except Exception:
        return ""


def _secret(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    return _read_user_env(name).strip()


ACCESS_TOKEN = _secret(ACCESS_TOKEN_NAME)
CLIENT_TOKEN = _secret(CLIENT_TOKEN_NAME)
if not ACCESS_TOKEN and not CLIENT_TOKEN:
    print(
        "FATAL: neither %s nor %s is set (process environment or HKCU\\Environment)."
        % (ACCESS_TOKEN_NAME, CLIENT_TOKEN_NAME),
        flush=True,
    )
    sys.exit(2)


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = "%s %s\n" % (stamp, message)
    with _log_lock:
        try:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
                for index in range(LOG_BACKUPS, 0, -1):
                    src = LOG_PATH if index == 1 else "%s.%d" % (LOG_PATH, index - 1)
                    dst = "%s.%d" % (LOG_PATH, index)
                    if os.path.exists(src):
                        try:
                            os.replace(src, dst)
                        except OSError:
                            pass
            with open(LOG_PATH, "a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass
    sys.stdout.write(line)
    sys.stdout.flush()


def _upstream_key() -> str:
    key = _secret(KEY_NAME)
    if not key:
        raise RuntimeError("%s is not available on this machine." % KEY_NAME)
    return key


def _forward(method: str, raw_path: str, body: bytes, client_model: str | None):
    """Send one request to the real gateway and return (status, headers, body)."""
    parsed = urllib.parse.urlsplit(raw_path)
    upstream_url = GATEWAY_BASE + parsed.path + (("?" + parsed.query) if parsed.query else "")
    headers = {
        "Authorization": "Bearer " + _upstream_key(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = body
    if body and client_model:
        try:
            parsed_body = json.loads(body.decode("utf-8"))
            if isinstance(parsed_body, dict) and not str(parsed_body.get("model") or "").strip():
                parsed_body["model"] = client_model
                payload = json.dumps(parsed_body).encode("utf-8")
        except Exception:
            payload = body
    request = urllib.request.Request(
        upstream_url, data=payload if method not in ("GET", "HEAD") else None,
        headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT) as response:
            return (
                response.status,
                response.headers.get("Content-Type") or "application/json",
                response.read(),
            )
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            exc.headers.get("Content-Type") if exc.headers else "application/json",
            exc.read(),
        )
    except socket.timeout as exc:
        raise TimeoutError(str(exc)) from exc
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), socket.timeout):
            raise TimeoutError(str(exc)) from exc
        raise


class Handler(BaseHTTPRequestHandler):
    server_version = "model-gateway-proxy/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # silence default stderr noise
        return

    def _reply(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _reply_json(self, status: int, payload: dict) -> None:
        self._reply(
            status, "application/json", json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization") or ""
        token = header[7:].strip() if header[:7].lower() == "bearer " else ""
        if not token:
            return False
        for candidate in (ACCESS_TOKEN, CLIENT_TOKEN):
            if candidate and hmac.compare_digest(token, candidate):
                return True
        return False

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path == "/health":
            key_ok = bool(_secret(KEY_NAME))
            self._reply_json(
                200,
                {
                    "status": "ok",
                    "upstream_base": GATEWAY_BASE,
                    "default_model": DEFAULT_MODEL,
                    "gateway_key_present": key_ok,
                },
            )
            return
        if not path.startswith("/v1/"):
            self._reply_json(404, {"error": {"message": "only /v1/* and /health are exposed"}})
            return
        if not self._authorized():
            log("GET %s -> 401 (bad or missing access token)" % path)
            self._reply_json(401, {"error": {"message": "invalid access token"}})
            return
        self._handle_upstream("GET", b"")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/v1/"):
            self._reply_json(404, {"error": {"message": "only /v1/* is exposed"}})
            return
        if not self._authorized():
            log("POST %s -> 401 (bad or missing access token)" % path)
            self._reply_json(401, {"error": {"message": "invalid access token"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        self._handle_upstream("POST", body)

    def _handle_upstream(self, method: str, body: bytes) -> None:
        path = urllib.parse.urlsplit(self.path).path
        client_model = DEFAULT_MODEL if path.endswith("/chat/completions") else None
        started = time.time()
        try:
            status, content_type, payload = _forward(method, self.path, body, client_model)
        except TimeoutError:
            log("%s %s -> 504 upstream timeout after %.1fs" % (method, path, time.time() - started))
            self._reply_json(504, {"error": {"message": "upstream model gateway timed out"}})
            return
        except RuntimeError as exc:
            log("%s %s -> 500 %s" % (method, path, exc))
            self._reply_json(500, {"error": {"message": str(exc)}})
            return
        except Exception as exc:  # noqa: BLE001
            log(
                "%s %s -> 502 upstream error %s after %.1fs"
                % (method, path, type(exc).__name__, time.time() - started)
            )
            self._reply_json(502, {"error": {"message": "upstream model gateway unreachable"}})
            return
        log(
            "%s %s -> %d in %.2fs (%d bytes)"
            % (method, path, status, time.time() - started, len(payload))
        )
        self._reply(status, content_type, payload)


def main() -> None:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.daemon_threads = True
    log(
        "proxy listening on %s:%d -> %s (model=%s, gateway_key_present=%s, client_token_configured=%s)"
        % (
            LISTEN_HOST,
            LISTEN_PORT,
            GATEWAY_BASE,
            DEFAULT_MODEL,
            bool(_secret(KEY_NAME)),
            bool(CLIENT_TOKEN),
        )
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
