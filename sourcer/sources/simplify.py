"""SimplifyJobs community lists: internship and new-grad listings, metadata only."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from typing import Any

import requests

from .. import config
from ..log import get_logger
from ._common import Posting, _get_json

log = get_logger(__name__)

SIMPLIFY_RAW = "https://raw.githubusercontent.com/{repo}/dev/.github/scripts/listings.json"
SIMPLIFY_TIMEOUT = 30

SIMPLIFY_LISTS: tuple[tuple[str, str, str | None], ...] = (
    # (repo, employment_type, start_date template)
    ("SimplifyJobs/Summer2027-Internships", "intern", None),
    ("SimplifyJobs/New-Grad-Positions", "full time", "new grad"),
)
# AI/data categories are admitted wholesale. "Software" is where boards file
# FDE and Solutions-type roles, "Product" holds associate PM and product
# analyst roles, and "Quant" holds some data and analyst roles, so those are
# admitted only for titles matching ROLE_RE.
SIMPLIFY_CATEGORIES = {"AI/ML/Data", "Data Science, AI & Machine Learning"}
SIMPLIFY_TITLE_GATED = {
    "Software",
    "Software Engineering",
    "Product",
    "Product Management",
    "Quant",
}

# the program term check lives in config; these names stay for callers that import them
SEASON_END = dict(config.TERM_END)
TERM_RE = config.TERM_RE


def term_is_current(term: str, now: datetime) -> bool:
    """False when the term names a season that has ended or ends this month:
    a listing for it has stopped hiring. "N/A", "new grad" and anything
    unparseable are kept."""
    next_month = date(now.year + now.month // 12, now.month % 12 + 1, 1)
    return config.program_ended(term, next_month) is None


def _simplify_entry(  # noqa: PLR0911
    e: dict[str, Any], etype: str, start_tpl: str | None, now: datetime
) -> Posting | None:
    """Map one SimplifyJobs row to a listing, or None when a gate rejects it."""
    if not (e.get("active") and e.get("is_visible")):
        return None
    cat, title = e.get("category"), e.get("title", "")
    if cat not in SIMPLIFY_CATEGORIES and not (
        cat in SIMPLIFY_TITLE_GATED and config.ROLE_RE.search(title)
    ):
        return None
    location = "; ".join(e.get("locations") or [])
    wide = config.starts_in_wide_window(
        {"title": title, "start_date": " ".join(e.get("terms") or [])}
    )
    if not config.location_in_scope(location, wide=wide):
        return None
    degrees = e.get("degrees") or []
    user_degree = os.environ.get("SOURCER_DEGREE", "Bachelor's")
    if degrees and user_degree not in degrees:
        return None
    terms = e.get("terms") or []
    if terms and not any(term_is_current(t, now) for t in terms):
        return None
    joined = ", ".join(terms)
    start = joined or start_tpl
    if start_tpl and joined:
        start = f"{start_tpl} {joined}"
    posted = e.get("date_posted")
    posted_at = datetime.fromtimestamp(posted, tz=UTC).date().isoformat() if posted else None
    if not config.is_fresh(posted_at, now=now):
        return None
    return {
        "title": title,
        "location": location,
        "url": e.get("url", ""),
        "description": "",
        "company": e.get("company_name", ""),
        "employment_type": etype,
        "start_date": start,
        "source": "simplify",
        "posted_at": posted_at,
    }


def fetch_simplify() -> list[Posting]:
    """Community-maintained internship and new-grad lists (SimplifyJobs).

    Metadata only (no posting text), so these records bypass the model
    entirely: employment type and start date come from the list itself,
    score stays NULL. They exist for discovery and alerts, not ranking,
    and never appear in the public showcase.
    """
    now = config.utc_now()
    out: list[Posting] = []
    errors: list[str] = []
    for repo, etype, start_tpl in SIMPLIFY_LISTS:
        try:
            rows = _get_json(SIMPLIFY_RAW.format(repo=repo), timeout=SIMPLIFY_TIMEOUT)
        except (requests.RequestException, ValueError) as exc:
            log.warning("simplify list %s unavailable: %s", repo, exc)
            errors.append(f"{repo}: {exc}")
            continue
        for e in rows:
            listing = _simplify_entry(e, etype, start_tpl, now)
            if listing is not None:
                out.append(listing)
    if errors and len(errors) == len(SIMPLIFY_LISTS):
        raise requests.RequestException("; ".join(errors))
    return out
