"""eligible(): one function for every rule about the candidate, with the
rule's code, plain reason, and the posting's own words when it read them."""

from __future__ import annotations

from typing import Any

import pytest

from sourcer import eligibility, rescreen
from sourcer.eligibility import BOARD_RULES, EXCLUSION_RULES, OPEN, RULES, eligible
from tests.conftest import PROFILE


def posting(**over: Any) -> dict[str, Any]:
    base = {
        "title": "Data Analyst",
        "location": "New York, NY",
        "employment_type": "full time",
        "start_date": "June 2027",
        "seniority": "entry",
        "years_experience_min": None,
        "work_mode": "onsite",
        "description": "",
    }
    return {**base, **over}


def test_an_open_posting_passes_every_rule() -> None:
    assert eligible(posting(), PROFILE) == OPEN
    assert OPEN.ok and not OPEN.rule


@pytest.mark.parametrize(
    "description",
    [
        "Full time contract starting Summer 2027, 40 hours a week.",
        "A full-time role for 2027 graduates on a 12 month contract.",
    ],
)
def test_a_full_time_contract_starting_in_the_target_summer_is_open(description: str) -> None:
    p = posting(employment_type="contract", start_date=None, description=description)
    assert eligible(p, PROFILE).ok


@pytest.mark.parametrize(
    ("over", "why"),
    [
        ({"description": "Remote contract, 20 hours a week, starts Summer 2027."}, "not full time"),
        ({"description": "Full time contract, start immediately."}, "no target start"),
        ({"description": ""}, "no text at all"),
    ],
)
def test_other_contract_roles_stay_out(over: dict[str, Any], why: str) -> None:
    v = eligible(posting(employment_type="contract", start_date=None, **over), PROFILE)
    assert (v.ok, v.rule, v.reason) == (False, "contract", "contract role"), why


def test_a_rule_that_reads_the_text_quotes_it() -> None:
    v = eligible(posting(description="Active security clearance required."), PROFILE)
    assert v.rule == "clearance_required" and v.quote and v.quote in v.reason
    assert RULES[v.rule] == "Clearance"


def test_board_rules_add_the_stored_location_and_the_level_grade() -> None:
    away = posting(location="London, UK", work_mode="remote", start_date="")
    assert eligible(away, PROFILE, EXCLUSION_RULES).ok
    assert eligible(away, PROFILE).rule == "location"
    past_class = posting(employment_type="intern", start_date="2026 new grad")  # a year too early
    assert eligible(past_class, PROFILE, EXCLUSION_RULES).ok
    v = eligible(past_class, PROFILE)
    assert (v.rule, v.reason) == ("level_misfit", "level misfit for profile")


def test_every_code_a_rule_can_return_has_a_label() -> None:
    assert set(EXCLUSION_RULES) < set(BOARD_RULES)
    codes = {"senior_title", "seniority", "years", "contract", "location", "level_misfit"}
    assert codes <= set(RULES)


def test_rescreen_reason_is_the_board_verdict() -> None:
    row = posting(title="Senior Data Analyst")
    assert rescreen.reason_for(row, PROFILE) == eligible(row, PROFILE).reason
    assert rescreen.reason_for(posting(), PROFILE) is None


def test_check_exclusion_is_the_scoring_time_verdict() -> None:
    p = posting(
        employment_type="intern", start_date="2026 new grad"
    )  # a level grade, not an exclusion
    assert eligibility.check_exclusion(p, PROFILE) is None
    assert eligibility.check_exclusion(posting(title="Staff Analyst"), PROFILE)
