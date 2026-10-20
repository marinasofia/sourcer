"""Workable adapter using its public, structured Markdown job feeds."""

from __future__ import annotations

import re
from typing import Any

from .. import config
from ._common import Posting, _get_text

WORKABLE_JOBS = "https://apply.workable.com/{slug}/jobs.md"
WORKABLE_QUERIES = ("ai", "data", "machine learning", "analytics", "intern")
TABLE_ROW_RE = re.compile(
    r"^\|\s*(?P<title>.*?)\s*\|\s*(?P<department>.*?)\s*\|\s*"
    r"(?P<location>.*?)\s*\|\s*(?P<kind>.*?)\s*\|\s*.*?\|\s*"
    r"(?P<posted>\d{4}-\d{2}-\d{2})\s*\|\s*"
    r"\[View\]\((?P<details>https://apply\.workable\.com/[^)]+)\)\s*\|$"
)
APPLY_RE = re.compile(r"\[Apply[^]]*\]\((https://apply\.workable\.com/[^)]+)\)")


def _rows(markdown: str) -> list[Posting]:
    out = []
    for line in markdown.splitlines():
        match = TABLE_ROW_RE.match(line.strip())
        if not match:
            continue
        values = match.groupdict()
        out.append(
            {
                "title": values["title"].strip(),
                "location": values["location"].strip(),
                "url": values["details"],
                "description": "",
                "employment_type": values["kind"].lower().replace("-", " "),
                "posted_at": values["posted"],
            }
        )
    return out


def _detail(posting: Posting) -> Posting:
    if not (
        config.location_in_scope(posting["location"], wide=config.starts_in_wide_window(posting))
        and not config.is_senior_title(posting["title"])
    ):
        return posting
    markdown = _get_text(posting["url"])
    apply = APPLY_RE.search(markdown)
    posting["description"] = markdown[: config.MAX_DESCRIPTION_CHARS]
    if apply:
        posting["url"] = apply.group(1)
    return posting


def fetch_workable(slug: str) -> list[Posting]:
    base = WORKABLE_JOBS.format(slug=slug)
    postings = _rows(_get_text(base))
    if not postings:
        for query in WORKABLE_QUERIES:
            postings.extend(_rows(_get_text(f"{base}?query={query.replace(' ', '+')}")))
    unique = {posting["url"]: posting for posting in postings}
    return [_detail(posting) for posting in unique.values()]


def fetch_postings(company: dict[str, Any]) -> list[Posting]:
    """Every posting on the company's board, before any filtering."""
    return fetch_workable(company["slug"])
