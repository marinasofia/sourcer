"""Matching fixes: fit judged from the candidate's evidence, internships read
as internships, the boroughs counted as NYC, and early career titles kept."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sourcer import config, llm
from sourcer.pipeline import scoring

PROFILE = {"education": "Bachelor's", "in_progress": True, "grad_year": 2027, "grad_month": 5,
           "max_years_experience": 2, "earliest_start_full_time": "2027-05"}  # fmt: skip
SKILLS = {"skills": [{"name": "Python", "weight": 10}]}
POSTING = {"company": "Acme", "title": "AI Intern", "location": "NYC", "url": "u",
           "description": "Build agents with MCP."}  # fmt: skip


@pytest.fixture
def evidence_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ev = tmp_path / "evidence.txt"
    ev.write_text("Sourcer\n- Deployed scoring as an MCP server.\n")
    monkeypatch.setattr(config, "EVIDENCE_PATH", ev)
    monkeypatch.setattr(config, "EVIDENCE_SCORING", True)
    return ev


def test_skills_scoring_stays_until_evidence_scoring_is_turned_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ev = tmp_path / "evidence.txt"
    ev.write_text("Built agents.")
    monkeypatch.setattr(config, "EVIDENCE_PATH", ev)
    system, _ = llm.parse_score_prompt(POSTING, SKILLS)
    assert "Candidate skills (weighted" in system and "CANDIDATE EVIDENCE" not in system
    assert llm.parse_score_required() is llm.PARSE_SCORE_REQUIRED


def test_fit_checks_against_evidence_replace_the_single_number(evidence_on: Path) -> None:
    system, user = llm.parse_score_prompt(POSTING, SKILLS)
    assert "CANDIDATE EVIDENCE:\nSourcer\n- Deployed scoring as an MCP server." in system
    assert '"role_fit"' in system and '"score"' not in system
    assert "Candidate skills (weighted" not in system and "Build agents with MCP." in user
    assert llm.parse_score_required() is llm.PARSE_CHECKS_REQUIRED


def test_the_named_checks_travel_with_the_score() -> None:
    out = llm.score_from_checks(
        {
            "role_fit": "adjacent",
            "stretch": "none",
            "must_haves": [{"need": "SQL", "covered": "yes"}, {"need": "React", "covered": "no"}],
            "rationale": "Built dashboards.",
        }
    )
    checks = out["fit_checks"]
    assert checks["fit"] == "adjacent" and checks["stretch"] == "none" and not checks["assumed"]
    assert checks["must_haves"] == [
        {"need": "SQL", "covered": "yes"},
        {"need": "React", "covered": "no"},
    ]
    assert checks["formula"] == "adjacent: 40 + 35 x 0.50 coverage = 58"  # recomputable by hand


def test_no_must_haves_is_marked_as_an_assumed_coverage() -> None:
    out = llm.score_from_checks({"role_fit": "adjacent", "stretch": "none", "must_haves": []})
    assert out["score"] == 58 and out["fit_checks"]["assumed"] is True


@pytest.mark.parametrize(
    ("checks", "score", "start"),
    [
        ({"role_fit": "core", "stretch": "none", "must_haves": [
            {"need": "LLMs", "covered": "yes"}, {"need": "MCP", "covered": "yes"}]},
         95, "Core fit, covers 2 of 2 must haves."),
        ({"role_fit": "core", "stretch": "none", "must_haves": [
            {"need": "Python", "covered": "yes"}, {"need": "Kubernetes", "covered": "no"},
            {"need": "SQL", "covered": "partly"}, {"need": "Go", "covered": "no"}]},
         70, "Core fit, covers 1 of 4 must haves, missing Kubernetes, Go."),
        ({"role_fit": "adjacent", "stretch": "none", "must_haves": [
            {"need": "SQL", "covered": "yes"}]}, 75, "Adjacent fit, covers 1 of 1"),
        ({"role_fit": "core", "stretch": "research", "must_haves": [
            {"need": "ML", "covered": "yes"}]}, 75, "Core fit, covers 1 of 1 must haves, scoped"),
        ({"role_fit": "other", "stretch": "none", "must_haves": []}, 20, "Other fit."),
    ],
)  # fmt: skip
def test_score_from_checks(checks: dict[str, Any], score: int, start: str) -> None:
    out = llm.score_from_checks({**checks, "rationale": "Built an MCP server."})
    assert out["score"] == score and out["rationale"].startswith(start)
    assert out["rationale"].endswith("Built an MCP server.")
    assert not {"role_fit", "must_haves", "stretch"} & set(out)


def test_rescoring_sends_the_posting_text_and_computes_the_score(
    evidence_on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake(client: Any, system: str, user: str, required: Any, usage: Any, **kw: Any) -> Any:
        seen.update(system=system, user=user, required=required)
        return {"role_fit": "core", "stretch": "none", "rationale": "MCP server.",
                "must_haves": [{"need": "MCP", "covered": "yes"}]}, None  # fmt: skip

    monkeypatch.setattr(llm, "json_call", fake)
    parsed = {"title": "AI Intern", "requirements": ["MCP"], "tech_stack": [],
              "seniority": "intern", "description": "Ship agents on MCP."}  # fmt: skip
    data, err = llm.score_posting(None, parsed, SKILLS, llm.Usage())  # type: ignore[arg-type]
    assert err is None and data is not None and data["score"] == 95
    assert seen["required"] is llm.CHECKS_REQUIRED
    assert (
        json.loads(seen["user"].split("<posting>\n", 1)[1].rsplit("\n</posting>", 1)[0])["text"]
        == "Ship agents on MCP."
    )


def test_an_internship_title_is_never_scored_as_full_time() -> None:
    """Disney's Spring 2027 internship was read as full time and excluded
    for starting before graduation."""
    parsed = {"company": "Disney", "title": "Marketplace Insights Intern, Spring 2027",
              "url": "u", "location": "New York, NY", "source": "ats", "requirements": [],
              "tech_stack": [], "seniority": "intern", "employment_type": "full time",
              "start_date": "January 2027", "years_experience_min": None,
              "work_mode": "onsite", "score": 81, "rationale": "fits"}  # fmt: skip
    out = scoring.score_node({"parsed": [parsed], "stats": {}, "profile": PROFILE})
    row = out["scored"][0]
    assert row["employment_type"] == "intern" and row["excluded"] is False and row["score"] == 81


def _checks(covered: list[str], fit: str = "core") -> dict[str, Any]:
    musts = [{"need": f"skill {i}", "covered": c} for i, c in enumerate(covered)]
    return {"role_fit": fit, "stretch": "none", "must_haves": musts, "rationale": "r"}


ALL = ["yes", "yes", "yes", "yes"]


@pytest.mark.parametrize(
    ("runs", "calls", "kept"),
    [
        ([["yes", "yes", "partly", "no"], ["yes", "no", "no", "no"]], 2, 65),  # 80 then 65
        ([["yes", "yes", "partly", "no"], ALL], 2, 80),  # higher: keep 80
        ([ALL, ALL], 2, 95),  # a core fit above the band is still checked
        ([["no", "no", "no", "no"]], 1, 55),  # 55 is below the band: one call
        ([(ALL, "adjacent"), (ALL, "adjacent")], 2, 75),  # fits agree: no third call
        ([ALL, (ALL, "adjacent"), ALL], 3, 95),  # fits disagree: two of three say core
        ([ALL, (ALL, "adjacent"), (ALL, "adjacent")], 3, 75),  # two of three say adjacent
        ([ALL, (ALL, "adjacent"), (ALL, "other")], 3, 30),  # three reads: the lowest
    ],
)
def test_scores_near_the_cutoff_are_checked_twice_and_keep_the_lower(
    evidence_on: Path, monkeypatch: pytest.MonkeyPatch, runs: list[Any], calls: int, kept: int
) -> None:
    replies = iter(runs)
    made: list[int] = []

    def fake(*a: Any, **kw: Any) -> Any:
        made.append(1)
        run = next(replies)
        return (_checks(*run) if isinstance(run, tuple) else _checks(run)), None

    monkeypatch.setattr(llm, "json_call", fake)
    parsed = {"title": "AI Intern", "requirements": [], "tech_stack": [], "seniority": "intern"}
    data, _ = llm.score_posting(None, parsed, SKILLS, llm.Usage())  # type: ignore[arg-type]
    assert data is not None and data["score"] == kept and len(made) == calls


def test_parse_and_score_also_steadies(evidence_on: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = {"title": "AI Intern", "location": "NYC", "requirements": ["MCP"],
             "seniority": "intern", "tech_stack": [], "employment_type": "intern",
             "start_date": None,
             "years_experience_min": None, "work_mode": "onsite",
             **_checks(["yes", "yes", "partly", "no"])}  # fmt: skip
    replies = iter([(first, None), (_checks(["no", "no", "no", "no"]), None)])
    monkeypatch.setattr(llm, "json_call", lambda *a, **kw: next(replies))
    data, _ = llm.parse_and_score(None, POSTING, SKILLS, llm.Usage())  # type: ignore[arg-type]
    assert data is not None and data["score"] == 55 and data["employment_type"] == "intern"
    trace = data["score_trace"]  # both reads kept, the lower one marked, and why
    assert [r["score"] for r in trace["reads"]] == [80, 55] and trace["kept"] == 1
    assert trace["rule"].startswith("near the texting cutoff")
    assert trace["input"]["chars"] > 0 and trace["reads"][1]["formula"].endswith("= 55")


def test_a_person_reading_gets_one_look_on_the_model_asked_for(
    evidence_on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = {"title": "AI Intern", "location": "NYC", "requirements": ["MCP"],
             "seniority": "intern", "tech_stack": [], "employment_type": "intern",
             "start_date": None,
             "years_experience_min": None, "work_mode": "onsite",
             **_checks(["yes", "yes", "partly", "no"])}  # fmt: skip
    models: list[Any] = []

    def fake(*a: Any, **kw: Any) -> Any:
        models.append(kw.get("model"))
        return first, None

    monkeypatch.setattr(llm, "json_call", fake)
    data, _ = llm.parse_and_score(
        None,  # type: ignore[arg-type]
        POSTING,
        SKILLS,
        llm.Usage(),
        model="claude-haiku-4-5",
        steady=False,
    )
    assert data is not None and models == ["claude-haiku-4-5"]  # one call, no second look


@pytest.mark.parametrize(
    "location", ["Queens, NY", "Bronx, NY", "Long Island City, NY", "Jersey City, NJ", "Hoboken"]
)
def test_boroughs_and_commuter_cities_count_as_nyc(location: str) -> None:
    assert config.NYC_RE.search(location)


@pytest.mark.parametrize(
    ("title", "senior"),
    [
        ("Program Manager Intern", False),
        ("Associate Product Manager, New Grad", False),
        ("Team Lead, Rotational Program", False),
        ("Data Analyst II", False),
        ("Business Analyst 2", False),
        ("Program Manager", True),
        ("Senior Data Analyst", True),
        ("Software Engineer II", True),
        ("Senior Manager, Internship Program", True),
    ],
)
def test_early_career_titles_are_not_senior(title: str, senior: bool) -> None:
    assert config.is_senior_title(title) is senior
