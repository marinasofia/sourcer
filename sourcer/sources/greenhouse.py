"""Greenhouse adapter: the unauthenticated job board JSON API."""

from __future__ import annotations

from typing import Any

from ._common import Posting, _get_json, clean_html

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


def fetch_greenhouse(slug: str) -> list[Posting]:
    out = []
    for j in _get_json(GREENHOUSE_API.format(slug=slug)).get("jobs", []):
        out.append(
            {
                "title": j.get("title", ""),
                "location": (j.get("location") or {}).get("name", ""),
                "url": j.get("absolute_url", ""),
                "description": clean_html(j.get("content", "")),
                "posted_at": (j.get("first_published") or j.get("updated_at") or "")[:10] or None,
            }
        )
    return out


def fetch_postings(company: dict[str, Any]) -> list[Posting]:
    """Every posting on the company's board, before any filtering."""
    return fetch_greenhouse(company["slug"])
