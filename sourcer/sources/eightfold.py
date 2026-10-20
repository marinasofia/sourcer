"""Eightfold adapter: the public search behind *.eightfold.ai career sites.

The company slug is "tenant/domain", for example "paypal/paypal.com": the
tenant names the site and the domain is the company the site serves. The
search returns ten postings a page, newest first, so paging stops at the
freshness window. Full text is fetched only for titles and places that could
pass the filters, as with Workday.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from .. import config, net
from ._common import Posting, clean_html
from .workday import _WORTH_OPENING_RE, _location_could_fit

SEARCH_API = "https://{tenant}.eightfold.ai/api/pcsx/search"
DETAIL_API = "https://{tenant}.eightfold.ai/api/pcsx/position_details"
PAGE = 10  # the most the search returns per call
MAX_PAGES = 40
MAX_DETAILS = 60
DETAIL_PAUSE_S = 0.3


def _day(ts: Any) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=UTC).date().isoformat()


def _get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    resp = net.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict) or "data" not in payload:
        raise ValueError(f"eightfold: unexpected reply from {url}")
    return dict(payload["data"])


def fetch_eightfold(slug: str) -> list[Posting]:
    tenant, domain = slug.split("/", 1)
    out: list[Posting] = []
    opened = 0
    for page in range(MAX_PAGES):
        data = _get(
            SEARCH_API.format(tenant=tenant),
            {"domain": domain, "query": "", "location": "", "start": page * PAGE,
             "num": PAGE, "sort_by": "timestamp"},
        )  # fmt: skip
        jobs = data.get("positions") or []
        stale = 0
        for j in jobs:
            posted = _day(j.get("postedTs"))
            age = config.posting_age_days(posted)
            if age is not None and age > config.LISTING_SCAN_DAYS:  # the paging bound
                stale += 1
                continue
            title, where = j.get("name", ""), "; ".join(j.get("locations") or [])
            posting: Posting = {
                "title": title,
                "location": where,
                "url": f"https://{tenant}.eightfold.ai/careers/job/{j.get('id')}",
                "description": "",
                "posted_at": posted,
            }
            if (
                opened < MAX_DETAILS
                and _WORTH_OPENING_RE.search(title)
                and not config.is_senior_title(title)
                and _location_could_fit(where)
            ):
                opened += 1
                time.sleep(DETAIL_PAUSE_S)
                detail = _get(
                    DETAIL_API.format(tenant=tenant), {"position_id": j.get("id"), "domain": domain}
                )
                posting["description"] = clean_html(detail.get("jobDescription", ""))
                posting["url"] = detail.get("publicUrl") or posting["url"]
            out.append(posting)
        if len(jobs) < PAGE or stale == len(jobs):
            break  # last page, or a whole page past the freshness window
    return out


def fetch_postings(company: dict[str, Any]) -> list[Posting]:
    """Postings on the company's Eightfold site, newest first, within freshness."""
    return fetch_eightfold(company["slug"])
