"""Ashby adapter: the unauthenticated job board posting API."""

from __future__ import annotations

from typing import Any

from ._common import Posting, _get_json, clean_html

ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def fetch_ashby(slug: str) -> list[Posting]:
    out = []
    for j in _get_json(ASHBY_API.format(slug=slug)).get("jobs", []):
        if str(j.get("isListed", "true")).lower() == "false":
            continue
        locs = [j.get("location") or ""] + [
            s.get("location", "") for s in (j.get("secondaryLocations") or [])
        ]
        if j.get("isRemote"):
            # Bare "Remote": isRemote says nothing about country. The gate in
            # config.location_in_scope decides using the full location list,
            # so "Tokyo, Japan" + isRemote no longer masquerades as Remote US.
            locs.append("Remote")
        out.append(
            {
                "title": j.get("title", "").strip(),
                "location": "; ".join(x for x in locs if x),
                "url": j.get("jobUrl", ""),
                "description": clean_html(
                    j.get("descriptionPlain") or j.get("descriptionHtml") or ""
                ),
                "posted_at": (j.get("publishedAt") or "")[:10] or None,
            }
        )
    return out


def fetch_postings(company: dict[str, Any]) -> list[Posting]:
    """Every posting on the company's board, before any filtering."""
    return fetch_ashby(company["slug"])
