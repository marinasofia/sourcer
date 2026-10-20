"""Official Getro API integration for configured VC portfolio networks."""

from __future__ import annotations

import json
import os
from typing import Any

from .http import DiscoveryClient
from .models import CompanyCandidate

GETRO_API = "https://api.getro.com/v2/networks/{network_id}/jobs"
SUPPORTED_NETWORKS = (
    "a16z",
    "sequoia",
    "insight",
    "thrive",
    "general_catalyst",
    "bessemer",
    "usv",
    "lerer_hippeau",
    "firstmark",
    "primary",
    "work_bench",
    "two_sigma_ventures",
)


def configured_networks() -> dict[str, str]:
    raw = os.environ.get("SOURCER_GETRO_NETWORKS", "{}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("SOURCER_GETRO_NETWORKS must be a JSON object") from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(value, (str, int)) for key, value in parsed.items()
    ):
        raise ValueError("SOURCER_GETRO_NETWORKS must map source names to network ids")
    unknown = set(parsed) - set(SUPPORTED_NETWORKS)
    if unknown:
        raise ValueError(f"unsupported Getro network names: {sorted(unknown)}")
    return {key: str(value) for key, value in parsed.items()}


def _candidate(item: dict[str, Any], network: str) -> CompanyCandidate | None:
    company = item.get("company") or {}
    if not isinstance(company, dict):
        company = {}
    name = company.get("name") or item.get("company_name")
    if not name:
        return None
    locations = item.get("locations") or []
    tags = tuple(str(value) for value in (*locations, *(item.get("job_functions") or [])))
    return CompanyCandidate(
        name=str(name),
        domain=company.get("website_url") or company.get("website"),
        source=f"getro_{network}",
        source_url=str(item.get("url") or item.get("apply_url") or ""),
        employee_count=company.get("employee_count"),
        funding_date=company.get("last_funding_date"),
        hiring=True,
        tags=tags,
    )


def fetch(client: DiscoveryClient) -> list[CompanyCandidate]:
    token = os.environ.get("SOURCER_GETRO_API_KEY")
    networks = configured_networks()
    if not token or not networks:
        return []
    out = []
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    for network, network_id in networks.items():
        payload = client.get_json(
            GETRO_API.format(network_id=network_id),
            headers=headers,
            params={"locations": "New York|Remote", "per_page": 100},
        )
        for item in payload.get("items", []):
            candidate = _candidate(item, network)
            if candidate is not None:
                out.append(candidate)
    return out
