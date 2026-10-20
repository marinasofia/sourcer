"""Discovery orchestration with per-source isolation and database persistence."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

import requests

from .. import db
from ..log import get_logger
from . import builtin, getro, hn, simplify, yc
from .http import DiscoveryClient
from .models import CompanyCandidate, company_key, merge_candidates

log = get_logger(__name__)
Fetcher = Callable[[DiscoveryClient], list[CompanyCandidate]]
FETCHERS: dict[str, Fetcher] = {
    "builtin": builtin.fetch,
    "yc": yc.fetch,
    "hn": hn.fetch,
    "simplify": simplify.fetch,
    "getro": getro.fetch,
}


@dataclass(frozen=True)
class DiscoveryResult:
    candidates: tuple[CompanyCandidate, ...]
    new_candidates: tuple[CompanyCandidate, ...]
    errors: tuple[str, ...]
    source_counts: Mapping[str, int]


def discover(
    conn: db.Connection,
    source_names: Iterable[str] | None = None,
    client: DiscoveryClient | None = None,
    fetchers: Mapping[str, Fetcher] | None = None,
) -> DiscoveryResult:
    """Fetch, merge, quality-label, and persist company observations."""
    registry = dict(fetchers or FETCHERS)
    selected = list(source_names or registry)
    unknown = set(selected) - set(registry)
    if unknown:
        raise ValueError(f"unknown discovery sources: {sorted(unknown)}")
    http = client or DiscoveryClient()
    found: list[CompanyCandidate] = []
    errors = []
    counts: dict[str, int] = {}
    for name in selected:
        try:
            rows = registry[name](http)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            log.warning("discovery source %s failed: %s", name, exc)
            errors.append(f"{name}: {exc}")
            counts[name] = 0
            continue
        counts[name] = len(rows)
        found.extend(rows)
    candidates = merge_candidates(found)
    existing = {company_key(str(row["name"]), row.get("domain")) for row in db.list_companies(conn)}
    new_candidates = tuple(
        candidate
        for candidate in candidates
        if company_key(candidate.name, candidate.domain) not in existing
    )
    db.save_discovered_companies(conn, [candidate.record() for candidate in candidates])
    return DiscoveryResult(tuple(candidates), new_candidates, tuple(errors), counts)
