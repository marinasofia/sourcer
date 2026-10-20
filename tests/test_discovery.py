"""Company discovery parsing, quality gates, persistence, and isolation."""

from __future__ import annotations

import json
import os
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from sourcer import sqlitedb
from sourcer.discovery import builtin, getro, hn, yc
from sourcer.discovery.http import DiscoveryClient
from sourcer.discovery.models import CompanyCandidate, merge_candidates, quality_status
from sourcer.discovery.service import discover


def test_quality_gate_requires_scale_or_recent_backing() -> None:
    today = date(2026, 9, 4)
    base = {"name": "Acme", "source": "test", "source_url": "https://example.com"}
    assert quality_status(CompanyCandidate(**base, employee_count=20), today) == "passed"
    assert quality_status(CompanyCandidate(**base, employee_count=19), today) == "pending"
    assert quality_status(CompanyCandidate(**base, funding_date="2025-04-01"), today) == "passed"
    assert quality_status(CompanyCandidate(**base, funding_date="2025-02-01"), today) == "pending"
    assert quality_status(CompanyCandidate(**base, batch="S26"), today) == "passed"
    assert quality_status(CompanyCandidate(**base, batch="W24"), today) == "pending"


def test_merge_normalizes_legal_suffixes_and_preserves_strongest_signals() -> None:
    merged = merge_candidates(
        [
            CompanyCandidate("Acme, Inc.", "one", "https://one.example", employee_count=12),
            CompanyCandidate(
                "Acme",
                "two",
                "https://two.example",
                domain="acme.example",
                employee_count=25,
                tags=("ai",),
            ),
        ]
    )
    assert len(merged) == 1
    assert merged[0].employee_count == 25
    assert merged[0].domain == "acme.example"
    assert merged[0].source == "one,two"


def test_builtin_parser_extracts_employee_count_and_hiring() -> None:
    html = """
    <div class="company-card-horizontal">
      <div class="company-info-section">
        <a data-track-id="title" href="/company/acme"><h2>Acme AI</h2></a>
        Artificial Intelligence • Fintech
      </div>
      <span>1,250 Employees</span><span>See Our Teams Hiring Now</span>
    </div>
    """
    rows = builtin.parse_page(html, "https://www.builtinnyc.com/companies/type/fintech")
    assert len(rows) == 1
    assert rows[0].name == "Acme AI"
    assert rows[0].employee_count == 1250
    assert rows[0].hiring
    assert rows[0].source_url == "https://www.builtinnyc.com/company/acme"


def test_yc_keeps_us_hiring_companies_and_dates_their_batch() -> None:
    rows: list[dict[str, object]] = [
        {
            "name": "Acme",
            "isHiring": True,
            "status": "Active",
            "batch": "Summer 2026",
            "regions": ["United States of America"],
            "team_size": 4,
            "website": "https://acme.dev",
            "url": "https://www.ycombinator.com/companies/acme",
            "tags": ["fintech"],
        },
        {
            "name": "Faraway",
            "isHiring": True,
            "status": "Active",
            "batch": "Fall 2025",
            "regions": ["Europe"],
            "team_size": 30,
        },
        {
            "name": "Quiet",
            "isHiring": False,
            "status": "Active",
            "batch": "Winter 2026",
            "regions": ["United States of America"],
        },
    ]
    found = yc.parse_companies(rows)
    assert [c.name for c in found] == ["Acme"]
    assert found[0].funding_date == "2026-06-01"
    assert found[0].domain == "https://acme.dev"
    assert found[0].tags == ("fintech",)
    assert yc.batch_start("Fall 2025") == "2025-09-01"
    assert yc.batch_start("Unspecified") is None


def test_hn_parser_filters_target_terms_and_extracts_domain() -> None:
    rows: list[dict[str, object]] = [
        {
            "objectID": "123",
            "comment_text": (
                'Acme | NYC | AI intern<br><a href="https://acme.example/jobs">Jobs</a>'
            ),
        },
        {"objectID": "124", "comment_text": "Bakery | Paris | Pastry chef"},
    ]
    found = hn.parse_comments(rows)
    assert len(found) == 1
    assert found[0].name == "Acme"
    assert found[0].domain == "acme.example"


def test_discovery_isolates_source_failure_and_persists_quality(tmp_db: Any) -> None:
    good = CompanyCandidate(
        "Acme",
        "test",
        "https://example.com/acme",
        employee_count=30,
    )

    def ok(_client: Any) -> list[CompanyCandidate]:
        return [good]

    def fail(_client: Any) -> list[CompanyCandidate]:
        raise requests.ConnectionError("offline")

    result = discover(tmp_db, fetchers={"ok": ok, "fail": fail})
    assert result.candidates == (good,)
    assert result.new_candidates == (good,)
    assert result.errors == ("fail: offline",)
    row = sqlitedb.list_companies(tmp_db)[0]
    assert row["quality_status"] == "passed"
    assert json.loads(row["quality_signals"])["employee_count"] == 30

    again = discover(tmp_db, fetchers={"ok": ok})
    assert again.new_candidates == ()
    assert (
        sqlitedb.save_discovered_companies(
            tmp_db, [{"name": "Acme", "quality_status": "pending", "quality_signals": {}}]
        )
        == 0
    )
    assert sqlitedb.list_companies(tmp_db)[0]["quality_status"] == "passed"


def test_discovery_input_cannot_enable_submission(tmp_db: Any) -> None:
    try:
        sqlitedb.save_discovered_companies(
            tmp_db,
            [
                {
                    "name": "Acme",
                    "quality_status": "passed",
                    "quality_signals": {},
                    "submit_enabled": True,
                }
            ],
        )
    except ValueError as exc:
        assert "cannot set submit_enabled" in str(exc)
    else:
        raise AssertionError("expected discovery submission gate")


class DiscoveryResponse:
    text = '{"items": []}'

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, list[Any]]:
        return {"items": []}


class DiscoverySession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> DiscoveryResponse:
        self.calls.append((url, kwargs))
        return DiscoveryResponse()


def test_discovery_client_adds_identity_header_and_decodes_json() -> None:
    session = DiscoverySession()
    client = DiscoveryClient(interval=0, session=session)
    assert client.get_json("https://example.com", headers={"Accept": "application/json"}) == {
        "items": []
    }
    _, kwargs = session.calls[0]
    assert kwargs["headers"]["User-Agent"].startswith("Sourcer")
    assert kwargs["headers"]["Accept"] == "application/json"


def test_getro_configuration_and_fetch() -> None:
    with (
        patch.dict(os.environ, {"SOURCER_GETRO_NETWORKS": "[]"}),
        pytest.raises(ValueError, match="must map"),
    ):
        getro.configured_networks()
    with patch.dict(
        os.environ,
        {
            "SOURCER_GETRO_API_KEY": "token",
            "SOURCER_GETRO_NETWORKS": '{"a16z":"42"}',
        },
    ):
        client = MagicMock()
        client.get_json.return_value = {
            "items": [
                {
                    "company": {
                        "name": "Acme",
                        "website_url": "https://acme.example",
                        "employee_count": 50,
                        "last_funding_date": "2026-01-01",
                    },
                    "url": "https://jobs.example/acme",
                    "locations": ["New York"],
                    "job_functions": ["Data"],
                },
                {},
            ]
        }
        rows = getro.fetch(client)
    assert len(rows) == 1
    assert rows[0].name == "Acme"
    assert rows[0].tags == ("New York", "Data")
