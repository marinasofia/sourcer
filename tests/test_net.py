"""Outbound HTTP policy and redirect checks."""

from __future__ import annotations

import socket
from typing import Any

import pytest
import requests
from urllib3.connection import HTTPConnection
from urllib3.exceptions import NewConnectionError

from sourcer import net
from sourcer.net import SafeSession, validate_public_https_url


def public_resolver(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
    del host, kwargs
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/jobs",
        "https://localhost/jobs",
        "https://127.0.0.1/jobs",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.1/jobs",
        "https://user:password@example.com/jobs",
        "https://example.com:8443/jobs",
    ],
)
def test_validate_public_https_url_rejects_unsafe_targets(url: str) -> None:
    with pytest.raises(ValueError):
        validate_public_https_url(url, public_resolver)


def test_validate_public_https_url_normalizes_and_removes_fragment() -> None:
    assert (
        validate_public_https_url("https://EXAMPLE.com/jobs?q=ai#top", public_resolver)
        == "https://example.com/jobs?q=ai"
    )


class Response:
    def __init__(self, status: int, url: str, location: str | None = None) -> None:
        self.status_code = status
        self.url = url
        self.headers = {"Location": location} if location else {}
        self.text = ""

    def close(self) -> None:
        pass


class Session:
    def __init__(self, responses: list[Response]) -> None:
        self.responses = iter(responses)
        self.calls: list[str] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        assert method in {"GET", "POST"}
        self.calls.append(url)
        return next(self.responses)


def test_safe_session_post_uses_the_same_url_policy() -> None:
    session = Session([Response(200, "https://example.com/api")])
    response = SafeSession(session, public_resolver, interval=0).post("https://example.com/api")  # type: ignore[arg-type]
    assert response.status_code == 200
    assert session.calls == ["https://example.com/api"]


def test_safe_session_validates_redirects() -> None:
    session = Session(
        [
            Response(302, "https://example.com/start", "/jobs"),
            Response(200, "https://example.com/jobs"),
        ]
    )
    response = SafeSession(session, public_resolver, interval=0).get("https://example.com/start")  # type: ignore[arg-type]
    assert response.status_code == 200
    assert session.calls == ["https://example.com/start", "https://example.com/jobs"]


def test_safe_session_blocks_private_redirect_before_request() -> None:
    session = Session([Response(302, "https://example.com", "https://127.0.0.1/secret")])
    with pytest.raises(ValueError):
        SafeSession(session, public_resolver, interval=0).get("https://example.com")  # type: ignore[arg-type]
    assert session.calls == ["https://example.com/"]


def test_safe_session_caps_redirects() -> None:
    redirects = [Response(302, "https://example.com", "/again") for _ in range(6)]
    with pytest.raises(requests.TooManyRedirects):
        SafeSession(Session(redirects), public_resolver, interval=0).get("https://example.com")  # type: ignore[arg-type]


def test_safe_session_rejects_other_methods() -> None:
    with pytest.raises(ValueError, match="only permits"):
        SafeSession(Session([]), public_resolver).request("DELETE", "https://example.com")  # type: ignore[arg-type]


def test_post_does_not_forward_private_payload_on_redirect() -> None:
    session = Session([Response(307, "https://example.com", "https://evil.example")])
    response = SafeSession(session, public_resolver, interval=0).post(
        "https://example.com", json={"private": 1}
    )
    assert response.status_code == 307
    assert len(session.calls) == 1


def test_authenticated_get_cannot_redirect_to_another_host() -> None:
    session = Session([Response(302, "https://example.com", "https://evil.example")])
    with pytest.raises(ValueError, match="authenticated"):
        SafeSession(session, public_resolver, interval=0).get(
            "https://example.com", headers={"authorization": "secret"}
        )
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://[64:ff9b::7f00:1]/jobs",  # NAT64 wrapping 127.0.0.1
        "https://[::ffff:127.0.0.1]/jobs",
        "https://[2002:7f00:1::]/jobs",  # 6to4 wrapping 127.0.0.1
    ],
)
def test_ipv6_forms_that_wrap_a_private_address_are_refused(url: str) -> None:
    with pytest.raises(ValueError):
        validate_public_https_url(url, public_resolver)


def test_an_ipv6_literal_keeps_its_brackets() -> None:
    url = "https://[2606:4700::1111]/jobs"
    assert validate_public_https_url(url, public_resolver) == url


def private_resolver(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
    del host, kwargs
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]


def test_the_connection_refuses_an_answer_that_changed_to_private(monkeypatch) -> None:
    monkeypatch.setattr(net, "RESOLVER", private_resolver)
    conn = net._PinnedHTTPSConnection("jobs.example.com", 443)
    with pytest.raises(NewConnectionError):
        conn._new_conn()


def test_the_connection_dials_the_address_it_checked(monkeypatch) -> None:
    dialed = []
    monkeypatch.setattr(net, "RESOLVER", public_resolver)
    monkeypatch.setattr(
        HTTPConnection, "_new_conn", lambda self: dialed.append(self._dns_host) or "socket"
    )
    conn = net._PinnedHTTPSConnection("jobs.example.com", 443)
    assert conn._new_conn() == "socket"
    assert dialed == ["93.184.216.34"] and conn._dns_host == "jobs.example.com"


class Streamed:
    url = "https://example.com/big"

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    def iter_content(self, size: int) -> Any:
        del size
        return iter(self.chunks)

    def close(self) -> None:
        self.closed = True


def test_a_reply_over_the_size_cap_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(net, "MAX_BODY_BYTES", 10)
    big = Streamed([b"x" * 8, b"x" * 8])

    class Guarded:
        def request(self, method: str, url: str, **kwargs: Any) -> Streamed:
            assert kwargs["stream"] is True
            return big

    monkeypatch.setattr(net, "_public_session", Guarded)
    with pytest.raises(net.Refused) as caught:
        net.get("https://example.com/big")
    assert isinstance(caught.value, requests.RequestException) and big.closed


def test_a_reply_within_the_cap_is_read_once(monkeypatch) -> None:
    small = Streamed([b"ab", b"cd"])

    class Guarded:
        def request(self, method: str, url: str, **kwargs: Any) -> Streamed:
            return small

    monkeypatch.setattr(net, "_public_session", Guarded)
    assert net.get("https://example.com/small")._content == b"abcd"


def test_get_refuses_plain_http_before_any_connection() -> None:
    with pytest.raises(net.Refused):
        net.get("http://example.com/jobs")
