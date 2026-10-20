"""Storage behavior for submitted applications and their event log."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from sourcer import db, snowdb, sqlitedb
from tests.test_snowdb import make_conn


def application(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "company": "TestCo",
        "title": "Data Science Intern",
        "url": "https://boards.greenhouse.io/testco/jobs/1",
        "ats": "greenhouse",
        "applied_at": "2026-09-29T10:15:00-04:00",
        "posted_at": "2026-09-27",
        "posting_age_days": 2,
        "resume_path": "data/sent/1_resume.pdf",
        "resume_sha256": "a" * 64,
        "jd_snapshot_path": "data/jd/1.html",
        "keyword_hits": ["Python", "SQL"],
        "keyword_misses": ["Databricks"],
    }
    return {**base, **over}


def test_insert_application_logs_captured_event(tmp_db: sqlite3.Connection) -> None:
    app_id = sqlitedb.insert_application(tmp_db, application())
    [row] = sqlitedb.list_applications(tmp_db)
    assert row["id"] == app_id
    assert row["status"] == "applied"
    assert row["status_changed_at"] == row["applied_at"]
    assert row["keyword_misses"] == '["Databricks"]'
    [event] = sqlitedb.list_application_events(tmp_db, app_id)
    assert (event["kind"], event["at"]) == ("captured", row["applied_at"])


def test_insert_application_requires_snapshot_fields(tmp_db: sqlite3.Connection) -> None:
    for field in ("resume_sha256", "jd_snapshot_path", "applied_at"):
        with pytest.raises(ValueError, match=field):
            sqlitedb.insert_application(tmp_db, application(**{field: None}))
    assert sqlitedb.list_applications(tmp_db) == []


def test_status_changes_are_validated_and_logged(tmp_db: sqlite3.Connection) -> None:
    app_id = sqlitedb.insert_application(tmp_db, application())
    with pytest.raises(ValueError):
        sqlitedb.set_application_status(tmp_db, app_id, "archived")
    assert sqlitedb.set_application_status(tmp_db, 999, "interview") == 0
    assert sqlitedb.set_application_status(tmp_db, app_id, "interview", "recruiter screen") == 1
    assert sqlitedb.list_applications(tmp_db)[0]["status"] == "interview"
    kinds = [(e["kind"], e["detail"]) for e in sqlitedb.list_application_events(tmp_db, app_id)]
    assert kinds == [("captured", None), ("status_set", "interview: recruiter screen")]


def test_event_kinds_are_whitelisted(tmp_db: sqlite3.Connection) -> None:
    app_id = sqlitedb.insert_application(tmp_db, application())
    with pytest.raises(ValueError):
        sqlitedb.add_application_event(tmp_db, app_id, "anything")
    sqlitedb.add_application_event(tmp_db, app_id, "posting_closed")
    assert sqlitedb.list_application_events(tmp_db, app_id)[-1]["kind"] == "posting_closed"


def test_existing_database_gains_application_tables(tmp_path: Any) -> None:
    old = tmp_path / "old.db"
    raw = sqlite3.connect(old)
    raw.execute(
        "CREATE TABLE postings (id INTEGER PRIMARY KEY, hash TEXT, company TEXT, "
        "title TEXT, url TEXT, date_scraped TEXT)"
    )
    raw.commit()
    raw.close()
    conn = sqlitedb.connect(old)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"applications", "application_events"} <= tables


def test_facade_exposes_application_functions() -> None:
    for name in (
        "insert_application",
        "add_application_event",
        "set_application_status",
        "list_applications",
        "list_application_events",
    ):
        assert getattr(db, name) is getattr(sqlitedb, name)
    assert snowdb.APPLICATION_STATUSES == sqlitedb.APPLICATION_STATUSES
    assert snowdb.APPLICATION_FIELDS == sqlitedb.APPLICATION_FIELDS


def test_snowflake_application_insert_and_status() -> None:
    conn, store = make_conn(rows=[(7,)])
    assert snowdb.insert_application(conn, application()) == 7
    verbs = [verb for verb, _, _ in store["executed"]]
    assert verbs == ["INSERT", "SELECT", "INSERT"]
    assert store["executed"][2][2][2] == "captured"
    with pytest.raises(ValueError):
        snowdb.insert_application(conn, application(resume_sha256=""))
    with pytest.raises(ValueError):
        snowdb.set_application_status(conn, 7, "archived")
    assert snowdb.set_application_status(conn, 7, "rejected") == 1
    assert store["executed"][-1][2][2] == "status_set"
