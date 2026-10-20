"""SmartRecruiters adapter using its public postings API."""

from __future__ import annotations

from typing import Any

from .. import config
from ._common import Posting, _get_json, clean_html

SMARTRECRUITERS_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"


def _location(value: dict[str, Any]) -> str:
    location = value.get("fullLocation") or ", ".join(
        str(value.get(key) or "") for key in ("city", "region", "country")
    ).strip(", ")
    if value.get("remote"):
        location = f"{location}; Remote" if location else "Remote"
    return location


def _description(payload: dict[str, Any]) -> str:
    sections = ((payload.get("jobAd") or {}).get("sections") or {}).values()
    return clean_html("\n".join(str(section.get("text") or "") for section in sections))


def fetch_smartrecruiters(slug: str) -> list[Posting]:
    out = []
    offset = 0
    while True:
        payload = _get_json(f"{SMARTRECRUITERS_API.format(slug=slug)}?limit=100&offset={offset}")
        rows = payload.get("content", [])
        for row in rows:
            location = _location(row.get("location") or {})
            posting: Posting = {
                "title": str(row.get("name") or "").strip(),
                "location": location,
                "url": str(row.get("ref") or ""),
                "description": "",
                "employment_type": ((row.get("typeOfEmployment") or {}).get("label") or "")
                .lower()
                .replace("-", " "),
                "posted_at": (row.get("releasedDate") or "")[:10] or None,
            }
            wide = config.starts_in_wide_window(posting)
            if config.location_in_scope(location, wide=wide) and not config.is_senior_title(
                posting["title"]
            ):
                detail = _get_json(posting["url"])
                posting["description"] = _description(detail)
                posting["url"] = detail.get("postingUrl") or posting["url"]
            out.append(posting)
        offset += len(rows)
        if not rows or offset >= int(payload.get("totalFound") or 0):
            break
    return out


def fetch_postings(company: dict[str, Any]) -> list[Posting]:
    """Every posting on the company's board, before any filtering."""
    return fetch_smartrecruiters(company["slug"])
