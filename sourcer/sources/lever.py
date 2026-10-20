"""Lever adapter: the unauthenticated postings JSON API."""

from __future__ import annotations

from typing import Any

from ._common import Posting, _get_json, clean_html, iso_from_ms

LEVER_API = "https://api.lever.co/v0/postings/{slug}?mode=json"


def fetch_lever(slug: str) -> list[Posting]:
    out = []
    for j in _get_json(LEVER_API.format(slug=slug)):
        cats = j.get("categories") or {}
        out.append(
            {
                "title": j.get("text", ""),
                "location": cats.get("location", "") or "",
                "url": j.get("hostedUrl", ""),
                "description": clean_html(j.get("descriptionPlain") or j.get("description", "")),
                "posted_at": iso_from_ms(j.get("createdAt")),
            }
        )
    return out


def fetch_postings(company: dict[str, Any]) -> list[Posting]:
    """Every posting on the company's board, before any filtering."""
    return fetch_lever(company["slug"])
