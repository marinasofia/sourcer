"""Computed skill weights, their reasons, overrides and the skills command,
on made up facts and a made up local database."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from sourcer import config, db, llm, main
from sourcer.onboard import cli, store, weights
from tests.test_onboard_cli import Script

TODAY = date(2026, 10, 30)


def fact(skills: list[str], **over: Any) -> dict[str, Any]:
    base = {
        "id": "f" + "".join(skills) + str(over.get("group", "")) + str(over.get("source_type", "")),
        "status": "approved",
        "skills": skills,
        "kind": "project",
        "group": "Project A",
        "dates": "2026-10",
        "source_type": "github",
        "source_id": "github:x",
        "item_id": "github:x/a#0",
        "text": "Built a thing.",
        "quote": "Built a thing",
        "source_ref": "https://github.com/octo-example/a",
    }
    return {**base, **over}


def test_latest_month_reads_every_date_shape() -> None:
    assert weights.latest_month("June 2024 to August 2024", TODAY) == (2024, 8)
    assert weights.latest_month("Sept. 2023 to present", TODAY) == (2026, 10)
    assert weights.latest_month("2025-03", TODAY) == (2025, 3)
    assert weights.latest_month("03/2022", TODAY) == (2022, 3)
    assert weights.latest_month("2021", TODAY) == (2021, 12)
    assert weights.latest_month("2030", TODAY) is None  # past next year: not a date
    assert weights.latest_month("Fall 2027", TODAY) == (2026, 10)  # never after today
    assert weights.latest_month("", TODAY) is None


def test_compute_depth_recency_agreement_and_reason() -> None:
    facts = [
        fact(["Python", "SQL"], kind="job", group="Analyst, Example Co",
             dates="June 2024 to present", source_type="resume",
             text="Built dashboards used by 40 users."),
        fact(["python"], group="Thesis", dates="2026-10"),
        fact(["Python"], group="Bot", dates="2025-01", source_type="folder"),
        fact(["Postgres"], group="", item_id="folder:y/db#0", dates="", source_type="folder"),
        fact(["Excel"], status="pending"),  # only approved facts count
    ]  # fmt: skip
    got = {e["skill"]: e for e in weights.compute(facts, None, TODAY)}
    assert list(got) == ["Python", "SQL", "PostgreSQL"]
    py = got["Python"]
    assert py["source"] == "computed" and py["category"] == "Languages"
    assert py["synonyms"] == []  # "python" differs only in case
    assert py["reason"] == (
        "2 projects and 1 job with shipped work, used this month, named by resume, GitHub "
        "and project folders, demand not counted, no local postings yet"
    )
    # depth 3/5*0.8+0.2, recency 1, agreement 1, no demand: 1 + 9 * 0.622 / 0.75 = 8.46
    assert py["weight"] == 8
    pg = got["PostgreSQL"]
    assert pg["synonyms"] == ["Postgres"] and pg["category"] == "Data"
    assert "no dates" in pg["reason"] and pg["weight"] == 2


def test_recency_phrases() -> None:
    def reason(dates: str) -> str:
        return str(weights.compute([fact(["Rust"], dates=dates)], None, TODAY)[0]["reason"])

    assert "used 4 months ago" in reason("June 2026")
    assert "used 1 month ago" in reason("2026-09")
    assert "last used in 2023" in reason("2023")


def test_demand_counts_only_with_enough_postings() -> None:
    facts = [fact(["Go"]), fact(["Kubernetes"])]
    few = ["Go and Kubernetes"] * 5
    got = weights.compute(facts, few, TODAY)
    assert "only 5 matching postings (needs 30)" in got[0]["reason"]
    many = ["We use Go and k8s."] * 15 + ["Let's go to the office."] * 15
    by = {e["skill"]: e for e in weights.compute(facts, many, TODAY)}
    # "go" in prose is not the Go language; the k8s synonym counts
    assert "in 50% of matching postings" in by["Go"]["reason"]
    assert "in 50% of matching postings" in by["Kubernetes"]["reason"]
    none = {e["skill"]: e for e in weights.compute(facts, ["Java only."] * 30, TODAY)}
    assert none["Go"]["weight"] < by["Go"]["weight"]


def test_demand_reads_matching_postings_from_the_local_db(
    onboard_paths: Path, posting_row: Callable[..., dict[str, Any]]
) -> None:
    assert weights.demand_texts() is None  # no database yet, and none is created
    assert not config.DB_PATH.exists()
    conn = db.connect()
    db.insert_posting(conn, posting_row(title="Data Analyst Intern", url="u1",
                                        description="SQL and Tableau daily."))  # fmt: skip
    db.insert_posting(conn, posting_row(title="Chef", url="u2", description="SQL"))
    conn.commit()
    conn.close()
    texts = weights.demand_texts()
    assert texts is not None and len(texts) == 1 and "Tableau" in texts[0]


def test_demand_survives_a_broken_db(onboard_paths: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.DB_PATH.write_text("not a database")
    assert weights.demand_texts() is None
    monkeypatch.setattr(weights.db, "BACKEND", "snowflake")
    assert weights.demand_texts() is None


def test_recompute_keeps_overrides_and_spares_hand_written(onboard_paths: Path) -> None:
    facts = [fact(["Python"]), fact(["SQL"], group="Other")]
    assert weights.recompute(facts).written
    data = json.loads(config.SKILLS_PATH.read_text())
    assert {e["skill"] for e in data["skills"]} == {"Python", "SQL"}
    assert "Computed by sourcer" in data["meta"]["source"]
    entry = weights.set_override(facts, "python", 3)
    assert entry["weight"] == 3 and entry["source"] == "override"
    assert entry["reason"].startswith("set by you; computed ")
    weights.set_override(facts, "Docker", 9)  # no fact names it, still kept
    weights.recompute(facts)
    by = {e["skill"]: e for e in weights.load_vector()["skills"]}
    assert by["Python"]["weight"] == 3 and by["Docker"]["weight"] == 9
    assert by["Docker"]["reason"] == "set by you; no approved fact names it"
    assert weights.reset_override(facts, "PYTHON")
    assert not weights.reset_override(facts, "Python")
    with pytest.raises(ValueError, match="1 to 10"):
        weights.set_override(facts, "Python", 11)
    # a vector written by hand is never replaced without --force
    hand = {"skills": [{"skill": "Rust", "weight": 10, "category": "Languages", "synonyms": []}]}
    config.SKILLS_PATH.write_text(json.dumps(hand))
    done = weights.recompute(facts)
    assert not done.written and "hand written" in done.note
    with pytest.raises(ValueError, match="hand written"):
        weights.set_override(facts, "Python", 4)
    with pytest.raises(ValueError, match="hand written"):
        weights.reset_override(facts, "Python")
    assert json.loads(config.SKILLS_PATH.read_text()) == hand
    assert weights.recompute(facts, force=True).written
    backup = config.SKILLS_PATH.with_name("skills_vector.json.bak")
    assert json.loads(backup.read_text()) == hand
    assert weights.load_vector()["skills"][0]["source"] == "computed"


def test_load_vector_tolerates_junk(onboard_paths: Path) -> None:
    assert weights.load_vector() == {"skills": []}
    config.SKILLS_PATH.write_text("[1, 2]")
    assert weights.load_vector() == {"skills": []}


def test_skills_command(onboard_paths: Path) -> None:
    facts = store.load_facts()
    facts["facts"] = [fact(["Python"]), fact(["SQL"], group="B")]
    store.save_facts(facts)
    script = Script()
    assert cli.main(["skills"], console=script.console()) == 0
    assert "No skill weights yet" in script.text
    assert cli.main(["skills", "compute"], console=script.console()) == 0
    assert "Python" in script.text and "named by GitHub" in script.text
    script = Script()
    assert cli.main(["skills", "set", "SQL", "9"], console=script.console()) == 0
    assert "SQL is now 9" in script.text
    assert cli.main(["skills"], console=script.console()) == 0
    assert "  9  SQL" in script.text and "(yours)" in script.text
    assert cli.main(["skills", "reset", "SQL"], console=script.console()) == 0
    assert cli.main(["skills", "reset", "SQL"], console=script.console()) == 1
    assert "has no weight set by you" in script.text
    config.SKILLS_PATH.write_text(json.dumps({"skills": [{"skill": "R", "weight": 1}]}))
    script = Script()
    assert cli.main(["skills"], console=script.console()) == 0
    assert "hand written" in script.text
    assert cli.main(["skills", "compute"], console=script.console()) == 1
    assert cli.main(["skills", "set", "R", "2"], console=script.console()) == 1
    assert cli.main(["skills", "compute", "--force"], console=script.console()) == 0
    assert cli.skills_cmd(Script().console(), "set", "R", None) == 1  # no weight given


def test_review_recomputes_weights(onboard_paths: Path) -> None:
    facts = store.load_facts()
    facts["facts"] = [fact(["Python"], status="pending")]
    store.save_facts(facts)
    script = Script("a")
    assert cli.main(["facts", "review"], console=script.console()) == 0
    assert "Skill weights updated: Python " in script.text
    assert weights.load_vector()["skills"][0]["skill"] == "Python"
    # forget --all puts the starter file back
    assert cli.main(["forget", "--all"], console=Script().console(yes=True)) == 0
    assert weights.load_vector()["skills"] == []


def test_scorer_ignores_reason_and_source_but_main_reads_them(paths: Path) -> None:
    entry = {"skill": "Python", "weight": 7, "category": "Languages", "synonyms": [],
             "reason": "1 project", "source": "computed"}  # fmt: skip
    suffix = llm._skills_suffix({"skills": [entry]})
    assert "reason" not in suffix and '"source"' not in suffix and '"weight":7' in suffix
    plain = {"skill": "SQL", "weight": 9}
    assert llm._skills_suffix({"skills": [plain]}).endswith(
        json.dumps([plain], separators=(",", ":"))
    )
    config.SKILLS_PATH.write_text(json.dumps({"skills": [entry]}))
    state = main.build_state(client=MagicMock())
    state["conn"].close()
    assert state["skills"]["skills"][0]["reason"] == "1 project"
