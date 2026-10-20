"""Edge scoring: the work she wants, in her words, with every answer quoted."""

from __future__ import annotations

import json
from typing import Any

import pytest

from sourcer import config, db, llm
from sourcer.pipeline import source

TEXT = (
    "You will partner with finance teams to turn their messy workflows into AI tools. "
    "Expect a LeetCode style coding round on algorithms and data structures."
)
WANTS = {
    "target_work": "Turning business problems into AI tools for clients.",
    "avoid_work": "LeetCode",
}
POSTING = {"company": "A", "title": "T", "location": "NY", "url": "u", "description": TEXT}


def checks(**edge: Any) -> dict[str, Any]:
    return {
        "role_fit": "core",
        "stretch": "none",
        "must_haves": [{"need": "Python", "covered": "yes"}],
        "rationale": "Built a CFO tool.",
        **edge,
    }


def test_a_quoted_edge_is_blended_with_the_skills_score() -> None:
    quote = "turn their messy workflows into AI tools"
    out = llm.score_from_checks(checks(edge=5, edge_quote=quote, coding_gate="no"), TEXT)
    # skills 95, edge 5 of 5: 0.6 x 100 + 0.4 x 95 = 98, calibrated 99
    assert out["fit_checks"]["raw_score"] == 98 and out["score"] == llm.calibrate(98) == 99
    assert out["fit_checks"]["edge_quoted"] and out["fit_checks"]["skills_score"] == 95
    assert out["rationale"].startswith("Core fit, covers 1 of 1 must haves, edge 5 of 5.")
    assert out["rationale"].endswith("Built a CFO tool.")
    assert not {"edge", "edge_quote", "coding_gate"} & set(out)


def test_an_edge_quote_not_in_the_posting_is_not_trusted() -> None:
    out = llm.score_from_checks(
        checks(edge=5, edge_quote="You will lead AI strategy for big CFOs", coding_gate="no"),
        TEXT,
    )
    assert out["fit_checks"]["edge"] == llm.EDGE_UNQUOTED_CAP
    assert out["fit_checks"]["edge_said"] == 5
    assert "(unquoted)" in out["rationale"]
    assert out["fit_checks"]["raw_score"] == round(0.6 * 40 + 0.4 * 95)


def test_a_quoted_coding_gate_demotes_and_an_unquoted_one_does_not() -> None:
    quote = "turn their messy workflows into AI tools"
    gated = llm.score_from_checks(
        checks(edge=5, edge_quote=quote, coding_gate="yes",
               coding_quote="Expect a LeetCode style coding round"),
        TEXT,
    )  # fmt: skip
    assert gated["fit_checks"]["raw_score"] == 98 - llm.CODING_PENALTY
    assert gated["fit_checks"]["coding_gate"] and "coding interview gate" in gated["rationale"]
    unquoted = llm.score_from_checks(
        checks(edge=5, edge_quote=quote, coding_gate="yes", coding_quote="whiteboard rounds"), TEXT
    )
    assert unquoted["score"] == 99 and unquoted["fit_checks"]["coding_unquoted"]


@pytest.mark.parametrize(
    ("raw", "calibrated"),
    [(0, 0), (45, 50), (50, 62), (55, 75), (70, 85), (100, 100)],
)
def test_calibration_maps_the_gold_cutoffs_and_keeps_the_order(raw: int, calibrated: int) -> None:
    assert llm.calibrate(raw) == calibrated
    assert all(llm.calibrate(x) <= llm.calibrate(x + 1) for x in range(100))


@pytest.mark.parametrize(
    ("quote", "found"),
    [
        ("Turn their  messy workflows into AI tools.", True),  # case and spacing ignored
        ("partner with finance teams", True),
        ("AI tools", False),  # too short to prove anything
        ("partner with marketing teams", False),
    ],
)
def test_quoted_in(quote: str, found: bool) -> None:
    assert llm.quoted_in(quote, TEXT) is found


def test_no_edge_answer_keeps_the_old_score() -> None:
    assert llm.score_from_checks(checks(), TEXT)["score"] == 95


def test_edge_checks_follow_the_profile(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(config, "EVIDENCE_SCORING", True)
    monkeypatch.setattr(config, "load_evidence", lambda: "Built a CFO tool.")
    monkeypatch.setattr(config, "load_profile", lambda path=None: None)
    system, _ = llm.parse_score_prompt(POSTING, {"skills": []})
    assert "edge_quote" not in system and "edge" not in llm.parse_score_required()
    monkeypatch.setattr(config, "load_profile", lambda path=None: WANTS)
    system, _ = llm.parse_score_prompt(POSTING, {"skills": []})
    assert "WORK SHE WANTS: Turning business problems into AI tools" in system
    assert '"edge_quote"' in system and "edge" in llm.parse_score_required()


def test_the_title_check_reads_her_words_not_role_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "load_profile", lambda path=None: WANTS)
    prompt = llm.triage_system()
    assert "WORK SHE WANTS: Turning business problems into AI tools for clients." in prompt
    assert "WORK SHE AVOIDS: LeetCode" in prompt and '{"match": [numbers]}' in prompt
    monkeypatch.setattr(config, "load_profile", lambda path=None: None)
    assert llm.DEFAULT_TARGET_WORK in llm.triage_system()


def test_the_title_check_skips_postings_already_stored(fake_client, tmp_db) -> None:
    old = {"company": "Acme", "title": "Analyst", "location": "NYC", "url": "u1"}
    new = {"company": "Acme", "title": "Builder", "location": "NYC", "url": "u2"}
    tmp_db.execute(
        "INSERT INTO postings (hash, company, title, location, url, date_scraped, source) "
        "VALUES (?, 'Acme', 'Analyst', 'NYC', 'u1', '2026-10-10', 'ats')",
        (db.posting_hash("Acme", "Analyst", "NYC"),),
    )
    client = fake_client([json.dumps({"match": [1]})])
    state: Any = {"client": client, "conn": tmp_db, "usage": llm.Usage(), "stats": {}}
    postings: list[Any] = []
    assert source._triage(state, [old, new], postings) == 2  # both on to dedupe
    assert [p["url"] for p in postings] == ["u1", "u2"]
    sent = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "Builder" in sent and "Analyst" not in sent


def test_stale_reason_names_what_changed() -> None:
    skills: dict[str, Any] = {"skills": []}
    basis = llm.score_basis(skills)
    note = {k: basis[k] for k in ("model", "evidence", "scorer")}
    now = {"input": {**note, "prompt": next(iter(basis["prompts"]))}}
    assert llm.stale_reason(json.dumps(now), basis) is None
    for field, value, why in (
        ("scorer", "x", "older scoring arithmetic"),
        ("prompt", "x", "older prompt or profile"),
        ("evidence", "x", "older evidence"),
        ("model", "x", "another model"),
    ):
        assert llm.stale_reason({"input": {**now["input"], field: value}}, basis) == why
    assert llm.stale_reason(None, basis) == "no score trace"
    assert llm.stale_reason("not json", basis) == "no score trace"


def test_the_scorer_version_moves_with_the_calibration(monkeypatch: pytest.MonkeyPatch) -> None:
    before = llm.scorer_version()
    monkeypatch.setattr(llm, "CALIBRATION", ((0, 0), (100, 100)))
    assert llm.scorer_version() != before
