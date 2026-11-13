"""Helpers shared by the source adapters: HTTP, HTML cleanup, date parsing."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bs4 import BeautifulSoup

from .. import config, net
from ..safetext import multi_line, one_line

Posting = dict[str, Any]


# fields that may span lines; every other text field is kept on one line
MULTI_LINE_FIELDS = frozenset({"description"})


def safe_posting(posting: Posting) -> Posting:
    """The posting with control characters stripped from every text field.

    Applied once where postings enter, so no terminal escape or forged line
    break from a board reaches the database, the digest or the tracker.
    """
    for key, value in posting.items():
        if isinstance(value, str):
            posting[key] = multi_line(value) if key in MULTI_LINE_FIELDS else one_line(value)
        elif isinstance(value, list):
            posting[key] = [one_line(v) if isinstance(v, str) else v for v in value]
    return posting


def clean_html(html: str | None) -> str:
    text = BeautifulSoup(html or "", "html.parser").get_text("\n")
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)[: config.MAX_DESCRIPTION_CHARS]


def _get_json(url: str, timeout: int = config.REQUEST_TIMEOUT) -> Any:
    resp = net.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _get_text(url: str, timeout: int = config.REQUEST_TIMEOUT) -> str:
    resp = net.get(url, timeout=timeout, headers={"User-Agent": "Sourcer collector/0.3"})
    resp.raise_for_status()
    return str(resp.text)


def iso_from_ms(ms: int | float | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).date().isoformat()
