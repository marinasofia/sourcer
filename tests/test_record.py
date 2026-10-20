"""One scored posting the same way everywhere: judge and save_score."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sourcer import db
from sourcer.pipeline.record import judge, read_columns, save_score

PROFILE = {"needs_sponsorship": True, "grad_year": 2027, "grad_month": 5}
SPONSOR_LINE = "Requirements:\n- We are unable to sponsor visas now or in the future."


def test_a_read_that_passes_keeps_its_score_and_gets_its_timing_note() -> None:
    parsed = {"title": "Data Analyst", "employment_type": "unspecified", "start_date": None}
    j = judge(parsed, PROFILE, score="88", rationale="Fits. [timing unclear: old note]")
    assert j.reason is None and j.timing
    assert j.fields["score"] == 88 and j.fields["excluded"] is False
    assert (
        j.fields["rationale"].startswith("Fits. [timing unclear:")
        and "old note" not in j.fields["rationale"]
    )
    assert j.fields["level_fit"] in {"fit", "stretch", "misfit"}


def test_the_posting_text_is_read_even_when_the_parse_reply_lacks_it() -> None:
    parsed = {"title": "Data Analyst", "employment_type": "full time", "start_date": "June 2027"}
    assert judge(parsed, PROFILE, score=90, rationale="Fits.").reason is None
    j = judge(parsed, PROFILE, score=90, rationale="Fits.", description=SPONSOR_LINE)
    assert j.reason and j.fields["score"] == 0 and j.fields["excluded"] is True
    assert j.fields["rationale"] == f"excluded: {j.reason}"


def test_an_intern_title_counts_as_an_internship_and_rules_can_be_skipped() -> None:
    parsed = {"title": "Data Science Intern", "employment_type": "full time", "start_date": None}
    j = judge(parsed, PROFILE, score=70, rationale="ok", description=SPONSOR_LINE, rules=())
    assert j.reason is None and j.fields["employment_type"] == "intern"


def test_read_columns_stores_only_what_the_read_has() -> None:
    cols = read_columns({"requirements": ["SQL"], "seniority": "entry", "score_trace": {"kept": 0}})
    assert cols == {"requirements": '["SQL"]', "seniority": "entry", "score_trace": '{"kept": 0}'}
    assert read_columns({}) == {}


@pytest.fixture
def conn(tmp_path: Path) -> db.Connection:
    c = db.connect(tmp_path / "s.db")
    db.insert_posting(c, {"hash": "h", "company": "Acme", "title": "Analyst", "url": "https://x/1",
                          "score": 50, "excluded": False, "source": "ats"})  # fmt: skip
    c.commit()
    return c


def _events(conn: db.Connection) -> list[tuple[str, str]]:
    return [
        (r[0], r[1]) for r in conn.execute("SELECT stage, verdict FROM posting_events ORDER BY id")
    ]


def test_save_score_updates_the_row_and_leaves_one_score_event(conn: db.Connection) -> None:
    save_score(
        conn,
        1,
        {"score": 81, "rationale": "Fits.", "timing_unclear": True},
        how="scored again",
        was=50,
    )
    row = conn.execute(
        "SELECT score, rationale, timing_unclear FROM postings WHERE id=1"
    ).fetchone()
    assert tuple(row) == (81, "Fits.", 1)
    assert _events(conn) == [("score", "81")]
    detail = json.loads(conn.execute("SELECT detail FROM posting_events").fetchone()[0])
    assert detail == {"how": "scored again", "was": 50}


def test_an_excluded_save_leaves_a_rules_event(conn: db.Connection) -> None:
    fields = {
        "score": 0,
        "excluded": True,
        "exclusion_reason": "contract role",
        "rationale": "excluded: contract role",
    }
    save_score(conn, 1, fields, how="excluded")
    assert conn.execute("SELECT excluded, score FROM postings WHERE id=1").fetchone()[:] == (1, 0)
    assert _events(conn) == [("rules", "excluded: contract role")]


def test_save_score_refuses_a_column_that_is_not_a_score(conn: db.Connection) -> None:
    with pytest.raises(ValueError, match="not a score column"):
        save_score(conn, 1, {"status": "applied"}, how="x")
