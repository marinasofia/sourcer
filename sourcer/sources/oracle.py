"""Oracle Recruiting adapter: the public REST search behind Oracle Cloud career sites.

The company slug is "host/site", for example
"egug.fa.us2.oraclecloud.com/CX_1": the tenant's own host and its career site
number (CX_1 on most tenants). Listing is newest first by posting date, so
paging stops at the freshness window, and full text is fetched only for
titles and places that could pass the filters, as with Workday.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .. import config, net
from ._common import Posting, clean_html
from .workday import _WORTH_OPENING_RE, _location_could_fit

API = "https://{host}/hcmRestApi/resources/latest"
PAGE = 25
MAX_PAGES = 20
MAX_DETAILS = 60
DETAIL_PAUSE_S = 0.3
HEADERS = {"Accept": "application/json"}
# a tenant host is letters, digits, dots and dashes under oraclecloud.com; the
# slug can come from a careers page, so nothing else may reach the URL
HOST_RE = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.oraclecloud\.com")
SITE_RE = re.compile(r"[\w-]+")


def _items(url: str) -> list[dict[str, Any]]:
    resp = net.get(url, headers=HEADERS, timeout=config.REQUEST_TIMEOUT)
    resp.raise_for_status()
    items = resp.json().get("items")
    if not isinstance(items, list):
        raise ValueError(f"oracle: unexpected reply from {url}")
    return items


SHORT_TEXT = 400  # under this the posting's own fields say too little to score


def _detail(host: str, site: str, req_id: str) -> dict[str, Any]:
    url = (
        f"{API.format(host=host)}/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
        f'&finder=ById;Id="{req_id}",siteNumber={site}'
    )
    items = _items(url)
    info = items[0] if items else {}
    fields = ["ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr"]
    text = clean_html(" ".join(str(info.get(k) or "") for k in fields))
    if len(text) < SHORT_TEXT:
        # some employers (Goldman's campus roles) leave these empty and write
        # the role into the division and program blurbs instead
        more = ("OrganizationDescriptionStr", "CorporateDescriptionStr")
        text = clean_html(" ".join(str(info.get(k) or "") for k in fields + list(more)))
    places = [info.get("PrimaryLocation") or ""] + [
        str(loc.get("Name") or "") for loc in info.get("secondaryLocations") or []
    ]
    return {"description": text, "location": "; ".join(p for p in places if p)}


def fetch_oracle(slug: str) -> list[Posting]:
    host, site = slug.split("/", 1)
    if not HOST_RE.fullmatch(host) or not SITE_RE.fullmatch(site):
        raise ValueError(f"oracle: not a tenant host and site: {slug}")
    out: list[Posting] = []
    opened = 0
    for page in range(MAX_PAGES):
        url = (
            f"{API.format(host=host)}/recruitingCEJobRequisitions?onlyData=true"
            f"&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={site},"
            f"limit={PAGE},offset={page * PAGE},sortBy=POSTING_DATES_DESC"
        )
        items = _items(url)
        jobs = (items[0].get("requisitionList") if items else None) or []
        stale = 0
        for j in jobs:
            posted = (j.get("PostedDate") or "")[:10] or None
            age = config.posting_age_days(posted)
            if age is not None and age > config.LISTING_SCAN_DAYS:  # the paging bound
                stale += 1
                continue
            title = j.get("Title", "")
            others = [str(loc.get("Name") or "") for loc in j.get("secondaryLocations") or []]
            where = "; ".join(p for p in [j.get("PrimaryLocation") or "", *others] if p)
            posting: Posting = {
                "title": title,
                "location": where,
                "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{j.get('Id')}",
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
                detail = _detail(host, site, str(j.get("Id")))
                posting["description"] = detail["description"]
                posting["location"] = detail["location"] or where
            out.append(posting)
        if len(jobs) < PAGE or stale == len(jobs):
            break  # last page, or a whole page past the freshness window
    return out


def fetch_postings(company: dict[str, Any]) -> list[Posting]:
    """Postings on the company's Oracle career site, newest first, within freshness."""
    return fetch_oracle(company["slug"])
