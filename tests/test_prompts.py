"""The prompt registry: every prompt carries a readable version, a changed text
needs a new version, a wording only change keeps stored scores current, and
no prompt assumes the candidate's level or gender."""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any

import pytest

from sourcer import config, llm
from tests.conftest import make_client, worker_replies

# The hash of each prompt's fixed text, per version. Changing a prompt's text
# fails this test: give the prompt a new version, add its line here, and keep
# the old lines. Mark the new version compatible only when the change is
# wording only (see llm.Prompt).
PINNED = {
    ("triage", 2): "b25968e0b094",
    ("extract", 1): "e4edd4184600",
    ("fit_checks", 2): "12905cea2473",
    ("skills_score", 1): "3d2d3e74902c",
    ("parse_score", 1): "e016f2e84d26",
    ("facts", 1): "415182a28519",
}

# Prompt hashes stored score traces carry from before prompts were versioned
# (fit checks version 1, gendered wording), rendered with the fixtures below.
# score_basis must keep accepting them, or every stored score goes stale.
EVIDENCE = "Built a CFO tool."
WANTS = {
    "target_work": "Turning business problems into AI tools for clients.",
    "avoid_work": "LeetCode",
}
SKILLS: dict[str, Any] = {"skills": [{"skill": "Python", "weight": 10}]}
V1_HASHES = {
    "no profile": {"b7fe08d1df09", "3b5e1a4a817b"},
    "wants": {"d0390beb4818", "7276c5587222"},
}
SKILLS_HASHES = {"1f4091efd20e", "60725c8f0354"}

GENDERED = re.compile(r"\b(she|her|hers|herself|he|him|his|himself)\b", re.I)


def unpinned(prompts: dict[str, llm.Prompt]) -> list[str]:
    return [
        p.label for p in prompts.values() if PINNED.get((p.name, p.version)) != llm._sha(p.text)
    ]


def evidence_on(monkeypatch: pytest.MonkeyPatch, profile: dict[str, Any] | None) -> None:
    monkeypatch.setattr(config, "EVIDENCE_SCORING", True)
    monkeypatch.setattr(config, "load_evidence", lambda: EVIDENCE)
    monkeypatch.setattr(config, "load_profile", lambda path=None: profile)


def test_every_prompt_text_is_pinned_to_its_version() -> None:
    assert unpinned(llm.PROMPTS) == []
    assert set(llm.SCORING_PROMPTS) <= set(llm.PROMPTS)


def test_a_changed_text_without_a_new_version_fails() -> None:
    fit = llm.PROMPTS["fit_checks"]
    edited = {**llm.PROMPTS, "fit_checks": dataclasses.replace(fit, text=fit.text + " ")}
    assert unpinned(edited) == ["fit_checks/2"]
    bumped = {**edited, "fit_checks": dataclasses.replace(edited["fit_checks"], version=3)}
    assert unpinned(bumped) == ["fit_checks/3"]  # a new version needs its own pinned line


def test_compatible_versions_are_earlier_and_renderable() -> None:
    for p in llm.PROMPTS.values():
        assert all(v < p.version for v in p.compatible), p.label
    fit = llm.PROMPTS["fit_checks"]
    assert set(llm.CHECKS_WORDING) == {fit.version, *fit.compatible}
    assert fit.label == "fit_checks/2"


@pytest.mark.parametrize(("name", "profile"), [("no profile", None), ("wants", WANTS)])
def test_scores_read_with_version_1_stay_current(
    monkeypatch: pytest.MonkeyPatch, name: str, profile: dict[str, Any] | None
) -> None:
    evidence_on(monkeypatch, profile)
    basis = llm.score_basis(SKILLS)
    assert V1_HASHES[name] <= basis["prompts"]
    # and today's wording is current too
    assert llm._sha(llm.score_prompt(SKILLS).replace(EVIDENCE, "")) in basis["prompts"]
    old = {"input": {**{k: basis[k] for k in ("model", "evidence", "scorer")}}}
    for prompt in V1_HASHES[name]:
        assert llm.stale_reason({"input": {**old["input"], "prompt": prompt}}, basis) is None
        tagged = {**old["input"], "prompt": prompt, "prompt_version": "fit_checks/1"}
        assert llm.stale_reason({"input": tagged}, basis) is None


def test_skills_scoring_hashes_did_not_move(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "EVIDENCE_SCORING", False)
    monkeypatch.setattr(config, "load_profile", lambda path=None: None)
    assert llm.score_basis(SKILLS)["prompts"] == SKILLS_HASHES


def test_a_profile_change_still_makes_scores_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence_on(monkeypatch, None)
    before = llm.score_basis(SKILLS)
    evidence_on(monkeypatch, WANTS)
    after = llm.score_basis(SKILLS)
    assert not before["prompts"] & after["prompts"]


def test_an_unknown_or_dropped_version_is_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence_on(monkeypatch, WANTS)
    basis = llm.score_basis(SKILLS)
    assert basis["versions"] == {
        "fit_checks/2",
        "fit_checks/1",
        "skills_score/1",
        "parse_score/1",
    }
    note = {k: basis[k] for k in ("model", "evidence", "scorer")}
    prompt = next(iter(basis["prompts"]))
    trace = {"input": {**note, "prompt": prompt, "prompt_version": "fit_checks/9"}}
    assert llm.stale_reason(trace, basis) == "another prompt version"
    # a version no longer marked compatible: its scores go stale
    fit = llm.PROMPTS["fit_checks"]
    monkeypatch.setitem(llm.PROMPTS, "fit_checks", dataclasses.replace(fit, compatible=()))
    narrow = llm.score_basis(SKILLS)
    assert "fit_checks/1" not in narrow["versions"]
    assert not V1_HASHES["wants"] & narrow["prompts"]
    trace = {"input": {**note, "prompt": prompt, "prompt_version": "fit_checks/1"}}
    assert llm.stale_reason(trace, narrow) == "another prompt version"


def test_a_basis_without_versions_falls_back_to_the_hash() -> None:
    basis = {"scorer": "s", "prompts": {"p"}, "evidence": None, "model": "m"}
    note = {"scorer": "s", "prompt": "p", "evidence": None, "model": "m"}
    assert llm.stale_reason({"input": {**note, "prompt_version": "fit_checks/2"}}, basis) == (
        "another prompt version"
    )
    assert llm.stale_reason({"input": note}, basis) is None


TEXT = "Summer 2027 internship. Build Python data tools with finance teams."
POSTING = {
    "company": "A",
    "title": "Data Intern",
    "location": "NY",
    "url": "u",
    "description": TEXT,
}


def test_every_score_trace_names_its_prompt_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    # skills scoring: the trace parse_and_score writes
    monkeypatch.setattr(config, "EVIDENCE_SCORING", False)
    client = make_client(worker_replies())
    data, err = llm.parse_and_score(client, POSTING, SKILLS, llm.Usage())
    assert err is None and data is not None
    trace = data["score_trace"]
    assert trace["input"]["prompt_version"] == "skills_score/1"
    assert trace["facts"]["prompt_version"] == "extract/1"
    # evidence scoring: the fit checks trace
    evidence_on(monkeypatch, None)
    checks = {
        "role_fit": "core",
        "must_haves": [{"need": "Python", "covered": "yes"}],
        "stretch": "none",
        "rationale": "Built a CFO tool.",
    }
    client = make_client([*worker_replies(scored=False), json.dumps(checks)])
    data, err = llm.parse_and_score(client, POSTING, SKILLS, llm.Usage(), steady=False)
    assert err is None and data is not None
    note = data["score_trace"]["input"]
    assert note["prompt_version"] == "fit_checks/2"
    assert llm.stale_reason(data["score_trace"], llm.score_basis(SKILLS)) is None


def test_no_prompt_assumes_a_gender(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence_on(monkeypatch, WANTS)
    sent = [
        llm.triage_system(WANTS),
        llm.score_prompt(SKILLS),
        llm.parse_score_prompt(POSTING, SKILLS)[0],
        llm.EXTRACT_SYSTEM,
        llm.FACTS_SYSTEM,
        llm.SCORE_SYSTEM,
    ]
    for text in sent:
        assert not GENDERED.search(text.replace(EVIDENCE, "")), GENDERED.findall(text)


def test_the_title_check_takes_the_level_from_the_profile() -> None:
    level = "mid career, three to six years in data roles"
    prompt = llm.triage_system({**WANTS, "target_level": level})
    assert f"TARGET LEVEL: {level}." in prompt
    assert "early career" not in prompt.split("\n", 1)[0]
    default = llm.triage_system({})
    assert f"TARGET LEVEL: {llm.DEFAULT_TARGET_LEVEL}." in default
    assert llm.DEFAULT_TARGET_WORK in default and "nothing stated" in default


def test_candidate_level_reads_the_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    assert config.candidate_level({"target_level": "  entry level  "}) == "entry level"
    assert config.candidate_level({}) == ""
    monkeypatch.setattr(config, "load_profile", lambda path=None: None)
    assert config.candidate_level() == ""


def test_prompt_builders_render() -> None:
    assert llm.PROMPTS["extract"].build() == llm.EXTRACT_SYSTEM
    assert llm.PROMPTS["facts"].build() == llm.FACTS_SYSTEM
    assert "TARGET LEVEL" in llm.PROMPTS["triage"].build({})
