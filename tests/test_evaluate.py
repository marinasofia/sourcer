"""Evaluation harness: the shipped labelled set must agree with the rules,
and the reporting arithmetic must be right on synthetic sets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sourcer import config, evaluate, llm
from tests.conftest import GOOD_PARSE_SCORE, PROFILE, SKILLS, make_client, worker_replies


def test_shipped_labelled_set_has_no_mismatches() -> None:
    records = evaluate.load_labelled()
    data = json.loads(evaluate.DEFAULT_LABELLED.read_text())
    report = evaluate.evaluate_rules(records, data["profile"])
    assert report["mismatches"] == []
    assert report["exclusion"]["precision"] == 1.0
    assert report["exclusion"]["recall"] == 1.0
    assert report["level_fit_agreement"] == 1.0


def test_load_labelled_accepts_a_bare_list_and_rejects_bad_records(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps([{"id": "a", "posting": {"title": "x"}, "expected": {}}]))
    assert evaluate.load_labelled(good)[0]["id"] == "a"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"id": "a", "posting": {}}]))
    with pytest.raises(ValueError, match="expected"):
        evaluate.load_labelled(bad)


def test_rules_report_counts_every_confusion_cell() -> None:
    records = [
        {"id": "tp", "posting": {"title": "Senior X"}, "expected": {"excluded": True}},
        {"id": "fp", "posting": {"title": "Staff Y"}, "expected": {"excluded": False}},
        {"id": "fn", "posting": {"title": "AI Intern"}, "expected": {"excluded": True}},
        {"id": "tn", "posting": {"title": "AI Intern"}, "expected": {"excluded": False}},
        {
            "id": "wrong-reason",
            "posting": {"title": "Senior Z"},
            "expected": {"excluded": True, "reason_contains": "contract"},
        },
        {
            "id": "wrong-fit",
            "posting": {"title": "AI Intern", "seniority": "unspecified"},
            "expected": {"excluded": False, "level_fit": "fit"},
        },
    ]
    report = evaluate.evaluate_rules(records, PROFILE)
    ex = report["exclusion"]
    assert (ex["tp"], ex["fp"], ex["fn"], ex["tn"]) == (2, 1, 1, 2)
    assert {m["id"] for m in report["mismatches"]} == {"fp", "fn", "wrong-reason", "wrong-fit"}
    assert ex["precision"] == round(2 / 3, 4)
    assert ex["recall"] == round(2 / 3, 4)


def test_rules_report_on_empty_set_is_all_ones() -> None:
    report = evaluate.evaluate_rules([], None)
    assert report["exclusion"]["precision"] == 1.0
    assert report["level_fit_agreement"] == 1.0


def test_model_report_scores_only_records_with_bands_and_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "COST_CAP_USD", 10.0)
    records = [
        {
            "id": "in-band",
            "posting": {},
            "expected": {"score_band": [80, 100]},
            "scoring_input": {
                "company": "C",
                "title": "T",
                "location": "L",
                "url": "u",
                "description": "d",
            },
        },
        {"id": "no-band", "posting": {}, "expected": {}, "scoring_input": {"description": "d"}},
        {"id": "no-input", "posting": {}, "expected": {"score_band": [0, 10]}},
        {
            "id": "invalid",
            "posting": {},
            "expected": {"score_band": [0, 10]},
            "scoring_input": {
                "company": "C",
                "title": "T",
                "location": "L",
                "url": "u",
                "description": "d",
            },
        },
    ]
    client = make_client([*worker_replies(), "garbage", "garbage"])
    report = evaluate.evaluate_model(records, client, SKILLS, llm.Usage())
    assert report["scored"] == 1
    assert report["failed"] == 1
    assert report["band_agreement"] == 1.0
    assert report["mean_abs_error"] == abs(GOOD_PARSE_SCORE["score"] - 90)


def test_model_report_respects_cost_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "COST_CAP_USD", 0.0)
    records = [
        {
            "id": "capped",
            "posting": {},
            "expected": {"score_band": [0, 100]},
            "scoring_input": {
                "company": "C",
                "title": "T",
                "location": "L",
                "url": "u",
                "description": "d",
            },
        }
    ]
    client = make_client([])
    report = evaluate.evaluate_model(records, client, SKILLS, llm.Usage())
    assert report["skipped_cost_cap"] == 1
    assert report["mean_abs_error"] is None
    client.messages.create.assert_not_called()


def test_main_exits_nonzero_on_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    labelled = tmp_path / "l.json"
    labelled.write_text(
        json.dumps([{"id": "x", "posting": {"title": "Senior X"}, "expected": {"excluded": False}}])
    )
    assert evaluate.main(["--labelled", str(labelled)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["rules"]["mismatches"][0]["id"] == "x"


def test_main_with_model_flag(
    paths: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config, "COST_CAP_USD", 10.0)
    monkeypatch.setattr(evaluate.llm, "get_client", lambda: make_client(worker_replies()))
    labelled = paths / "l.json"
    labelled.write_text(
        json.dumps(
            [
                {
                    "id": "x",
                    "posting": {"title": "AI Intern"},
                    "expected": {"excluded": False, "score_band": [0, 100]},
                    "scoring_input": {
                        "company": "C",
                        "title": "T",
                        "location": "L",
                        "url": "u",
                        "description": "d",
                    },
                }
            ]
        )
    )
    assert evaluate.main(["--labelled", str(labelled), "--model"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["model"]["scored"] == 1
