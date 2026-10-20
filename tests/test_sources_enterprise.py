"""Eightfold and Oracle Recruiting adapters, with the HTTP layer faked."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sourcer import score_listings
from sourcer.sources import eightfold, oracle
from tests.conftest import FROZEN_NOW

TODAY = FROZEN_NOW


def reply(payload: Any) -> MagicMock:
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def ts(days: int) -> int:
    return int((TODAY - timedelta(days=days)).timestamp())


def test_eightfold_reads_listed_postings_and_opens_the_promising_ones() -> None:
    page = {"data": {"positions": [
        {"id": 1, "name": "Data Analyst", "locations": ["New York City, New York"],
         "postedTs": ts(2)},
        {"id": 2, "name": "Warehouse Associate", "locations": ["Omaha, Nebraska"],
         "postedTs": ts(3)},
        {"id": 3, "name": "Data Analyst", "locations": ["New York"], "postedTs": ts(90)},
    ]}}  # fmt: skip
    detail = {"data": {"jobDescription": "<p>SQL and Python</p>",
                       "publicUrl": "https://paypal.eightfold.ai/careers/job/1"}}  # fmt: skip

    def get(url: str, params: dict[str, Any], timeout: int) -> MagicMock:
        return reply(detail if "position_details" in url else page)

    with patch.object(eightfold.net, "get", side_effect=get), \
            patch.object(eightfold.time, "sleep"):  # fmt: skip
        out = eightfold.fetch_eightfold("paypal/paypal.com")
    # the 90 day old analyst role is still listed, so it is read too
    assert [p["title"] for p in out] == ["Data Analyst", "Warehouse Associate", "Data Analyst"]
    assert out[0]["description"] == "SQL and Python"
    assert out[0]["url"] == "https://paypal.eightfold.ai/careers/job/1"
    assert out[1]["description"] == ""  # not worth opening


def test_oracle_reads_the_division_and_program_text_when_the_role_fields_are_empty() -> None:
    division = "<b>About the division</b> " + "Asset management. " * 30
    program = "About the program: a ten week summer internship."
    detail = {"items": [{"ExternalDescriptionStr": "<p> </p>", "ExternalQualificationsStr": "",
                         "OrganizationDescriptionStr": division,
                         "CorporateDescriptionStr": program,
                         "PrimaryLocation": "New York, NY"}]}  # fmt: skip
    with patch.object(oracle.net, "get", return_value=reply(detail)):
        got = oracle._detail("hdpc.fa.us2.oraclecloud.com", "LateralHiring", "155950")
    assert got["description"].startswith("About the division\nAsset management.")
    assert got["description"].endswith("a ten week summer internship.")


def test_oracle_reads_the_requisition_list_and_details() -> None:
    day = (TODAY - timedelta(days=1)).date().isoformat()
    listing = {"items": [{"requisitionList": [
        {"Id": "55", "Title": "Associate Product Manager", "PostedDate": day,
         "PrimaryLocation": "New York, NY, United States", "secondaryLocations": []},
    ]}]}  # fmt: skip
    detail = {"items": [{"ExternalDescriptionStr": "<b>Build</b>",
                         "ExternalQualificationsStr": "SQL", "PrimaryLocation": "New York, NY",
                         "secondaryLocations": [{"Name": "Remote, US"}]}]}  # fmt: skip

    def get(url: str, headers: dict[str, str], timeout: int) -> MagicMock:
        return reply(detail if "Details" in url else listing)

    with patch.object(oracle.net, "get", side_effect=get), \
            patch.object(oracle.time, "sleep"):  # fmt: skip
        out = oracle.fetch_oracle("egug.fa.us2.oraclecloud.com/CX_1")
    assert out == [{
        "title": "Associate Product Manager",
        "location": "New York, NY; Remote, US",
        "url": "https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/55",
        "description": "Build\nSQL",
        "posted_at": day,
    }]  # fmt: skip


@pytest.mark.parametrize(
    "slug",
    [
        "127.0.0.1:8600#.oraclecloud.com/CX_1",
        "evil.com/x.oraclecloud.com/CX_1",
        "a.oraclecloud.com/CX_1?x",
    ],
)
def test_oracle_refuses_a_slug_that_is_not_a_tenant_host(slug: str) -> None:
    with patch.object(oracle.net, "get") as get, pytest.raises(ValueError):
        oracle.fetch_oracle(slug)
    get.assert_not_called()


def test_an_oracle_link_with_a_smuggled_host_is_not_read_as_oracle() -> None:
    smuggled = (
        "https://127.0.0.1:5820#.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1"
    )
    assert score_listings.ORACLE_URL_RE.search(smuggled) is None
