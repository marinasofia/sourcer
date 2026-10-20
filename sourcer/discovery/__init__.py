"""Weekly company discovery from curated, read-only sources."""

from .models import CompanyCandidate, merge_candidates, quality_status
from .service import DiscoveryResult, discover

__all__ = [
    "CompanyCandidate",
    "DiscoveryResult",
    "discover",
    "merge_candidates",
    "quality_status",
]
