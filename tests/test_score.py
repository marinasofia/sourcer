"""Score function and exclusion/flag policy tests."""

import json

from sourcer import llm
from sourcer.eligibility import check_exclusion
from sourcer.pipeline.filters import timing_flag
from tests.test_parse import make_client

SKILLS = {
    "skills": [
        {"skill": "Python", "weight": 10, "category": "Languages", "synonyms": []},
        {"skill": "LangGraph", "weight": 10, "category": "AI/ML", "synonyms": ["LangChain"]},
    ]
}


def parsed_posting(**over):
    base = {
        "company": "TestCo",
        "title": "AI Engineer Intern",
        "location": "New York, NY",
        "url": "https://example.com/job/1",
        "requirements": ["Python", "LangGraph"],
        "seniority": "intern",
        "tech_stack": ["Python"],
        "employment_type": "intern",
        "start_date": "summer 2027",
        "years_experience_min": None,
        "hash": "abc",
    }
    base.update(over)
    return base


def test_score_valid():
    client = make_client([json.dumps({"score": 87, "rationale": "strong Python match"})])
    data, err = llm.score_posting(client, parsed_posting(), SKILLS, llm.Usage())
    assert err is None
    assert data["score"] == 87
    assert "Python" in data["rationale"]


def test_score_retry_then_fail_logged():
    client = make_client(["nope", json.dumps({"score": "high"})])  # wrong type twice
    data, err = llm.score_posting(client, parsed_posting(), SKILLS, llm.Usage())
    assert data is None
    assert err is not None
    assert client.messages.create.call_count == 2


# --- exclusion policy: hard exclude ONLY on explicit signals ---


def test_senior_title_excluded():
    reason = check_exclusion(parsed_posting(title="Senior Data Scientist"))
    assert reason and "senior" in reason.lower()


def test_five_plus_years_excluded():
    reason = check_exclusion(parsed_posting(years_experience_min=5))
    assert reason and "5+" in reason


def test_immediate_start_full_time_excluded():
    reason = check_exclusion(
        parsed_posting(employment_type="full time", start_date="immediate start")
    )
    assert reason == "full time with immediate start"


def test_contract_excluded():
    assert check_exclusion(parsed_posting(employment_type="contract")) == "contract role"


def test_intern_not_excluded():
    assert check_exclusion(parsed_posting()) is None


def test_ambiguous_timing_not_excluded_but_flagged():
    p = parsed_posting(employment_type="unspecified", start_date=None)
    assert check_exclusion(p) is None  # scored normally, never hard excluded
    unclear, note = timing_flag(p)
    assert unclear is True
    assert "employment type not stated" in note
    assert "start date not stated" in note


def test_new_grad_2027_full_time_clear_timing():
    p = parsed_posting(employment_type="full time", start_date="2027 new grad")
    assert check_exclusion(p) is None
    unclear, _ = timing_flag(p)
    assert unclear is False


def test_four_years_excluded_for_student_targets():
    assert check_exclusion(parsed_posting(years_experience_min=4)) == "requires 4+ years experience"
