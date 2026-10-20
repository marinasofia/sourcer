"""Company discovery records, normalization, quality gates, and merging."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlparse

LEGAL_SUFFIX_RE = re.compile(
    r"\s+(?:incorporated|corporation|technologies|technology|company|labs|inc|corp|llc|ltd)\.?$",
    re.I,
)
BATCH_RE = re.compile(r"^([SW])\s*(20)?(\d{2})$", re.I)


@dataclass(frozen=True)
class CompanyCandidate:
    """One company observation from a discovery source."""

    name: str
    source: str
    source_url: str
    domain: str | None = None
    employee_count: int | None = None
    funding_date: str | None = None
    batch: str | None = None
    hiring: bool = False
    tags: tuple[str, ...] = ()

    def record(self, now: date | None = None) -> dict[str, Any]:
        return {
            "name": clean_name(self.name),
            "domain": normalize_domain(self.domain),
            "source": self.source,
            "quality_status": quality_status(self, now),
            "quality_signals": {
                "employee_count": self.employee_count,
                "funding_date": self.funding_date,
                "batch": self.batch,
                "hiring": self.hiring,
                "sources": self.source.split(","),
                "source_urls": self.source_url.split(","),
                "tags": list(self.tags),
            },
        }


def clean_name(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()[:200]


def company_key(name: str, domain: str | None = None) -> str:
    without_suffix = LEGAL_SUFFIX_RE.sub("", clean_name(name))
    normalized_name = re.sub(r"[^a-z0-9]", "", without_suffix.lower())
    if normalized_name:
        return normalized_name
    return normalize_domain(domain) or ""


def normalize_domain(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value if "://" in value else f"//{value}")
    if not parsed.hostname:
        return None
    return parsed.hostname.lower().removeprefix("www.")


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _batch_date(value: str | None) -> date | None:
    match = BATCH_RE.match(value or "")
    if not match:
        return None
    year = 2000 + int(match.group(3))
    month = 1 if match.group(1).upper() == "W" else 6
    return date(year, month, 1)


def quality_status(candidate: CompanyCandidate, now: date | None = None) -> str:
    """Pass companies with 20 employees or verified backing in the last 18 months."""
    if candidate.employee_count is not None and candidate.employee_count >= 20:
        return "passed"
    today = now or datetime.now(UTC).date()
    cutoff_month = today.month - 18
    cutoff_year = today.year
    while cutoff_month <= 0:
        cutoff_month += 12
        cutoff_year -= 1
    cutoff = date(cutoff_year, cutoff_month, min(today.day, 28))
    signal_date = _parse_date(candidate.funding_date) or _batch_date(candidate.batch)
    return "passed" if signal_date is not None and cutoff <= signal_date <= today else "pending"


def merge_candidates(candidates: list[CompanyCandidate]) -> list[CompanyCandidate]:
    """Deduplicate observations and preserve the strongest available signals."""
    merged: dict[str, CompanyCandidate] = {}
    for candidate in candidates:
        name = clean_name(candidate.name)
        if not name:
            continue
        normalized = replace(candidate, name=name, domain=normalize_domain(candidate.domain))
        key = company_key(normalized.name, normalized.domain)
        if not key:
            continue
        previous = merged.get(key)
        if previous is None:
            merged[key] = normalized
            continue
        tags = tuple(dict.fromkeys((*previous.tags, *normalized.tags)))
        employee_counts = [
            count
            for count in (previous.employee_count, normalized.employee_count)
            if count is not None
        ]
        funding_dates = [
            value for value in (previous.funding_date, normalized.funding_date) if value
        ]
        merged_sources = ",".join(
            dict.fromkeys((*previous.source.split(","), *normalized.source.split(",")))
        )
        merged_urls = ",".join(
            dict.fromkeys((*previous.source_url.split(","), *normalized.source_url.split(",")))
        )
        merged[key] = CompanyCandidate(
            name=previous.name if len(previous.name) >= len(normalized.name) else normalized.name,
            domain=previous.domain or normalized.domain,
            source=merged_sources,
            source_url=merged_urls,
            employee_count=max(employee_counts) if employee_counts else None,
            funding_date=max(funding_dates) if funding_dates else None,
            batch=previous.batch or normalized.batch,
            hiring=previous.hiring or normalized.hiring,
            tags=tags,
        )
    return sorted(merged.values(), key=lambda item: item.name.casefold())
