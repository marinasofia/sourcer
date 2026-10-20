"""Listing verification: prove each posting still exists at the source.

  python -m sourcer.apply.verify [--limit N] [--all]

Every stored URL points at the company's own careers or ATS page, so
verification means checking that page directly. Outcomes, written to the
``verified`` column:

  source    came straight from a company ATS board (Greenhouse/Lever/Ashby);
            verified at ingestion by construction
  verified  page fetched, job title found on it
  live      page fetched (200) on a recognized careers host, but the title
            could not be confirmed (JS-rendered page)
  closed    page says the role is closed / no longer accepting
  dead      404/410 or the ATS API says the posting is gone
  unknown   network error; retried next run

closed and dead listings are auto-archived with a note so they leave the
inbox. Workday URLs are checked against Workday's JSON API (the HTML shell
always returns 200, even for dead roles). No model calls; HTTP only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

import requests

from .. import db, log, net

logger = log.get_logger(__name__)

ATS_HOSTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "successfactors.com",
    "icims.com",
    "smartrecruiters.com",
    "oraclecloud.com",
    "workable.com",
    "bamboohr.com",
    "jobvite.com",
    "greenhouse.dev",
    "rippling.com",
    "gem.com",
)
CLOSED_RE = re.compile(
    r"(no longer accepting|job is no longer|position has been filled"
    r"|posting is closed|job not found|position[ \w]{0,20} closed"
    r"|this job has expired|requisition[ \w]{0,20} closed)",
    re.I,
)
UA = {"User-Agent": "Mozilla/5.0 (compatible; sourcer-verify)"}
WORKDAY_RE = re.compile(
    r"https://([^.]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?"
    r"([^/]+)/job/(?:[^/]+/)*([^/?#]+)"
)
GENERIC_TOKENS = {"intern", "engineer", "software", "senior"}
HTTP_TIMEOUT = 12
MAX_PAGE_CHARS = 400_000
WORKERS = 16
GONE_STATUSES = (404, 410)
WORKDAY_PULLED = 403  # Workday's answer for a job that was taken down

SELECT_PENDING = (
    "SELECT id, url, title, company FROM postings "
    "WHERE source='simplify' AND (verified IS NULL OR verified='unknown') LIMIT ?"
)
SELECT_ALL_LISTINGS = "SELECT id, url, title, company FROM postings WHERE source='simplify' LIMIT ?"


def title_tokens(title: str | None) -> list[str]:
    return [t for t in re.findall(r"[a-z]{4,}", (title or "").lower()) if t not in GENERIC_TOKENS][
        :3
    ]


def check_workday(url: str) -> str | None:
    """Workday verdict from its JSON API.

    None when the url is not a Workday url or the check was inconclusive.
    """
    m = WORKDAY_RE.match(url)
    if not m:
        return None
    tenant, wd, site, slug = m.groups()
    api = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{slug}"
    try:
        r = net.get(api, timeout=HTTP_TIMEOUT, headers=UA)
    except requests.RequestException:
        return None
    if r.status_code == GONE_STATUSES[0]:
        return "dead"
    if r.status_code == WORKDAY_PULLED and workday_board_answers(tenant, wd, site):
        return "closed"
    if r.ok and "jobPostingInfo" in r.text:
        return "verified"
    return None


def workday_board_answers(tenant: str, wd: str, site: str) -> bool:
    """Workday answers 403 for a job that was taken down and 404 for one that
    never existed. A 403 counts as taken down only when the same board's
    search still answers, since a block would refuse the search too."""
    try:
        r = net.post(
            f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
            json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
            headers={**UA, "Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException:
        return False
    return r.ok


def classify_page(row: Any, r: Any) -> str:
    """Verdict for a fetched page: closed text, title present, or a recognised careers host."""
    text = r.text[:MAX_PAGE_CHARS]
    if CLOSED_RE.search(text):
        return "closed"
    toks = title_tokens(row["title"])
    if toks and all(t in text.lower() for t in toks):
        return "verified"
    host = urlparse(str(r.url)).netloc.lower()
    company_token = re.sub(r"[^a-z0-9]", "", (row["company"] or "").lower())[:8]
    on_known_host = any(h in host for h in ATS_HOSTS)
    on_company_host = bool(company_token) and company_token in host.replace("-", "")
    return "live" if on_known_host or on_company_host else "unknown"


def check_one(row: Any) -> tuple[int, str]:
    url = row["url"]
    wd = check_workday(url)
    if wd:
        return row["id"], wd
    try:
        r = net.get(url, timeout=HTTP_TIMEOUT, headers=UA, allow_redirects=True)
    except requests.RequestException:
        return row["id"], "unknown"
    if r.status_code in GONE_STATUSES:
        return row["id"], "dead"
    if not r.ok:
        return row["id"], "unknown"
    return row["id"], classify_page(row, r)


def apply_results(conn: db.Connection, results: list[tuple[int, str]]) -> dict[str, int]:
    now = db.now_iso()
    counts: dict[str, int] = {}
    for pid, status in results:
        counts[status] = counts.get(status, 0) + 1
        conn.execute("UPDATE postings SET verified=?, verified_at=? WHERE id=?", (status, now, pid))
        if status in ("dead", "closed"):
            conn.execute(
                "UPDATE postings SET status='archived', "
                "notes='auto-archived: source link ' || ? WHERE id=? AND status='new'",
                (status, pid),
            )
    conn.commit()
    return counts


def main(argv: list[str] | None = None) -> int:
    log.configure()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--all", action="store_true", help="re-check already verified rows too")
    args = ap.parse_args(argv)

    conn = db.connect()
    conn.execute("UPDATE postings SET verified='source' WHERE source='ats' AND verified IS NULL")
    conn.commit()
    query = SELECT_ALL_LISTINGS if args.all else SELECT_PENDING
    rows = conn.execute(query, (args.limit,)).fetchall()
    if not rows:
        print("nothing to verify")
        return 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(check_one, rows))
    counts = apply_results(conn, results)
    print(
        f"verified {len(results)} listings in {round(time.time() - t0, 1)}s: " + json.dumps(counts)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
