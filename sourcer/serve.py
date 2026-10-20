"""Minimal HTTP scoring service, stdlib only. Used by the Kubernetes
Deployment; also runnable directly:

  python -m sourcer.serve            # listens on :8500

Endpoints:
  GET  /healthz   liveness probe
  GET  /digest    ranked postings from the bundled data.json
  POST /score     {"posting_text": "...", "title": "", "company": ""}
                  parse + score via the Claude API (same as the MCP tool)

/digest and /score need "Authorization: Bearer $SOURCER_HTTP_TOKEN"; the
service will not start without the token, even on loopback, because any web
page the operator visits can reach 127.0.0.1. SOURCER_COST_CAP bounds the
process's total spend, at most SOURCER_HTTP_CONCURRENCY scores run at once,
and a connection gets CONNECTION_DEADLINE seconds in all.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import anthropic
from dotenv import load_dotenv

from . import config, llm, log
from .pipeline import scoring

logger = log.get_logger(__name__)

PORT = int(os.environ.get("SOURCER_HTTP_PORT", "8500"))
HOST = os.environ.get("SOURCER_HTTP_HOST", "127.0.0.1")
TOKEN = os.environ.get("SOURCER_HTTP_TOKEN", "")
# one spending total for the life of the process: the cost cap bounds all of it
BUDGET = llm.Usage()
SCORING = threading.BoundedSemaphore(max(1, int(os.environ.get("SOURCER_HTTP_CONCURRENCY", "2"))))
MAX_CONNECTIONS = 32
CONNECTION_DEADLINE = 30.0  # seconds for a whole request, however slowly it arrives
CONTENT_LENGTH_RE = re.compile(r"[0-9]{1,7}")
DATA = config.PROJECT_ROOT / "docs" / "data.json"
DIGEST_FIELDS = ("company", "title", "url", "score", "timing_unclear", "status")
MAX_BODY_BYTES = 256_000
DRAIN_TIMEOUT = 1.0


def score(payload: dict[str, Any]) -> dict[str, Any]:
    """Delegate scoring policy to the shared application service."""
    result = scoring.score_text(
        payload["posting_text"], payload.get("title", ""), payload.get("company", ""), BUDGET
    )
    return {key: value for key, value in result.items() if key != "parsed"}


def digest() -> list[dict[str, Any]] | None:
    """Ranked postings from the bundled export, or None when there is no export."""
    if not DATA.exists():
        return None
    data = json.loads(DATA.read_text())
    # Unscored listings (sourced but not yet through score_listings) have
    # score None and are not rankable.
    ranked = [
        {k: p[k] for k in DIGEST_FIELDS}
        for p in data["postings"]
        if not p["excluded"] and p["score"] is not None
    ]
    return sorted(ranked, key=lambda p: -p["score"])


class Handler(BaseHTTPRequestHandler):
    server_version = "sourcer"
    sys_version = ""

    def setup(self) -> None:
        self.request.settimeout(config.REQUEST_TIMEOUT)
        # a client that trickles its headers one byte at a time still loses
        # the connection at the deadline
        self._deadline = threading.Timer(CONNECTION_DEADLINE, self._cut)
        self._deadline.daemon = True
        self._deadline.start()
        super().setup()

    def _cut(self) -> None:
        with contextlib.suppress(OSError):
            self.request.shutdown(socket.SHUT_RDWR)

    def finish(self) -> None:
        self._deadline.cancel()
        super().finish()

    def _authorized(self) -> bool:
        given = self.headers.get("Authorization", "").encode()
        return bool(TOKEN) and hmac.compare_digest(given, f"Bearer {TOKEN}".encode())

    def _json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _drain_unread_body(self) -> None:
        """Close after refusing a body without reading it.

        Closing a socket that still holds unread bytes resets the connection,
        so a client still sending its body gets a broken pipe instead of our
        reply. Stop writing, read and discard what is left, then close.
        """
        self.close_connection = True
        try:
            self.request.shutdown(socket.SHUT_WR)
            self.request.settimeout(DRAIN_TIMEOUT)
            left = MAX_BODY_BYTES
            while left > 0 and (chunk := self.request.recv(min(left, 65536))):
                left -= len(chunk)
        except OSError:
            pass

    def do_GET(self) -> None:  # http.server API name
        if self.path == "/healthz":
            self._json(200, {"ok": True})
        elif self.path == "/digest":
            if not self._authorized():
                self._json(401, {"error": "bearer token required"})
                return
            ranked = digest()
            if ranked is None:
                self._json(404, {"error": "no data.json in image"})
            else:
                self._json(200, ranked)
        else:
            self._json(404, {"error": "unknown path"})

    def do_POST(self) -> None:  # http.server API name
        if self.path != "/score":
            self._json(404, {"error": "unknown path"})
            return
        if not self._authorized():
            self._json(401, {"error": "bearer token required"})
            self._drain_unread_body()
            return
        body_read = False
        try:
            if (
                self.headers.get("Transfer-Encoding")
                or len(self.headers.get_all("Content-Length", [])) != 1
            ):
                raise ValueError("one Content-Length header required")
            length = self.headers.get("Content-Length", "")
            if not CONTENT_LENGTH_RE.fullmatch(length):
                raise ValueError("Content-Length must be plain digits")
            n = int(length)
            if n <= 0:
                raise ValueError("Content-Length must be positive")
            if n > MAX_BODY_BYTES:
                self._json(413, {"error": f"body over {MAX_BODY_BYTES} bytes"})
                self._drain_unread_body()
                return
            raw = self.rfile.read(n)
            body_read = True
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not payload.get("posting_text"):
                self._json(400, {"error": "posting_text required"})
                return
            scoring.validate_input(
                payload["posting_text"], payload.get("title", ""), payload.get("company", "")
            )
        except (ValueError, TimeoutError, RecursionError):
            self._json(400, {"error": "invalid scoring request"})
            if not body_read:
                self._drain_unread_body()
            return
        if not SCORING.acquire(blocking=False):
            self._json(429, {"error": "busy, try again shortly"})
            return
        try:
            self._score(payload)
        finally:
            SCORING.release()

    def _score(self, payload: dict[str, Any]) -> None:
        try:
            self._json(200, score(payload))
        except anthropic.APIError:
            logger.warning("model call failed")
            self._json(502, {"error": "model call failed"})
        except Exception:  # keep the pod alive on any failure
            logger.exception("unhandled error scoring a posting")
            self._json(500, {"error": "internal scoring error"})

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s %s", self.address_string(), fmt % args)


class Server(ThreadingHTTPServer):
    """At most MAX_CONNECTIONS connections at once; more are closed at once."""

    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._open = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._open.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._open.release()


def main() -> int:
    load_dotenv()
    log.configure()
    if not TOKEN:
        logger.error("set SOURCER_HTTP_TOKEN: /score is a paid model call")
        return 2
    logger.info("sourcer scoring service on :%s", PORT)
    Server((HOST, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
