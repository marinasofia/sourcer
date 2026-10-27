"""Pipeline node tests: source, dedupe, parse, score, output. No network, no API."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
import requests

from sourcer import config, eligibility, llm, sqlitedb
from sourcer.digest import render_digest
from sourcer.eligibility import RULES
from sourcer.pipeline import dedupe, filters, output, scoring, source
from sourcer.pipeline.filters import _rule_eligibility
from sourcer.pipeline.state import State
from tests.conftest import (
    GOOD_PARSE_SCORE,
    PROFILE,
    SKILLS,
    current_trace,
    make_client,
    worker_replies,
)


def raw_posting(**over: Any) -> dict[str, Any]:
    base = {
        "company": "TestCo",
        "title": "AI Engineer Intern",
        "location": "New York, NY",
        "url": "https://boards.greenhouse.io/testco/jobs/1",
        "description": "Summer 2027 internship. Python and LangGraph.",
        "posted_at": "2026-08-20",
    }
    base.update(over)
    return base


def base_state(conn: Any, client: Any = None, **over: Any) -> State:
    state: State = {
        "companies": [{"company": "TestCo", "ats": "greenhouse", "slug": "testco"}],
        "skills": SKILLS,
        "profile": PROFILE,
        "client": client,
        "usage": llm.Usage(),
        "conn": conn,
        "stats": {},
    }
    state.update(over)  # type: ignore[typeddict-item]
    return state


# --- source node ---


def test_source_node_counts_a_dead_board_as_failure(tmp_db):
    def boom(entry, **kwargs):
        raise requests.ConnectionError("down")

    state = base_state(tmp_db)
    with (
        patch("sourcer.sources.fetch_company", side_effect=boom),
        patch("sourcer.sources.fetch_simplify", return_value=[]),
    ):
        out = source.source_node(state)
    assert out["stats"]["companies_covered"] == 0
    assert out["stats"]["source_failures"] == ["TestCo: down"]
    assert out["postings"] == []
    assert out["stats"]["node_metrics"]["source"]["fetch_failures"] == 1


def test_source_node_fetches_boards_in_parallel_and_keeps_order(tmp_db):
    companies = [{"company": f"Co{i}", "ats": "greenhouse", "slug": f"co{i}"} for i in range(6)]

    def fetch(entry, *, profile=None, reasons=None, dropped=None):
        if entry["company"] == "Co3":
            raise requests.Timeout("slow")
        reasons["stale"] += 2
        return [raw_posting(company=entry["company"])], [], 2

    state = base_state(tmp_db, companies=companies)
    with (
        patch("sourcer.sources.fetch_company", side_effect=fetch),
        patch("sourcer.sources.fetch_simplify", return_value=[]),
    ):
        out = source.source_node(state)
    assert [p["company"] for p in out["postings"]] == ["Co0", "Co1", "Co2", "Co4", "Co5"]
    assert out["stats"]["source_failures"] == ["Co3: slow"]
    assert out["stats"]["drop_reasons"]["stale"] == 10
    assert out["stats"]["companies_covered"] == 5


def test_source_node_tries_an_unreachable_board_once_more(tmp_db):
    companies = [{"company": f"Co{i}", "ats": "greenhouse", "slug": f"co{i}"} for i in range(3)]
    calls: dict[str, int] = {}

    def fetch(entry, *, profile=None, reasons=None, dropped=None):
        name = entry["company"]
        calls[name] = calls.get(name, 0) + 1
        if name == "Co0" and calls[name] == 1:
            raise requests.ConnectionError("asleep")
        if name == "Co1":
            raise requests.HTTPError("404 Not Found")
        return [raw_posting(company=name)], [], 0

    state = base_state(tmp_db, companies=companies)
    with (
        patch("sourcer.sources.fetch_company", side_effect=fetch),
        patch("sourcer.sources.fetch_simplify", return_value=[]),
    ):
        out = source.source_node(state)
    assert calls == {"Co0": 2, "Co1": 1, "Co2": 1}
    assert [p["company"] for p in out["postings"]] == ["Co0", "Co2"]
    assert out["stats"]["source_failures"] == ["Co1: 404 Not Found"]


def test_triage_fails_open_when_the_model_call_raises():
    client = make_client([])
    client.messages.create.side_effect = RuntimeError("claude CLI timed out after 180s")
    assert llm.triage_titles(client, ["Deployment Strategist"], llm.Usage()) == [0]


def test_source_node_survives_a_failing_simplify_fetch(tmp_db):
    kept = [raw_posting()]
    with (
        patch("sourcer.sources.fetch_company", return_value=(kept, [], 3)),
        patch("sourcer.sources.fetch_simplify", side_effect=requests.Timeout("slow")),
    ):
        out = source.source_node(base_state(tmp_db))
    assert out["stats"]["source_failures"] == ["simplify: slow"]
    assert out["stats"]["postings_seen"] == 4
    assert out["stats"]["prefiltered_out"] == 3
    assert out["postings"] == kept
    # the simplify list is not a company, so its failure does not lower the count
    assert out["stats"]["companies_covered"] == 1


def test_source_node_skips_simplify_when_disabled(tmp_db, monkeypatch):
    monkeypatch.setenv("SOURCER_SIMPLIFY", "0")
    with (
        patch("sourcer.sources.fetch_company", return_value=([], [], 0)),
        patch("sourcer.sources.fetch_simplify") as simplify,
    ):
        out = source.source_node(base_state(tmp_db))
    simplify.assert_not_called()
    assert out["stats"]["listings_seen"] == 0


def test_source_node_appends_listings(tmp_db, monkeypatch):
    monkeypatch.setenv("SOURCER_SIMPLIFY", "1")
    listing = raw_posting(source="simplify", description="")
    with (
        patch("sourcer.sources.fetch_company", return_value=([], [], 0)),
        patch("sourcer.sources.fetch_simplify", return_value=[listing]),
    ):
        out = source.source_node(base_state(tmp_db))
    assert out["postings"] == [listing]
    assert out["stats"]["listings_seen"] == 1


def test_triage_admits_matched_titles(tmp_db):
    unsure = [raw_posting(title="Deployment Strategist"), raw_posting(title="Chef")]
    client = make_client([json.dumps({"match": [1]})])
    with (
        patch("sourcer.sources.fetch_company", return_value=([], unsure, 0)),
        patch("sourcer.sources.fetch_simplify", return_value=[]),
    ):
        out = source.source_node(base_state(tmp_db, client))
    assert out["stats"]["triaged_in"] == 1
    assert [p["title"] for p in out["postings"]] == ["Deployment Strategist"]
    assert out["stats"]["prefiltered_out"] == 1  # the unmatched title


def test_triage_blocked_by_cost_cap(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "COST_CAP_USD", 0.0)
    unsure = [raw_posting(title="Deployment Strategist")]
    client = make_client([json.dumps({"match": [1]})])
    with (
        patch("sourcer.sources.fetch_company", return_value=([], unsure, 0)),
        patch("sourcer.sources.fetch_simplify", return_value=[]),
    ):
        out = source.source_node(base_state(tmp_db, client))
    client.messages.create.assert_not_called()
    assert out["stats"]["triaged_in"] == 0
    assert out["stats"]["prefiltered_out"] == 1


def test_triage_skipped_without_client(tmp_db):
    unsure = [raw_posting(title="Deployment Strategist")]
    with (
        patch("sourcer.sources.fetch_company", return_value=([], unsure, 0)),
        patch("sourcer.sources.fetch_simplify", return_value=[]),
    ):
        out = source.source_node(base_state(tmp_db, None))
    assert out["stats"]["triaged_in"] == 0
    assert out["postings"] == []


# --- dedupe node ---


def test_dedupe_within_run_and_against_db(tmp_db, posting_row):
    stored = posting_row(title="Stored", url="https://x/1")
    stored["hash"] = sqlitedb.posting_hash(stored["company"], stored["title"], stored["url"])
    sqlitedb.insert_posting(tmp_db, stored)
    tmp_db.commit()
    a = raw_posting(title="Stored", url="https://x/1")
    b = raw_posting(title="Fresh", url="https://x/2")
    b_dup = raw_posting(title="Fresh", url="https://x/2")
    state = base_state(tmp_db, postings=[a, b, b_dup], stats={})
    out = dedupe.dedupe_node(state)
    assert [p["title"] for p in out["new_postings"]] == ["Fresh"]
    assert out["stats"]["dedupe_count"] == 2
    assert out["stats"]["new_postings"] == 1
    assert all("hash" in p for p in [a, b, b_dup])


# --- parse node ---


def test_parse_listing_bypasses_model(tmp_db):
    listing = raw_posting(source="simplify", hash="h", employment_type="intern")
    client = make_client([])
    out = scoring.parse_node(base_state(tmp_db, client, new_postings=[listing]))
    client.messages.create.assert_not_called()
    rec = out["parsed"][0]
    assert rec["source"] == "simplify" and rec["requirements"] == []
    assert rec["seniority"] == "unspecified"


def test_parse_senior_title_short_circuits(tmp_db):
    p = raw_posting(title="Senior ML Engineer", hash="h")
    client = make_client([])
    out = scoring.parse_node(base_state(tmp_db, client, new_postings=[p]))
    client.messages.create.assert_not_called()
    rec = out["parsed"][0]
    assert rec["seniority"] == "senior" and rec["score"] == 0
    assert out["stats"]["skipped_cost_cap"] == 0


def test_parse_reuses_scored_twin(tmp_db, posting_row):
    twin = posting_row(title="AI Engineer Intern", url="https://x/old", score=91)
    twin.update(hash="old", score_trace=current_trace(SKILLS))
    sqlitedb.insert_posting(tmp_db, twin)
    tmp_db.commit()
    p = raw_posting(hash="new", url="https://x/new")
    client = make_client([])
    out = scoring.parse_node(base_state(tmp_db, client, new_postings=[p]))
    client.messages.create.assert_not_called()
    rec = out["parsed"][0]
    assert rec["score"] == 91 and rec["url"] == "https://x/new"
    assert rec["requirements"] == ["Python"]
    assert rec["score_trace"]["reused_from"] == 1  # the copy says where it came from


def test_parse_reads_again_when_the_twin_is_from_an_older_scorer(tmp_db, posting_row):
    old = current_trace(SKILLS)
    old["input"]["scorer"] = "before-calibration"
    twin = posting_row(title="AI Engineer Intern", url="https://x/old", score=91)
    twin.update(hash="old", score_trace=old)
    sqlitedb.insert_posting(tmp_db, twin)
    tmp_db.commit()
    p = raw_posting(hash="new", url="https://x/new")
    client = make_client(worker_replies())
    out = scoring.parse_node(base_state(tmp_db, client, new_postings=[p]))
    assert client.messages.create.call_count == 2  # read again (facts, then fit), never copied
    assert "reused_from" not in (out["parsed"][0].get("score_trace") or {})


def test_a_twin_whose_facts_were_never_checked_is_read_again(tmp_db, posting_row):
    old = current_trace(SKILLS)
    del old["facts"]  # scored by the older one-call reader: a summary, not the posting's words
    twin = posting_row(title="AI Engineer Intern", url="https://x/old", score=91)
    twin.update(hash="old", score_trace=old)
    sqlitedb.insert_posting(tmp_db, twin)
    tmp_db.commit()
    client = make_client(worker_replies())
    p = raw_posting(hash="new", url="https://x/new")
    out = scoring.parse_node(base_state(tmp_db, client, new_postings=[p]))
    assert client.messages.create.call_count == 2 and out["parsed"][0]["score"] == 88


def test_parse_does_not_reuse_excluded_twin(tmp_db, posting_row):
    twin = posting_row(
        title="AI Engineer Intern",
        url="https://x/old",
        score=0,
        excluded=True,
        exclusion_reason="x",
    )
    twin["hash"] = "old"
    sqlitedb.insert_posting(tmp_db, twin)
    tmp_db.commit()
    p = raw_posting(hash="new", url="https://x/new")
    client = make_client(worker_replies())
    out = scoring.parse_node(base_state(tmp_db, client, new_postings=[p]))
    assert client.messages.create.call_count == 2
    assert out["parsed"][0]["score"] == 88


def test_parse_skips_on_cost_cap(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "COST_CAP_USD", 0.0)
    client = make_client([json.dumps(GOOD_PARSE_SCORE)])
    out = scoring.parse_node(base_state(tmp_db, client, new_postings=[raw_posting(hash="h")]))
    client.messages.create.assert_not_called()
    assert out["parsed"] == []
    assert out["stats"]["skipped_cost_cap"] == 1


def test_a_posting_without_text_is_read_first_and_never_scored_from_its_title(tmp_db, monkeypatch):
    monkeypatch.setattr(scoring, "MIN_TEXT", 400)
    client = make_client(worker_replies())
    bare = raw_posting(hash="h", description="")  # a Workday card whose detail was never opened
    out = scoring.parse_node(base_state(tmp_db, client, new_postings=[bare]))
    client.messages.create.assert_not_called()  # no score guessed from the title
    assert out["stats"]["skipped_no_text"] == 1 and out["parsed"][0]["no_text"]
    page = "Requirements: 3+ years of professional software engineering experience. " * 8
    state = base_state(tmp_db, client, new_postings=[raw_posting(hash="h2", description="")])
    state["read_posting"] = lambda url: page
    out = scoring.parse_node(state)
    # read from its page; the rules stop the 3+ years role on its facts, before any fit score
    assert client.messages.create.call_count == 1
    assert out["parsed"][0]["description"] == page


def test_parse_skips_without_client(tmp_db):
    out = scoring.parse_node(base_state(tmp_db, None, new_postings=[raw_posting(hash="h")]))
    assert out["parsed"] == []
    assert out["stats"]["skipped_cost_cap"] == 1


def test_parse_records_failure(tmp_db):
    client = make_client(["garbage", "still garbage"])
    out = scoring.parse_node(base_state(tmp_db, client, new_postings=[raw_posting(hash="h")]))
    assert out["parsed"] == []
    assert out["stats"]["parse_failures"] == 1
    detail = out["stats"]["parse_failure_detail"][0]
    assert detail["company"] == "TestCo" and detail["error"]


def test_parse_success_trusts_board_fields(tmp_db):
    reply = {**GOOD_PARSE_SCORE, "title": "Model echo", "location": "Mars"}
    client = make_client(worker_replies(reply))
    p = raw_posting(hash="h", title="Board Title", location="New York, NY")
    out = scoring.parse_node(base_state(tmp_db, client, new_postings=[p]))
    rec = out["parsed"][0]
    assert rec["title"] == "Board Title" and rec["location"] == "New York, NY"
    assert rec["hash"] == "h" and rec["source"] == "ats" and rec["posted_at"] == "2026-08-20"


# --- level fit ---


def parsed(**over: Any) -> dict[str, Any]:
    base = {
        "company": "TestCo",
        "title": "AI Engineer",
        "url": "u",
        "location": "New York, NY",
        "seniority": "entry",
        "years_experience_min": None,
        "employment_type": "full time",
        "start_date": None,
        "work_mode": "remote",
    }
    base.update(over)
    return base


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        ({"seniority": "senior"}, "misfit"),
        ({"seniority": "mid"}, "misfit"),
        ({"years_experience_min": 3}, "misfit"),
        ({"start_date": "2026 new grad"}, "misfit"),
        ({"start_date": "2027 new grad"}, "fit"),
        ({"employment_type": "intern"}, "fit"),
        ({"start_date": "summer 2027 internship"}, "fit"),
        ({"employment_type": "part time"}, "fit"),
        ({"seniority": "entry", "years_experience_min": 1}, "fit"),
        ({"seniority": "entry", "years_experience_min": 2}, "fit"),
        ({"seniority": "unspecified"}, "stretch"),
    ],
)
def test_level_fit_branches(over, expected):
    assert eligibility.level_fit(parsed(**over), PROFILE) == expected


def test_level_fit_without_profile_is_fit():
    assert eligibility.level_fit(parsed(seniority="senior"), None) == "fit"


def test_level_fit_new_grad_without_in_progress_is_not_misfit():
    profile = {**PROFILE, "in_progress": False}
    assert eligibility.level_fit(parsed(start_date="2026 new grad"), profile) == "fit"


# --- exclusion rules ---


def test_every_rule_fires():
    cases = {
        eligibility._rule_senior_title: parsed(title="Staff Engineer"),
        eligibility._rule_years: parsed(years_experience_min=5),
        eligibility._rule_immediate_full_time: parsed(start_date="immediate start"),
        eligibility._rule_contract: parsed(employment_type="contract"),
        eligibility._rule_in_person_outside_nyc: parsed(work_mode="hybrid", location="Austin, TX"),
        eligibility._rule_title_outside_nyc: parsed(title="Forward Deployed Engineer (West Coast)"),
        eligibility._rule_school_program: parsed(title="Data Co-Op (Northeastern University)"),
        eligibility._rule_degree_in_title: parsed(title="Campus Graduate Masters Engineer, AI"),
        _rule_eligibility: parsed(description="Active security clearance required."),
        eligibility._rule_starts_before_graduation: parsed(start_date="fall 2026"),
        eligibility._rule_internship_before_availability: parsed(
            employment_type="intern", start_date="winter 2027"
        ),
        eligibility._rule_graduation_window: parsed(
            description="Anticipated graduation date between December 2028 and June 2029"
        ),
    }
    assert set(cases) == set(eligibility.EXCLUSION_RULES)
    profile = {**PROFILE, "earliest_start_internship": "2027-05"}
    for rule, p in cases.items():
        verdict = rule(p, profile)
        assert verdict and not verdict.ok and verdict.rule in RULES, rule.__name__
        assert eligibility.check_exclusion(p, profile) == verdict.reason


def test_every_rule_passes_a_clean_posting():
    clean = parsed(employment_type="intern", start_date="summer 2027")
    profile = {**PROFILE, "earliest_start_internship": "2027-05"}
    for rule in eligibility.EXCLUSION_RULES:
        assert rule(clean, profile) is None, rule.__name__
    assert eligibility.check_exclusion(clean, profile) is None


def test_first_rule_wins():
    p = parsed(title="Senior Engineer", employment_type="contract")
    assert "senior" in (eligibility.check_exclusion(p) or "")


def test_timing_flag_intern_without_start_is_clear():
    assert filters.timing_flag(parsed(employment_type="intern", start_date=None)) == (False, "")


def test_timing_flag_reports_missing_pieces():
    unclear, note = filters.timing_flag(parsed(employment_type="", start_date=None))
    assert unclear and "employment type" in note and "start date" in note


# --- score node ---


def test_score_node_paths(tmp_db):
    listing = {**parsed(), "source": "simplify", "score": 5, "rationale": "x"}
    excluded = {**parsed(title="Senior X"), "score": 50, "rationale": "r"}
    unclear = {**parsed(employment_type="unspecified"), "score": 70, "rationale": "ok"}
    clean = {
        **parsed(employment_type="intern", start_date="summer 2027"),
        "score": 90.0,
        "rationale": "great",
    }
    state = base_state(tmp_db, parsed=[listing, excluded, unclear, clean], stats={})
    out = scoring.score_node(state)
    s_listing, s_excl, s_unclear, s_clean = out["scored"]
    assert s_listing["score"] is None and s_listing["excluded"] is False
    assert (
        s_excl["excluded"] and s_excl["score"] == 0 and s_excl["rationale"].startswith("excluded:")
    )
    assert s_unclear["timing_unclear"] and "[timing unclear:" in s_unclear["rationale"]
    assert s_clean["score"] == 90 and s_clean["timing_unclear"] is False
    assert out["stats"]["excluded_count"] == 1
    assert out["stats"]["timing_unclear_count"] == 1
    assert out["stats"]["exclusions"][0]["title"] == "Senior X"
    assert out["stats"]["score_failures"] == 0
    assert all("level_fit" in p for p in out["scored"])


# --- output node ---


def test_output_node_writes_rows_and_archives_misfits(tmp_db):
    fit = {**parsed(), "hash": "a", "score": 80, "rationale": "r", "level_fit": "fit"}
    misfit = {
        **parsed(title="Mid role"),
        "hash": "b",
        "score": 60,
        "rationale": "r",
        "level_fit": "misfit",
    }
    usage = llm.Usage()
    usage.prompt_tokens, usage.completion_tokens = 10, 5
    state = base_state(tmp_db, scored=[fit, misfit], stats={"new_postings": 2}, usage=usage)
    out = output.output_node(state)
    rows = {r["title"]: dict(r) for r in tmp_db.execute("SELECT * FROM postings")}
    assert rows["AI Engineer"]["status"] == "new"
    assert rows["Mid role"]["status"] == "archived" and "misfit" in rows["Mid role"]["notes"]
    assert out["stats"]["prompt_tokens"] == 10 and out["stats"]["completion_tokens"] == 5
    assert out["stats"]["est_cost_usd"] == usage.est_cost_usd
    assert out["stats"]["node_metrics"]["output"]["written"] == 2


# --- digest ---


def test_render_digest_full():
    state = {
        "scored": [
            {
                **parsed(),
                "score": 90,
                "rationale": "great",
                "timing_unclear": False,
                "excluded": False,
            },
            {
                **parsed(title="B"),
                "score": 70,
                "rationale": "ok",
                "timing_unclear": True,
                "excluded": False,
            },
            {**parsed(title="Senior"), "score": 0, "excluded": True, "rationale": "excluded"},
            {**parsed(title="L"), "score": None, "excluded": False, "source": "simplify"},
        ],
        "stats": {
            "companies_covered": 1,
            "postings_seen": 4,
            "prefiltered_out": 0,
            "new_postings": 4,
            "dedupe_count": 0,
            "parse_failures": 0,
            "score_failures": 0,
            "excluded_count": 1,
            "timing_unclear_count": 1,
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "est_cost_usd": 0.0,
            "exclusions": [{"company": "TestCo", "title": "Senior", "reason": "senior-level"}],
        },
    }
    text = render_digest(state)
    assert "SOURCER DIGEST" in text
    assert " 1. [ 90] TestCo - AI Engineer" in text
    assert " 2. [ 70] TestCo - B  [TIMING UNCLEAR]" in text
    assert "listing-only new  : 1" in text
    assert "EXCLUDED (logged to run_logs):" in text and "Senior: senior-level" in text
    # excluded rows never appear in the ranked list above the separator
    assert "[  0]" not in text


def test_render_digest_empty_state():
    text = render_digest({})
    assert "SOURCER DIGEST" in text
    assert "EXCLUDED" not in text
    assert text.count("=" * 78) == 3


def test_score_node_does_not_stack_timing_suffix_on_a_twin():
    """A twin's rationale already carries the suffix; the flag is computed once."""
    parsed = {
        "company": "TestCo",
        "title": "AI Engineer",
        "url": "u2",
        "location": "New York, NY",
        "source": "ats",
        "requirements": [],
        "tech_stack": [],
        "seniority": "unspecified",
        "employment_type": "unspecified",
        "start_date": None,
        "years_experience_min": None,
        "work_mode": "unspecified",
        "score": 80,
        "rationale": "Strong match. [timing unclear: start date not stated]",
    }
    state = {"parsed": [parsed], "stats": {}, "profile": None}
    out = scoring.score_node(state)
    rationale = out["scored"][0]["rationale"]
    assert rationale.count("[timing unclear") == 1
    assert rationale.startswith("Strong match.")
    assert out["scored"][0]["timing_unclear"] is True


def test_a_crashing_model_call_skips_only_that_posting(tmp_db):
    """A timed out Claude CLI call raises; the run keeps going and counts it."""
    client = make_client(worker_replies())
    ok = client.messages.create.side_effect
    client.messages.create.side_effect = [RuntimeError("claude CLI timed out after 180s"), *ok]
    posts = [
        raw_posting(hash="a", title="Data Analyst"),
        raw_posting(hash="b", title="AI Engineer Intern"),
    ]
    out = scoring.parse_node(base_state(tmp_db, client, new_postings=posts))
    assert out["stats"]["parse_failures"] == 1
    assert "timed out" in out["stats"]["parse_failure_detail"][0]["error"]
    assert [p["title"] for p in out["parsed"]] == ["AI Engineer Intern"]


def test_the_read_budget_goes_to_fresh_varied_nyc_roles_first() -> None:
    def day(n: int) -> str:
        return (config.utc_now() - timedelta(days=n)).isoformat()

    def p(company: str, title: str, location: str, age: int) -> dict[str, Any]:
        return {"company": company, "title": title, "location": location, "posted_at": day(age)}

    rows = [
        p("Old Co", "Data Scientist", "New York, NY", 30),
        p("Big", "Data Scientist", "New York, NY", 1),
        p("Big", "Data Engineer", "New York, NY", 1),
        p("Gig", "Chemistry - Analyst - Phoenix, US", "Phoenix, AZ", 1),
        p("Gig", "Chemistry - Analyst - Tulsa, US", "Tulsa, OK", 1),
        p("Small", "Machine Learning Engineer", "New York, NY", 2),
        p("Far", "Data Scientist", "Austin, TX", 0),
    ]
    order = [(r["company"], r["title"][:9]) for r in scoring.reading_order(rows)]
    assert order[:3] == [("Big", "Data Scie"), ("Small", "Machine L"), ("Big", "Data Engi")]
    assert order.index(("Far", "Data Scie")) < order.index(("Old Co", "Data Scie"))
    assert order.index(("Gig", "Chemistry")) < len(order) - 2  # one copy early, its twin late
    assert order[-1] == ("Old Co", "Data Scie") or order[-2] == ("Old Co", "Data Scie")
