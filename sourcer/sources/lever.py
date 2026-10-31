"""Lever adapter: the unauthenticated postings JSON API."""

from __future__ import annotations

from typing import Any

from ._common import Posting, _get_json, clean_html, iso_from_ms

LEVER_API = "https://api.lever.co/v0/postings/{slug}?mode=json"


def _text(j: dict[str, Any]) -> str:
    """The whole posting: its description, then each list (requirements,
    responsibilities) under its heading, then the closing text, where many
    boards put eligibility lines such as a US person requirement."""
    parts = [clean_html(j.get("descriptionPlain") or j.get("description", ""))]
    for block in j.get("lists") or []:
        if isinstance(block, dict):
            parts.append(clean_html(str(block.get("text") or "")))
            parts.append(clean_html(str(block.get("content") or "")))
    parts.append(clean_html(j.get("additionalPlain") or j.get("additional", "")))
    return "\n".join(p for p in parts if p)


def fetch_lever(slug: str) -> list[Posting]:
    out = []
    for j in _get_json(LEVER_API.format(slug=slug)):
        cats = j.get("categories") or {}
        out.append(
            {
                "title": j.get("text", ""),
                "location": cats.get("location", "") or "",
                "url": j.get("hostedUrl", ""),
                "description": _text(j),
                "posted_at": iso_from_ms(j.get("createdAt")),
            }
        )
    return out


def fetch_postings(company: dict[str, Any]) -> list[Posting]:
    """Every posting on the company's board, before any filtering."""
    return fetch_lever(company["slug"])
