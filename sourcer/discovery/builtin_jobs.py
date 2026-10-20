"""Built In NYC job listings as leads, each confirmed on the company's own board.

Built In's robots.txt allows the first three pages of a job listing
(``/jobs/...?page=1`` to ``3``), every ``/job/...`` page, and company pages; it
forbids searching, filters, and deeper paging. Its apply button needs a login,
so it never reveals the employer's board. A lead is therefore confirmed the
long way round:

1. read the listings for the role families she wants; each job card already
   names the role, the employer, and the employer's Built In page, so job
   pages are never opened;
2. find the employer's board: the watchlist first, else the website on the
   employer's Built In page, probed for a hosted board;
3. look for the same role on that board. Found means verified: the role is
   real, open, and Sourcer's daily search will read it from the source.

A company whose board is found is added to the watchlist by the caller.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from html import unescape
from typing import Any
from urllib.parse import urlparse

BASE = "https://www.builtinnyc.com"
# her role families first, then internship and entry level, then the AI
# industry list (every role at AI companies, there to find new employers)
LISTINGS = (
    "dev-engineering",
    "dev-engineering/machine-learning",
    "dev-engineering/artificial-intelligence",
    "data-analytics",
    "data-analytics/data-science",
    "product",
    "internships",
    "entry-level",
    "remote/dev-engineering",
    "artificial-intelligence",
)
PAGES = (1, 2, 3)  # robots.txt allows these three and nothing deeper
# a job card: its id, then the employer link and name, then the role link
CARD = re.compile(
    r'id="job-card-(\d+)".*?'
    r'<a href="(/company/[^"]+)"[^>]*data-id="company-title"[^>]*>\s*<span>(.*?)</span>'
    r'.*?<a href="(/job/[^"]+)"[^>]*data-id="job-card-title"[^>]*>(.*?)</a>',
    re.S,
)
RATE_LIMITED = 429
# Built In answers 429 at one request a second; a client must wait at least this long
MIN_INTERVAL = 3.0
LD_JSON = re.compile(r'<script type="application/ld(?:\+|&#x2B;)json">(.*?)</script>', re.S)

# a lead's outcome, in the order they are tried
VERIFIED = "verified"  # the same role is on the employer's own board
NOT_LISTED = "not_listed"  # the board answers but does not list this role
BOARD_ONLY = "board_found"  # board found, role not checked (paged boards)
NO_BOARD = "no_board"  # no hosted board found for the employer
UNCHECKED = "unchecked"  # over this run's probe budget; tried again tomorrow


@dataclass
class JobLead:
    job_id: str
    title: str
    company: str
    builtin_url: str
    posted: str | None = None
    location: str = ""
    company_page: str | None = None
    ats: str | None = None
    slug: str | None = None
    official_url: str | None = None
    status: str = UNCHECKED
    notes: list[str] = field(default_factory=list)


def listing_urls() -> list[str]:
    return [f"{BASE}/jobs/{path}" + ("" if page == 1 else f"?page={page}")
            for path in LISTINGS for page in PAGES]  # fmt: skip


def _ld_objects(html: str) -> list[dict[str, Any]]:
    """Every JSON-LD object on the page, graphs flattened."""
    out: list[dict[str, Any]] = []
    for block in LD_JSON.findall(html or ""):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        items = data.get("@graph", [data]) if isinstance(data, dict) else data
        out.extend(x for x in items if isinstance(x, dict))
    return out


def parse_company_domain(html: str) -> str | None:
    """The employer's own website from its Built In company page."""
    for obj in _ld_objects(html):
        if obj.get("@type") == "Organization" and obj.get("url"):
            host = urlparse(str(obj["url"])).hostname or ""
            host = host.removeprefix("www.")
            if host and "builtin" not in host:
                return host
    return None


def _words(title: str) -> list[str]:
    title = re.sub(r"\([^)]*\)", " ", title.lower())
    return re.findall(r"[a-z0-9+#]+", title)


def same_role(a: str, b: str) -> bool:
    """Whether two titles name the same role: equal words, or nearly all shared."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    if wa == wb:
        return True
    sa, sb = set(wa), set(wb)
    return len(sa & sb) / len(sa | sb) >= 0.8


def best_match(title: str, postings: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The board posting for this role: an exact title first, parentheses
    included, so "Backend (Platform)" never lands on "Backend (Product)";
    else the closest title that same_role accepts."""
    exact = [
        p
        for p in postings
        if re.findall(r"[a-z0-9+#]+", (p.get("title") or "").lower())
        == re.findall(r"[a-z0-9+#]+", title.lower())
    ]
    if exact:
        return exact[0]
    near = [p for p in postings if same_role(title, p.get("title") or "")]
    return near[0] if len(near) == 1 else None


def parse_cards(html: str) -> list[JobLead]:
    """Every job card on a listing page: role, employer, and employer page."""
    out = []
    for job_id, company_path, company, job_path, title in CARD.findall(html or ""):
        out.append(
            JobLead(
                job_id=job_id,
                title=unescape(re.sub(r"<[^>]+>", "", title)).strip(),
                company=unescape(re.sub(r"<[^>]+>", "", company)).strip(),
                builtin_url=f"{BASE}{job_path.split('?')[0]}",
                company_page=f"{BASE}{company_path}",
            )
        )
    return out


def _rate_limited(exc: Exception) -> bool:
    return getattr(getattr(exc, "response", None), "status_code", None) == RATE_LIMITED


def collect(client: Any, known: set[str], max_jobs: int = 400) -> list[JobLead]:
    """New jobs from the listing pages, read from their cards. A rate limit
    answer ends the reading for today: what was read is kept."""
    interval = getattr(client, "interval", None)
    if interval is not None and interval < MIN_INTERVAL:
        raise ValueError(f"Built In needs at least {MIN_INTERVAL} seconds between requests")
    leads: dict[str, JobLead] = {}
    for url in listing_urls():
        try:
            cards = parse_cards(client.get(url).text)
        except (OSError, ValueError) as exc:  # requests errors are OSErrors
            if _rate_limited(exc):
                break
            continue
        for lead in cards:
            if lead.job_id not in known:
                leads.setdefault(lead.job_id, lead)
    return list(leads.values())[:max_jobs]


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower().removesuffix(".com"))


def verify(
    leads: Iterable[JobLead],
    *,
    companies: Iterable[dict[str, Any]],
    fetch_board: Callable[[dict[str, Any]], list[dict[str, Any]]],
    find_board: Callable[[str, str | None], tuple[str | None, str | None]],
    company_domain: Callable[[str], str | None],
    max_probes: int = 25,
) -> None:
    """Find each lead's employer board and look for the role on it, in place.

    ``companies`` are the watchlist rows; ``fetch_board`` returns a board's
    postings; ``find_board(name, domain)`` probes for (ats, slug);
    ``company_domain(url)`` reads the employer's website from its Built In page.
    """
    watched = {
        _key(str(c["name"])): c for c in companies if c.get("ats_type") and c.get("ats_slug")
    }
    boards: dict[tuple[str, str], list[dict[str, Any]] | None] = {}
    probed: dict[str, tuple[str | None, str | None]] = {}
    probes = 0
    for lead in leads:
        row = watched.get(_key(lead.company))
        if row is not None:
            lead.ats, lead.slug = row["ats_type"], row["ats_slug"]
        elif _key(lead.company) in probed:
            lead.ats, lead.slug = probed[_key(lead.company)]
        elif probes < max_probes:
            probes += 1
            domain = company_domain(lead.company_page) if lead.company_page else None
            lead.ats, lead.slug = probed[_key(lead.company)] = find_board(lead.company, domain)
        else:
            lead.status = UNCHECKED
            continue
        if not (lead.ats and lead.slug):
            lead.status = NO_BOARD
            continue
        if lead.ats in {
            "workday",
            "oracle",
            "eightfold",
        }:  # paged boards: too costly to scan per lead
            lead.status = BOARD_ONLY
            continue
        board_key = (lead.ats, lead.slug)
        if board_key not in boards:
            try:
                boards[board_key] = fetch_board(
                    {"company": lead.company, "ats": lead.ats, "slug": lead.slug}
                )
            except (OSError, ValueError, KeyError):
                boards[board_key] = None
        postings = boards[board_key]
        if postings is None:
            lead.status = NO_BOARD
            lead.notes.append("the board did not answer")
            continue
        hit = best_match(lead.title, postings)
        lead.status = VERIFIED if hit else NOT_LISTED
        lead.official_url = hit.get("url") if hit else None
