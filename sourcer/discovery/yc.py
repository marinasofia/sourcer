"""Y Combinator companies that are hiring in the US.

Reads the yc-oss dataset, a daily copy of YC's own company directory published
as plain JSON, instead of the directory page, which shows only its first 50
companies to a reader that does not run its scripts.
"""

from __future__ import annotations

from typing import Any

from .http import DiscoveryClient
from .models import CompanyCandidate

YC_HIRING_URL = "https://yc-oss.github.io/api/companies/hiring.json"
US_REGIONS = {"United States of America", "America / Canada", "Fully Remote"}
# YC runs four batches a year; the month each one starts
SEASON_MONTH = {"winter": 1, "spring": 4, "summer": 6, "fall": 9}


def batch_start(batch: str) -> str | None:
    """'Summer 2025' -> '2025-06-01', for the recent backing quality gate."""
    parts = (batch or "").split()
    if len(parts) != 2 or parts[0].lower() not in SEASON_MONTH or not parts[1].isdigit():
        return None
    return f"{parts[1]}-{SEASON_MONTH[parts[0].lower()]:02d}-01"


def parse_companies(rows: list[dict[str, Any]]) -> list[CompanyCandidate]:
    out = []
    for row in rows:
        if not row.get("isHiring") or row.get("status") not in ("Active", "Public"):
            continue
        if not US_REGIONS & set(row.get("regions") or ()):
            continue
        out.append(
            CompanyCandidate(
                name=str(row.get("name") or ""),
                source="yc",
                source_url=str(row.get("url") or ""),
                domain=row.get("website") or None,
                employee_count=row.get("team_size"),
                funding_date=batch_start(str(row.get("batch") or "")),
                batch=row.get("batch"),
                hiring=True,
                tags=tuple(row.get("tags") or ()),
            )
        )
    return [c for c in out if c.name]


def fetch(client: DiscoveryClient) -> list[CompanyCandidate]:
    return parse_companies(client.get_json(YC_HIRING_URL))
