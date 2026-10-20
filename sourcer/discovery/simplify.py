"""Turn SimplifyJobs listings into company discovery observations."""

from __future__ import annotations

from .. import sources
from .http import DiscoveryClient
from .models import CompanyCandidate


def fetch(client: DiscoveryClient) -> list[CompanyCandidate]:
    del client
    return [
        CompanyCandidate(
            name=posting.get("company", ""),
            source="simplify",
            source_url=posting.get("url", ""),
            hiring=True,
            tags=(posting.get("employment_type") or "",),
        )
        for posting in sources.fetch_simplify()
        if posting.get("company")
    ]
