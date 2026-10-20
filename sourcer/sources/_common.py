"""Helpers shared by the source adapters: HTTP, HTML cleanup, date parsing."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bs4 import BeautifulSoup

from .. import config, net

Posting = dict[str, Any]


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
