"""Restricted outbound HTTP for untrusted company and careers URLs."""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError

Resolver = Callable[..., list[tuple[Any, ...]]]
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5
# a fetched page or API reply is read up to this many bytes after decompression,
# and for at most this many seconds in all, so a hostile server cannot exhaust
# memory with a compressed bomb or hold a worker with a slow trickle
# real replies reach 13 MB (Veeva's Lever board, the SimplifyJobs lists,
# Anthropic's Greenhouse board at 10 MB); 8 MB refused them
MAX_BODY_BYTES = 64_000_000
MAX_READ_SECONDS = 60.0
# IPv6 ranges that carry an IPv4 address inside: NAT64, 6to4, Teredo
_EMBEDS_IPV4 = tuple(
    ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32")
)


def is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """A global address, and not an IPv6 form that wraps another address."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped.is_global
        if any(address in net for net in _EMBEDS_IPV4):
            return False
    return address.is_global


def validate_public_https_url(url: str, resolver: Resolver = socket.getaddrinfo) -> str:
    """Return a normalized public HTTPS URL or reject it.

    This blocks loopback, private, link-local, multicast, and reserved targets.
    DNS is checked before each request and redirect. This is the network policy
    used for URLs supplied through the CLI or Telegram.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("outbound URLs must use HTTPS and include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("outbound URLs cannot include credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("outbound URL has an invalid port") from exc
    if port not in (None, 443):
        raise ValueError("outbound URLs can only use port 443")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("outbound URL hostname is not public")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    addresses = [literal] if literal is not None else _resolve(host, resolver)
    if not addresses or any(not is_public(address) for address in addresses):
        raise ValueError("outbound URL resolves to a non-public address")
    name = f"[{host}]" if ":" in host else host
    netloc = name if port is None else f"{name}:{port}"
    return urlunparse(("https", netloc, parsed.path or "/", "", parsed.query, ""))


def _resolve(host: str, resolver: Resolver) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        records = resolver(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"outbound URL hostname cannot be resolved: {host}") from exc
    addresses = []
    for record in records:
        sockaddr = record[4]
        addresses.append(ipaddress.ip_address(sockaddr[0]))
    return list(dict.fromkeys(addresses))


class SafeSession:
    """Requests-compatible session that validates every redirect destination."""

    def __init__(
        self,
        session: requests.Session | None = None,
        resolver: Resolver = socket.getaddrinfo,
        interval: float = 1.0,
    ) -> None:
        self._session = session or requests.Session()
        self._resolver = resolver
        self._interval = interval
        self._last_request: float | None = None

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Send GET or POST after checking every destination and redirect."""
        if method not in {"GET", "POST"}:
            raise ValueError("SafeSession only permits GET and POST")
        follow = kwargs.pop("allow_redirects", method == "GET")
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            current = validate_public_https_url(current, self._resolver)
            if self._last_request is not None:
                time.sleep(max(0.0, self._interval - (time.monotonic() - self._last_request)))
            self._last_request = time.monotonic()
            response = self._session.request(method, current, allow_redirects=False, **kwargs)
            if response.status_code not in REDIRECT_STATUSES or not follow:
                return response
            location = response.headers.get("Location")
            if not location:
                return response
            destination = urljoin(current, location)
            if method != "GET":
                raise ValueError("POST redirects are disabled")
            authenticated = any(
                key.lower() in {"authorization", "cookie", "x-api-key"}
                for key in kwargs.get("headers", {})
            ) or bool(kwargs.get("auth"))
            if authenticated and urlparse(current).hostname != urlparse(destination).hostname:
                raise ValueError("authenticated cross-host redirects are disabled")
            response.close()
            current = destination
        raise requests.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects")


class _PinnedHTTPSConnection(HTTPSConnection):
    """Resolves the host itself, refuses any non-public answer, and connects to
    the address it checked, so a DNS answer that changes between the check and
    the connection (rebinding) never reaches a private address. TLS still
    verifies the certificate against the hostname."""

    def _new_conn(self) -> socket.socket:
        if self._tunnel_host is not None:  # through a proxy: the proxy connects
            _resolve_public(self._tunnel_host)
            return super()._new_conn()
        try:
            addresses = _resolve_public(self._dns_host)
        except ValueError as exc:
            raise NewConnectionError(self, str(exc)) from exc
        host = self._dns_host
        self._dns_host = str(addresses[0])
        try:
            return super()._new_conn()
        finally:
            self._dns_host = host


def _resolve_public(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    host = host.strip("[]")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        addresses = _resolve(host, RESOLVER)
    if not addresses or any(not is_public(a) for a in addresses):
        raise ValueError(f"{host} resolves to a non-public address")
    return addresses


RESOLVER: Resolver = socket.getaddrinfo


class _PinnedPool(HTTPSConnectionPool):
    ConnectionCls = _PinnedHTTPSConnection


class PublicAdapter(HTTPAdapter):
    """HTTPS only, every connection pinned to a checked public address."""

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"https": _PinnedPool}
        self.poolmanager.key_fn_by_scheme = {"https": self.poolmanager.key_fn_by_scheme["https"]}


_LOCAL = threading.local()


def _public_session() -> SafeSession:
    """One guarded session per thread: pipeline fetches run on worker threads."""
    session = getattr(_LOCAL, "session", None)
    if session is None:
        raw = requests.Session()
        raw.mount("https://", PublicAdapter())
        raw.mount("http://", HTTPAdapter(max_retries=0))  # validation rejects http first
        session = SafeSession(raw, interval=0.0)
        _LOCAL.session = session
    return session


def _capped(response: requests.Response) -> requests.Response:
    """Read the streamed body within MAX_BODY_BYTES and MAX_READ_SECONDS."""
    deadline = time.monotonic() + MAX_READ_SECONDS
    chunks: list[bytes] = []
    size = 0
    try:
        for chunk in response.iter_content(64 * 1024):  # decompressed bytes
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise ValueError(f"response over {MAX_BODY_BYTES} bytes: {response.url}")
            if time.monotonic() > deadline:
                raise requests.Timeout(f"response over {MAX_READ_SECONDS:.0f}s: {response.url}")
            chunks.append(chunk)
    finally:
        response.close()
    response._content = b"".join(chunks)  # the body, read once
    return response


class Refused(requests.RequestException, ValueError):
    """A URL or reply the guard would not take: callers that catch request
    errors and callers that catch ValueError both see it."""


def get(url: str, **kwargs: Any) -> requests.Response:
    """GET an untrusted URL: public HTTPS only, every redirect checked, the
    connection pinned to the checked address, the body size and time capped."""
    return _guarded("GET", url, kwargs)


def post(url: str, **kwargs: Any) -> requests.Response:
    """POST with the same guards as get(); redirects are not followed."""
    return _guarded("POST", url, kwargs)


def _guarded(method: str, url: str, kwargs: dict[str, Any]) -> requests.Response:
    kwargs["stream"] = True
    try:
        return _capped(_public_session().request(method, url, **kwargs))
    except requests.RequestException:
        raise
    except ValueError as exc:
        raise Refused(str(exc)) from exc
