"""Workday adapter: the public career site JSON behind *.myworkdayjobs.com.

The company slug is "tenant/wdN/site", read from a real posting URL:
https://capitalone.wd12.myworkdayjobs.com/Capital_One/job/... -> "capitalone/wd12/Capital_One".

Listing is newest first. Paging stops once postings are older than the
freshness window, and a posting's full text is fetched only when its title
and location could pass the filters, so a bank with thousands of openings
costs a few dozen requests, not thousands.
"""

from __future__ import annotations

import re
import time
from datetime import timedelta
from typing import Any

from .. import config, net
from ._common import Posting, clean_html

JOBS_API = "https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
PAGE = 20
MAX_PAGES = 15
MAX_DETAILS = 60
DETAIL_PAUSE_S = 0.3
HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}
_POSTED_RE = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.I)
# Loose on purpose: the exact rules run later in fetch_company. This only
# decides whether a posting is worth opening.
_WORTH_OPENING_RE = re.compile(
    r"intern|analyst|associate|graduate|campus|entry|junior|product|data|ai\b|machine"
    r"|strategy|operations|solutions|consult",
    re.I,
)


def parse_slug(slug: str) -> tuple[str, str, str]:
    tenant, wd, site = slug.split("/", 2)
    return tenant, wd, site


def days_ago(posted_on: str | None) -> int | None:
    """Workday's "Posted Today", "Posted 3 Days Ago", "Posted 30+ Days Ago"."""
    m = _POSTED_RE.search(posted_on or "")
    if not m:
        return None
    word = m.group(1).lower()
    if word == "today":
        return 0
    if word == "yesterday":
        return 1
    return int(m.group(2)) + (1 if "+" in m.group(0) else 0)


def _date_of(age: int | None) -> str | None:
    if age is None:
        return None
    return (config.utc_now() - timedelta(days=age)).date().isoformat()


def _location_could_fit(text: str, title: str = "") -> bool:
    # "3 Locations" hides the list; open it and let the location rule decide
    return bool(
        config.TARGET_RE.search(text)
        or config.REMOTE_RE.search(text)
        or re.search(r"\d+ locations", text, re.I)
        or (config.starts_in_wide_window({"title": title}) and config.in_the_us(text))
    )


def fetch_workday(slug: str) -> list[Posting]:
    tenant, wd, site = parse_slug(slug)
    base = JOBS_API.format(tenant=tenant, wd=wd, site=site)
    out: list[Posting] = []
    opened = 0
    for page in range(MAX_PAGES):
        resp = net.post(
            f"{base}/jobs",
            json={"appliedFacets": {}, "limit": PAGE, "offset": page * PAGE, "searchText": ""},
            headers=HEADERS,
            timeout=config.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        jobs = resp.json().get("jobPostings", [])
        stale = 0
        for j in jobs:
            age = days_ago(j.get("postedOn"))
            if age is not None and age > config.LISTING_SCAN_DAYS:  # the paging bound
                stale += 1
                continue
            title, where = j.get("title", ""), j.get("locationsText", "")
            posting: Posting = {
                "title": title,
                "location": where,
                "url": f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{j.get('externalPath', '')}",
                "description": "",
                "posted_at": _date_of(age),
            }
            if (
                opened < MAX_DETAILS
                and _WORTH_OPENING_RE.search(title)
                and not config.is_senior_title(title)
                and _location_could_fit(where, title)
            ):
                opened += 1
                time.sleep(DETAIL_PAUSE_S)
                posting.update(_detail(base, j.get("externalPath", "")))
            out.append(posting)
        if len(jobs) < PAGE or stale == len(jobs):
            break  # last page, or a whole page past the freshness window
    return out


def _detail(base: str, path: str) -> dict[str, Any]:
    resp = net.get(f"{base}{path}", headers=HEADERS, timeout=config.REQUEST_TIMEOUT)
    resp.raise_for_status()
    info = resp.json().get("jobPostingInfo", {})
    places = [info.get("location") or "", *(info.get("additionalLocations") or [])]
    return {
        "location": "; ".join(p for p in places if p),
        "description": clean_html(info.get("jobDescription", "")),
        "posted_at": (info.get("startDate") or "")[:10] or None,
        "url": info.get("externalUrl") or f"{base}{path}",
    }


def fetch_postings(company: dict[str, Any]) -> list[Posting]:
    """Postings on the company's Workday site, newest first, within freshness."""
    return fetch_workday(company["slug"])
