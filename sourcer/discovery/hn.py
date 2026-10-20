"""Hacker News Who is Hiring discovery through the public Algolia API."""

from __future__ import annotations

import re
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup

from .http import DiscoveryClient
from .models import CompanyCandidate

HN_API = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM = "https://news.ycombinator.com/item?id={item_id}"
TARGET_RE = re.compile(
    r"\b(new york|nyc|remote|intern|new grad|machine learning|\bai\b|data|fintech)\b",
    re.I,
)
GENERIC_NAMES = {"company", "hiring", "remote", "we are hiring", "who is hiring"}


def _search_url(params: dict[str, str | int]) -> str:
    return f"{HN_API}?{urlencode(params)}"


def _domain_from_comment(soup: BeautifulSoup) -> str | None:
    for anchor in soup.find_all("a", href=True):
        host = urlparse(str(anchor.get("href"))).hostname
        if host and not host.endswith(("ycombinator.com", "hn.algolia.com")):
            return host.lower().removeprefix("www.")
    return None


def parse_comments(rows: list[dict[str, object]]) -> list[CompanyCandidate]:
    out = []
    for row in rows:
        html = str(row.get("comment_text") or "")
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text("\n", strip=True)
        if not text or not TARGET_RE.search(text):
            continue
        first_line = text.splitlines()[0]
        name = re.split(r"\s*[|•]\s*", first_line, maxsplit=1)[0].strip()
        name = re.sub(r"^(?:hiring:?|company:?)\s*", "", name, flags=re.I)
        if not name or len(name) > 100 or name.casefold() in GENERIC_NAMES:
            continue
        item_id = row.get("objectID")
        out.append(
            CompanyCandidate(
                name=name,
                domain=_domain_from_comment(soup),
                source="hn_who_is_hiring",
                source_url=HN_ITEM.format(item_id=item_id),
                hiring=True,
                tags=("who-is-hiring",),
            )
        )
    return out


def fetch(client: DiscoveryClient) -> list[CompanyCandidate]:
    stories = client.get_json(
        _search_url(
            {
                "query": "Ask HN: Who is hiring?",
                "tags": "story",
                "hitsPerPage": 1,
            }
        )
    ).get("hits", [])
    if not stories:
        return []
    story_id = stories[0].get("objectID")
    comments = client.get_json(
        _search_url(
            {
                "tags": f"comment,story_{story_id}",
                "hitsPerPage": 1000,
            }
        )
    ).get("hits", [])
    return parse_comments(comments)
