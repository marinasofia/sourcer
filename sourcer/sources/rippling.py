"""Rippling ATS adapter for server-rendered public boards."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import config
from ._common import Posting, _get_text, clean_html

RIPPLING_BOARD = "https://ats.rippling.com/{slug}/jobs"


def fetch_rippling(slug: str) -> list[Posting]:
    board = RIPPLING_BOARD.format(slug=slug)
    soup = BeautifulSoup(_get_text(board), "html.parser")
    out = []
    seen = set()
    pattern = re.compile(rf"^/{re.escape(slug)}/jobs/[a-f0-9-]+$", re.I)
    for anchor in soup.find_all("a", href=pattern):
        href = str(anchor.get("href"))
        if href in seen or not anchor.get_text(" ", strip=True):
            continue
        seen.add(href)
        title = anchor.get_text(" ", strip=True)
        card = anchor.parent.parent.parent
        labels = [item.get_text(" ", strip=True) for item in card.find_all("p")]
        location = labels[-1] if labels else ""
        url = urljoin(board, href)
        description = ""
        wide = config.starts_in_wide_window({"title": title})
        if config.location_in_scope(location, wide=wide) and not config.is_senior_title(title):
            description = clean_html(_get_text(url))
        out.append(
            {
                "title": title,
                "location": location,
                "url": url,
                "description": description,
                "posted_at": None,
            }
        )
    return out


def fetch_postings(company: dict[str, Any]) -> list[Posting]:
    return fetch_rippling(company["slug"])
