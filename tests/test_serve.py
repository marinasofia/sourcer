"""HTTP scoring service tests against a live server on an ephemeral port."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import anthropic
import httpx
import pytest

from sourcer import serve
from sourcer.pipeline import scoring
from tests.conftest import make_client

PARSE_REPLY = json.dumps(
    {
        "title": "AI Engineer Intern",
        "location": "New York, NY",
        "requirements": ["Python"],
        "seniority": "intern",
        "tech_stack": ["Python"],
        "employment_type": "intern",
        "start_date": "summer 2027",
        "years_experience_min": None,
        "work_mode": "onsite",
    }
)
SCORE_REPLY = json.dumps({"score": 81, "rationale": "Strong Python match."})


TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(serve, "TOKEN", TOKEN)


@pytest.fixture
def server():
    httpd = serve.Server(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(base, path, body=None, headers=None):
    data = (
        None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    )
    headers = {**AUTH, **(headers or {})}
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_healthz_and_unknown_paths(server):
    assert call(server, "/healthz") == (200, {"ok": True})
    assert call(server, "/nope")[0] == 404
    assert call(server, "/nope", body={"x": 1})[0] == 404


def test_digest_without_and_with_data(server, monkeypatch, tmp_path):
    monkeypatch.setattr(serve, "DATA", tmp_path / "missing.json")
    assert call(server, "/digest")[0] == 404
    data = tmp_path / "data.json"
    data.write_text(
        json.dumps(
            {
                "postings": [
                    {
                        "company": "A",
                        "title": "t",
                        "url": "u",
                        "score": 50,
                        "timing_unclear": 0,
                        "status": "new",
                        "excluded": 0,
                    },
                    {
                        "company": "B",
                        "title": "t2",
                        "url": "u2",
                        "score": 90,
                        "timing_unclear": 1,
                        "status": "new",
                        "excluded": 0,
                    },
                    {
                        "company": "C",
                        "title": "listing",
                        "url": "u3",
                        "score": None,
                        "timing_unclear": 0,
                        "status": "new",
                        "excluded": 0,
                    },
                    {
                        "company": "D",
                        "title": "sr",
                        "url": "u4",
                        "score": 0,
                        "timing_unclear": 0,
                        "status": "new",
                        "excluded": 1,
                    },
                ]
            }
        )
    )
    monkeypatch.setattr(serve, "DATA", data)
    status, ranked = call(server, "/digest")
    assert status == 200
    assert [p["company"] for p in ranked] == ["B", "A"]
    assert set(ranked[0]) == set(serve.DIGEST_FIELDS)


def test_score_happy_path(server, paths, monkeypatch):
    monkeypatch.setattr(scoring.llm, "get_client", lambda: make_client([PARSE_REPLY, SCORE_REPLY]))
    status, out = call(server, "/score", {"posting_text": "Python intern", "title": "AI Intern"})
    assert status == 200
    assert out["score"] == 81 and out["excluded"] is False
    assert out["est_cost_usd"] > 0


def test_score_excluded(server, paths, monkeypatch):
    monkeypatch.setattr(scoring.llm, "get_client", lambda: make_client([PARSE_REPLY]))
    status, out = call(
        server, "/score", {"posting_text": "Lead everything", "title": "Senior ML Engineer"}
    )
    assert status == 200
    assert out["excluded"] is True and out["score"] == 0
    assert "senior" in out["exclusion_reason"]


def test_score_parse_and_score_failures(server, paths, monkeypatch):
    monkeypatch.setattr(scoring.llm, "get_client", lambda: make_client(["nope", "nope"]))
    status, out = call(server, "/score", {"posting_text": "x"})
    assert status == 200 and out["error"].startswith("parse failed")
    monkeypatch.setattr(
        scoring.llm, "get_client", lambda: make_client([PARSE_REPLY, "bad", "still bad"])
    )
    status, out = call(server, "/score", {"posting_text": "x"})
    assert status == 200 and out["error"].startswith("score failed")


def test_score_bad_requests(server):
    assert call(server, "/score", body=b"{not json", headers={"Content-Length": "9"})[0] == 400
    assert call(server, "/score", {"title": "no text"})[0] == 400
    assert call(server, "/score", body=b"[1,2]")[0] == 400
    # The limit is enforced from the header before the body is read.
    assert (
        call(
            server,
            "/score",
            body=b"{}",
            headers={"Content-Length": str(serve.MAX_BODY_BYTES + 1)},
        )[0]
        == 413
    )


def test_score_api_error_and_generic_error(server, monkeypatch):
    def api_error(payload):
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))

    monkeypatch.setattr(serve, "score", api_error)
    status, out = call(server, "/score", {"posting_text": "x"})
    assert status == 502 and "model call failed" in out["error"]

    def boom(payload):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(serve, "score", boom)
    status, out = call(server, "/score", {"posting_text": "x"})
    assert status == 500 and out["error"] == "internal scoring error"


def test_main_starts_server(monkeypatch):
    started = {}

    class FakeServer:
        def __init__(self, addr, handler):
            started["addr"] = addr
            started["handler"] = handler

        def serve_forever(self):
            started["ran"] = True

    monkeypatch.setattr(serve, "Server", FakeServer)
    assert serve.main() == 0
    assert started["ran"] and started["handler"] is serve.Handler
    assert started["addr"] == (serve.HOST, serve.PORT)


def test_score_and_digest_require_the_token(server, monkeypatch):
    monkeypatch.setattr(serve, "TOKEN", "s3cret")
    code, body = call(server, "/score", {"posting_text": "x"}, {"Authorization": ""})
    assert code == 401 and "token" in body["error"]
    assert call(server, "/digest", headers={"Authorization": ""})[0] == 401
    assert call(server, "/healthz", headers={"Authorization": ""})[0] == 200
    code, _ = call(server, "/score", {"posting_text": "x"}, {"Authorization": "Bearer wrong"})
    assert code == 401
    code, _ = call(server, "/score", {"posting_text": ""}, {"Authorization": "Bearer s3cret"})
    assert code == 400  # past the token check, then the usual input rules


def test_main_refuses_to_start_without_a_token_even_on_loopback(monkeypatch):
    monkeypatch.setattr(serve, "HOST", "127.0.0.1")
    monkeypatch.setattr(serve, "TOKEN", "")
    monkeypatch.setattr(serve, "Server", lambda *a: pytest.fail("must not listen"))
    assert serve.main() == 2


def test_the_cost_cap_bounds_the_whole_process_not_each_request(server, monkeypatch):
    budget = serve.llm.Usage()
    monkeypatch.setattr(serve, "BUDGET", budget)
    monkeypatch.setattr(budget, "can_call", lambda: False)  # the process already spent it
    monkeypatch.setattr(scoring.llm, "get_client", lambda: pytest.fail("no model call"))
    code, body = call(server, "/score", {"posting_text": "Python intern role"})
    assert code == 200 and "budget" in body["error"]


def test_scores_beyond_the_concurrency_limit_are_turned_away(server, monkeypatch):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()  # one score already running
    monkeypatch.setattr(serve, "SCORING", slots)
    monkeypatch.setattr(serve, "score", lambda _: pytest.fail("must not score"))
    assert call(server, "/score", {"posting_text": "x"})[0] == 429


def test_a_connection_that_trickles_headers_is_cut_at_the_deadline(server, monkeypatch):
    monkeypatch.setattr(serve, "CONNECTION_DEADLINE", 0.3)
    host, port = server.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(b"GET /healthz HTTP/1.1\r\nX-Slow: 1\r\n")
        time.sleep(0.6)
        assert sock.recv(100) == b""  # the server closed it


@pytest.mark.parametrize(
    "body,headers",
    [
        (b"{}", {"Content-Length": "-1"}),
        (b"{}", {"Content-Length": "0"}),
        (b"{}", {"Content-Length": "invalid"}),
        (b"{}", {"Content-Length": "+2"}),
        (b"[[[[", {}),
        (b"{}", {"Transfer-Encoding": "chunked"}),
        (b"\xff", {}),
        ({"posting_text": 123}, {}),
        ({"posting_text": " "}, {}),
        ({"posting_text": "x", "title": []}, {}),
    ],
)
def test_malformed_requests_do_not_reach_scoring(server, monkeypatch, body, headers):
    def forbidden(_):
        pytest.fail("Invalid input reached the model boundary")

    monkeypatch.setattr(serve, "score", forbidden)
    assert call(server, "/score", body, headers)[0] == 400


def test_refused_body_sent_after_reply_still_gets_the_reply(server):
    # A client may send its body after the server already refused it. The
    # server must read and drop that body, or the client gets a broken pipe.
    host, port = server.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(
            b"POST /score HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer test-token\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
        )
        sock.recv(1, socket.MSG_PEEK)  # the reply is out
        time.sleep(0.1)
        for _ in range(3):
            sock.sendall(b"2\r\n{}\r\n" * 1000)
        sock.shutdown(socket.SHUT_WR)
        reply = b""
        while chunk := sock.recv(65536):
            reply += chunk
    assert reply.startswith(b"HTTP/1.0 400") or reply.startswith(b"HTTP/1.1 400")
