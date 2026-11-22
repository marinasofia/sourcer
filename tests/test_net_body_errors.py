"""A connection dropped mid body reaches callers as a requests error (Nov 20 2026:
a raw urllib3 ProtocolError from one website lookup failed all of discover), and
one company that raises never stops the website backfill or the probe."""

import pytest
import requests
from urllib3.exceptions import ProtocolError, ReadTimeoutError

from sourcer import net
from sourcer.discovery import websites


class _Raw:
    def __init__(self, error):
        self.error = error

    def read1(self, n, decode_content=True):
        raise self.error


class _Response:
    def __init__(self, error):
        self.raw = _Raw(error)
        self.url = "https://example.com"

    def close(self):
        pass


@pytest.mark.parametrize(
    "error, kind",
    [
        (
            ProtocolError("Connection broken", ConnectionResetError(54, "reset")),
            requests.exceptions.ChunkedEncodingError,
        ),
        (
            ReadTimeoutError(None, "https://example.com", "read timed out"),
            requests.exceptions.ConnectionError,
        ),
        (ConnectionResetError(54, "reset"), requests.exceptions.ConnectionError),
    ],
)
def test_a_body_read_error_is_a_request_error(error, kind):
    with pytest.raises(kind) as got:
        net._capped(_Response(error))
    assert isinstance(got.value, requests.RequestException)


def test_one_failing_company_never_stops_the_backfill(monkeypatch, tmp_path):
    rows = [{"name": "Bad Co"}, {"name": "Good Co"}]
    saved = []
    monkeypatch.setattr(websites.db, "list_companies", lambda conn: rows)
    monkeypatch.setattr(
        websites.db, "save_company_domain", lambda conn, name, domain: saved.append(name)
    )
    monkeypatch.setattr(websites, "due", lambda row, moment: True)

    def find(row, client, throttle):
        if row["name"] == "Bad Co":
            raise RuntimeError("boom")
        return websites.WebsiteResult(row["name"], "good.example", None, 1)

    monkeypatch.setattr(websites, "find_website", find)
    got = websites.backfill_domains(None, limit=5, workers=2, requester=object())
    assert [r.name for r in got] == ["Good Co"] and saved == ["Good Co"]
