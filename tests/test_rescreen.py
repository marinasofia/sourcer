"""Rows scored under older rules leave the board once today's rules reject them."""

from __future__ import annotations

import sqlite3
from typing import Any

from sourcer import rescreen, score_listings, sqlitedb

PROFILE = {"grad_year": 2027, "in_progress": True, "max_years_experience": 2}


def add(conn: sqlite3.Connection, posting_row: Any, **over: Any) -> int:
    sqlitedb.insert_posting(conn, posting_row(**over))
    return int(conn.execute("SELECT max(id) FROM postings").fetchone()[0])


def status(conn: sqlite3.Connection, pid: int) -> str:
    return str(conn.execute("SELECT status FROM postings WHERE id=?", (pid,)).fetchone()[0])


def test_rescreen_archives_new_rows_that_fail_and_keeps_fits(
    tmp_db: sqlite3.Connection, posting_row: Any
) -> None:
    good = add(tmp_db, posting_row, title="Data Science Intern", location="New York, NY")
    level = add(tmp_db, posting_row, title="Machine Learning Engineer II", location="Remote US")
    years = add(
        tmp_db,
        posting_row,
        title="Forward Deployed Engineer",
        location="New York, NY",
        years_experience_min=4,
    )
    region = add(  # no 2027 start, so the NYC and remote rule holds
        tmp_db,
        posting_row,
        title="Forward Deployed Engineer (West Coast)",
        location="Remote US",
        employment_type="full time",
        start_date="",
    )
    picked = add(tmp_db, posting_row, title="Founding Engineer", location="New York, NY")
    tmp_db.execute("UPDATE postings SET status='shortlist' WHERE id=?", (picked,))

    result = rescreen.rescreen(tmp_db, PROFILE)

    assert status(tmp_db, good) == "new"
    for pid in (level, years, region):
        assert status(tmp_db, pid) == "archived"
    assert status(tmp_db, picked) == "shortlist"  # operator's choice, flagged only
    assert [r["id"] for r in result["flagged"]] == [picked]
    note = tmp_db.execute("SELECT notes FROM postings WHERE id=?", (years,)).fetchone()[0]
    assert note.startswith(rescreen.NOTE_PREFIX) and "4+ years" in note


def test_dry_run_changes_nothing_and_undo_restores(
    tmp_db: sqlite3.Connection, posting_row: Any
) -> None:
    pid = add(tmp_db, posting_row, title="Data Analyst III", location="New York, NY")
    assert len(rescreen.rescreen(tmp_db, PROFILE, dry_run=True)["archived"]) == 1
    assert status(tmp_db, pid) == "new"
    rescreen.rescreen(tmp_db, PROFILE)
    assert status(tmp_db, pid) == "archived"
    assert rescreen.undo(tmp_db) == 1
    assert status(tmp_db, pid) == "new"


def test_listing_selection_skips_senior_and_offsite_titles(
    tmp_db: sqlite3.Connection, posting_row: Any
) -> None:
    def listing(title: str, location: str = "New York, NY") -> None:
        add(
            tmp_db,
            posting_row,
            title=title,
            location=location,
            score=None,
            source="simplify",
            url=f"https://boards.greenhouse.io/x/jobs/{abs(hash(title))}",
        )

    listing("Data Science Intern")
    listing("Senior Data Scientist")
    listing("Marketing Intern")
    listing("ML Intern (West Coast)", "Remote US")
    listing("AI Intern", "London, UK")
    titles = [r["title"] for r in score_listings.select_rows(tmp_db, 10)]
    # the fixtures start in summer 2027, so a West Coast internship is in scope; London is not.
    # Which titles she wants is the title check's call, so Marketing stays in.
    assert sorted(titles) == ["Data Science Intern", "ML Intern (West Coast)", "Marketing Intern"]


def test_reason_for_location_and_level(posting_row: Any) -> None:
    row = {
        "title": "Data Analyst",
        "location": "Paris, France",
        "seniority": "entry",
        "years_experience_min": 0,
        "employment_type": "full time",
        "start_date": None,
        "work_mode": None,
    }
    assert rescreen.reason_for(row, None).startswith("location out of scope")
    row.update(location="New York, NY", seniority="mid")
    assert rescreen.reason_for(row, PROFILE)


def test_rescreen_refuses_to_empty_the_board_without_force(
    tmp_db: sqlite3.Connection, posting_row: Any
) -> None:
    keep = [
        add(
            tmp_db,
            posting_row,
            title=f"Data Science Intern {i}",
            location="New York, NY",
            url=f"https://example.com/keep/{i}",
        )
        for i in range(14)
    ]
    drop = [
        add(
            tmp_db,
            posting_row,
            title=f"Machine Learning Engineer II, Team {i}",
            location="Remote US",
            url=f"https://example.com/drop/{i}",
        )
        for i in range(rescreen.GUARDRAIL_MIN_ROWS - 14)
    ]

    held = rescreen.rescreen(tmp_db, PROFILE)
    assert held["refused"] and len(held["archived"]) == len(drop)
    assert all(status(tmp_db, pid) == "new" for pid in drop)

    forced = rescreen.rescreen(tmp_db, PROFILE, force=True)
    assert not forced["refused"]
    assert all(status(tmp_db, pid) == "archived" for pid in drop)
    assert all(status(tmp_db, pid) == "new" for pid in keep)


def test_rescreen_reason_quotes_the_posting(tmp_db: sqlite3.Connection, posting_row: Any) -> None:
    pid = add(
        tmp_db,
        posting_row,
        title="Data Analyst",
        location="New York, NY",
        description="Visa sponsorship is not offered for this role.",
    )
    result = rescreen.rescreen(tmp_db, {**PROFILE, "needs_sponsorship": True})
    (item,) = [i for i in result["archived"] if i["id"] == pid]
    assert item["reason"] == 'no_sponsorship: "Visa sponsorship is not offered for this role."'


def test_listing_selection_uses_the_one_freshness_window(
    tmp_db: sqlite3.Connection, posting_row: Any
) -> None:
    for title, posted in (("Fresh Intern", None), ("Stale Intern", "2000-01-01")):
        add(
            tmp_db,
            posting_row,
            title=title,
            score=None,
            source="simplify",
            posted_at=posted,
            url=f"https://boards.greenhouse.io/x/jobs/{abs(hash(title))}",
        )
    assert [r["title"] for r in score_listings.select_rows(tmp_db, 10)] == ["Fresh Intern"]


def test_a_board_role_that_came_without_its_text_is_scored_however_old(
    tmp_db: sqlite3.Connection, posting_row: Any
) -> None:
    add(
        tmp_db,
        posting_row,
        title="Summer Analyst",
        score=None,
        source="ats",
        posted_at="2000-01-01",
        url="https://hdpc.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/X/job/1",
    )
    add(
        tmp_db,
        posting_row,
        title="Scored Analyst",
        score=80,
        source="ats",
        posted_at=None,
        url="https://hdpc.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/X/job/2",
    )
    # still listed on the company's own board: age only lowers its rank
    assert [r["title"] for r in score_listings.select_rows(tmp_db, 10)] == ["Summer Analyst"]
