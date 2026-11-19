"""The timeline of a posting: every stage that decides something writes one
event; dropped postings are kept under their address, then pruned."""

from __future__ import annotations

import importlib
from collections import Counter
from typing import Any
from unittest.mock import patch

from sourcer import db, sources, sqlitedb, trace


def test_a_saved_posting_keeps_its_whole_story(tmp_path: Any) -> None:
    conn = db.connect(tmp_path / "s.db")
    p: dict[str, Any] = {"company": "Acme", "title": "Data Analyst", "url": "https://x/1"}
    trace.event(p, "found", "greenhouse/acme")
    trace.event(p, "gate", "kept")
    trace.event(p, "score", 82, rule="one read", formula="core: 55 + 40 x 0.67 coverage = 82")
    db.insert_posting(conn, {**p, "hash": "h", "location": "NYC", "score": 82, "excluded": False})
    pid = conn.execute("SELECT id FROM postings").fetchone()[0]
    trace.flush(conn, p, pid)
    db.set_status(conn, pid, "shortlist")  # a status change lands on the same timeline
    story = trace.timeline(conn, posting_id=pid)
    assert [e["stage"] for e in story] == ["found", "gate", "score", "status"]
    assert story[2]["detail"]["formula"].endswith("= 82") and story[3]["verdict"] == "shortlist"
    assert all(e["code"] and e["run"] for e in story)


def test_a_dropped_posting_says_which_gate_and_is_pruned_later(tmp_path: Any) -> None:
    conn = db.connect(tmp_path / "s.db")
    p = {"company": "Acme", "title": "Senior Staff Engineer", "url": "https://x/2"}
    trace.log_dropped(conn, [(p, "gate", {"verdict": "above_entry", "board": "greenhouse/acme"})])
    story = trace.timeline(conn, url="https://x/2")
    assert story[0]["stage"] == "gate" and story[0]["verdict"] == "above_entry"
    conn.execute("UPDATE posting_events SET at='2000-01-01T00:00:00'")
    assert trace.prune(conn) == 1 and trace.timeline(conn, url="https://x/2") == []


def test_fetch_company_hands_back_what_each_gate_dropped() -> None:
    raw = [
        {
            "title": "Data Analyst",
            "location": "New York, NY",
            "url": "u1",
            "posted_at": "2099-01-01",
        },
        {
            "title": "Data Analyst",
            "location": "Berlin, Germany",
            "url": "u2",
            "posted_at": "2099-01-01",
        },
    ]
    dropped: list[Any] = []
    with patch.object(sources, "fetch_postings", return_value=raw):
        _, kept, n = sources.fetch_company(
            {"company": "Acme"}, profile={}, reasons=Counter(), dropped=dropped
        )
    assert [p["url"] for p in kept] == ["u1"] and n == 1
    assert [(p["url"], v) for p, v in dropped] == [("u2", "location")]


def test_a_scheduler_run_id_from_the_environment_is_used(monkeypatch) -> None:
    monkeypatch.setenv("SOURCER_RUN_ID", "discover-20261118-0700-3f9a")
    try:
        assert importlib.reload(trace).RUN_ID == "discover-20261118-0700-3f9a"
    finally:
        monkeypatch.delenv("SOURCER_RUN_ID")
        importlib.reload(trace)


def test_the_run_log_keeps_its_run_id(tmp_db) -> None:
    sqlitedb.insert_run_log(tmp_db, {"run_id": "r-1", "new_postings": 3})
    assert tmp_db.execute("SELECT run_id FROM run_logs").fetchone()[0] == "r-1"
