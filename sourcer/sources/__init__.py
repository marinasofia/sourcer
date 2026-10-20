"""Source adapters: one module per ATS, plus the SimplifyJobs community lists.

Greenhouse, Lever, and Ashby expose unauthenticated JSON APIs, so no HTML
scraping of listing pages is needed. Each adapter exposes
``fetch_postings(company)`` returning every posting on the board;
``fetch_company`` below applies the pre-filter (role title + location) so the
model never sees clearly out-of-scope postings. Workable, SmartRecruiters,
Rippling, Workday, Eightfold, Oracle Recruiting, and optional static
careers pages are registered adapters.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

import requests

from .. import config
from ..log import get_logger
from . import (
    ashby,
    careers_page,
    eightfold,
    greenhouse,
    lever,
    oracle,
    rippling,
    simplify,
    smartrecruiters,
    workable,
    workday,
)
from ._common import Posting, _get_json, _get_text, clean_html, iso_from_ms
from .ashby import ASHBY_API, fetch_ashby
from .greenhouse import GREENHOUSE_API, fetch_greenhouse
from .lever import LEVER_API, fetch_lever
from .simplify import (
    SEASON_END,
    SIMPLIFY_CATEGORIES,
    SIMPLIFY_LISTS,
    SIMPLIFY_RAW,
    SIMPLIFY_TIMEOUT,
    SIMPLIFY_TITLE_GATED,
    TERM_RE,
    _simplify_entry,
    fetch_simplify,
    term_is_current,
)

__all__ = [
    "ASHBY_API",
    "FETCHERS",
    "GREENHOUSE_API",
    "LEVER_API",
    "SEASON_END",
    "SIMPLIFY_CATEGORIES",
    "SIMPLIFY_LISTS",
    "SIMPLIFY_RAW",
    "SIMPLIFY_TIMEOUT",
    "SIMPLIFY_TITLE_GATED",
    "TERM_RE",
    "Posting",
    "_get_json",
    "_get_text",
    "_simplify_entry",
    "ashby",
    "careers_page",
    "clean_html",
    "fetch_ashby",
    "fetch_company",
    "fetch_greenhouse",
    "fetch_lever",
    "fetch_postings",
    "fetch_simplify",
    "greenhouse",
    "in_scope",
    "iso_from_ms",
    "lever",
    "requests",
    "rippling",
    "simplify",
    "smartrecruiters",
    "term_is_current",
    "workable",
]

log = get_logger(__name__)

Fetcher = Callable[[dict[str, Any]], list[Posting]]

FETCHERS: dict[str, Fetcher] = {
    "greenhouse": greenhouse.fetch_postings,
    "lever": lever.fetch_postings,
    "ashby": ashby.fetch_postings,
    "careers_page": careers_page.fetch_postings,
    "workable": workable.fetch_postings,
    "smartrecruiters": smartrecruiters.fetch_postings,
    "rippling": rippling.fetch_postings,
    "workday": workday.fetch_postings,
    "eightfold": eightfold.fetch_postings,
    "oracle": oracle.fetch_postings,
}


def fetch_postings(company: dict[str, Any]) -> list[Posting]:
    """Dispatch to the adapter for the company's ATS type."""
    return FETCHERS[company["ats"]](company)


def in_scope(posting: Posting) -> bool:
    return bool(
        config.ROLE_RE.search(posting["title"])
        and config.location_in_scope(
            posting["location"],
            posting.get("description") or "",
            wide=config.starts_in_wide_window(posting),
        )
    )


def screen(p: Posting, entry: dict[str, Any], profile: dict[str, Any] | None = None) -> str:
    """The first gate a posting fails, in the order the search applies them, or
    "kept" (the title names a target role) or "title_unsure" (the title goes to
    the batched title check). One function, so the search and any "why was
    this missed" question always agree."""
    description = p.get("description") or ""
    wide = config.starts_in_wide_window(p)
    gates = (
        # no date or "stale" gate: a role listed on the company's own board is
        # open; its age only lowers its rank (config.age_penalty). An undated
        # role once dropped here was squarely in scope (gold set, 2026-10-10).
        ("staffing_agency", lambda: config.STAFFING_AGENCY_RE.search(entry.get("company") or "")),
        ("gig_work", lambda: config.GIG_TITLE_RE.search(p["title"])),
        ("title_location", lambda: config.title_location_conflict(p["title"], wide)),
        ("location", lambda: not config.location_in_scope(p["location"], description, wide)),
        ("above_entry", lambda: config.target_window_evidence(p) == "no"),
    )
    failed = next((name for name, fails in gates if fails()), None)
    if failed == "location":
        # the posting's own words when a description clause decided it
        quote = config.description_location_conflict(description, wide)
        if quote:
            p["_gate_evidence"] = quote
    if failed is None:
        found = config.eligibility_evidence(description, profile, p["title"])
        if found:
            failed, p["_gate_evidence"] = found
    if failed:
        return failed
    p["company"] = entry["company"]
    # Every title goes to the cheap batched title check, which reads it against
    # the work she wants in her own words: no list of role names to go stale
    # (senior titles were already dropped above as above entry level).
    return "title_unsure"


def fetch_company(
    entry: dict[str, Any],
    *,
    profile: dict[str, Any] | None = None,
    reasons: Counter[str] | None = None,
    dropped: list[tuple[Posting, str]] | None = None,
) -> tuple[list[Posting], list[Posting], int]:
    """Returns (in-scope postings, title-unsure postings, count prefiltered).

    Title-unsure postings pass the location gate but not the title regex;
    the source node sends their titles through one batched triage call
    instead of dropping them, so semantically matching roles the regex never
    anticipated still get scored.
    """
    raw = fetch_postings(entry)
    kept: list[Posting] = []
    unsure: list[Posting] = []
    counts: Counter[str] = reasons if reasons is not None else Counter()
    for p in raw:
        verdict = screen(p, entry, profile)
        if verdict == "kept":
            kept.append(p)
        elif verdict == "title_unsure":
            unsure.append(p)
        else:
            counts[verdict] += 1
            if dropped is not None:  # for the trace: which gate set it aside
                dropped.append((p, verdict))
    log.debug("%s: %d raw, %d kept, %d unsure", entry["company"], len(raw), len(kept), len(unsure))
    return kept, unsure, len(raw) - len(kept) - len(unsure)
