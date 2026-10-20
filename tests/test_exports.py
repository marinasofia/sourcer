"""Exporters and restore, end to end on a temporary database."""

from __future__ import annotations

import json

import pytest

from sourcer import db, export, export_public, export_showcase, restore


def seed(conn, posting_row, rows=None):
    rows = rows or [
        posting_row(hash="a", title="AI Engineer", rationale="Strong Python match \u2014 solid."),
        posting_row(hash="b", title="Data Scientist", url="u2", score=40, company="LeverCo"),
        posting_row(hash="c", title="Sr Thing", url="u3", score=0, excluded=True),
        posting_row(
            hash="d", title="ML Intern", url="u4", score=None, source="simplify", rationale=None
        ),
    ]
    for r in rows:
        db.insert_posting(conn, r)
    db.insert_run_log(
        conn,
        {"companies_covered": 2, "postings_seen": 10, "parse_failures": 1, "est_cost_usd": 0.01},
        run_ts="2026-09-01T09:00:00+00:00",
    )
    db.insert_run_log(conn, {"companies_covered": 2, "postings_seen": 12, "new_postings": 3})
    conn.commit()


def test_export_writes_postings_and_runs(tmp_db, tmp_path, posting_row):
    seed(tmp_db, posting_row)
    out = export.export(tmp_db, tmp_path / "out" / "data.json")
    data = json.loads(out.read_text())
    assert len(data["postings"]) == 4
    assert len(data["runs"]) == 2
    top = data["postings"][0]
    assert top["requirements"] == ["Python"]
    assert "\u2014" not in top["rationale"]
    assert data["runs"][0]["node_metrics"] == {}


def test_export_default_path_and_main(tmp_db, tmp_path, posting_row, monkeypatch, capsys):
    seed(tmp_db, posting_row)
    monkeypatch.setattr(export, "DOCS_DATA", tmp_path / "docs" / "data.json")
    monkeypatch.setattr(export.db, "connect", lambda: tmp_db)
    assert export.main() == 0
    assert (tmp_path / "docs" / "data.json").exists()
    assert "data.json" in capsys.readouterr().out


def test_clean_text_replaces_dashes():
    assert export.clean_text("a \u2014 b \u2013 c") == "a ,  b - c"


def test_export_public_stats(tmp_db, tmp_path, posting_row):
    seed(tmp_db, posting_row)
    out = export_public.export_public(tmp_db, tmp_path / "pub.json")
    stats = json.loads(out.read_text())
    assert stats["runs"] == 2
    assert stats["postings_scored_total"] == 3
    assert stats["postings_ranked"] == 2
    assert stats["listings_tracked"] == 1
    assert stats["parse_failures_total"] == 1
    assert stats["postings_on_boards"] == 12
    assert stats["last_run_new_postings"] == 3


def test_export_public_empty_db_and_main(tmp_db, tmp_path, monkeypatch, capsys):
    stats = export_public.build_public_stats(tmp_db)
    assert stats["runs"] == 0 and stats["updated"] is None
    assert stats["schema_failure_rate"] == 0.0
    monkeypatch.setattr(export_public.db, "connect", lambda: tmp_db)
    target = tmp_path / "p.json"
    assert export_public.main([str(target)]) == 0
    assert target.exists()
    monkeypatch.setattr(export_public, "OUT", tmp_path / "default.json")
    monkeypatch.setattr("sys.argv", ["export_public"])
    assert export_public.main() == 0
    assert (tmp_path / "default.json").exists()
    assert "json" in capsys.readouterr().out


def test_export_showcase_end_to_end(tmp_db, paths, posting_row):
    seed(tmp_db, posting_row)
    db.insert_posting(
        tmp_db,
        posting_row(
            hash="e",
            title="Year at TestCo - Forward Deployed Engineer",
            url="u5",
            score=91,
            rationale="Great fit for TestCo. Missing Kubernetes though.",
        ),
    )
    tmp_db.commit()
    out = export_showcase.export_showcase(tmp_db, paths / "showcase.json")
    payload = json.loads(out.read_text())
    postings = payload["postings"]
    assert {p["sector"] for p in postings} == {"Payments", "AI labs"}
    assert all("url" not in p and "company" not in p for p in postings)
    assert all(not export_showcase.GAP_MARKERS.search(p["blurb"]) for p in postings)
    fde = next(p for p in postings if "Forward Deployed" in p["role"])
    assert "TestCo" not in fde["role"] and "[employer]" in fde["role"]
    assert "testco" not in fde["blurb"].lower()
    assert payload["summary"]["roles_scored"] == 4
    assert payload["summary"]["listings_tracked"] == 1
    assert payload["summary"]["companies_covered"] == 2


def test_showcase_unlabelled_sector_and_empty_summary(tmp_db, paths, posting_row):
    db.insert_posting(tmp_db, posting_row(hash="z", company="Unknown Co"))
    tmp_db.commit()
    payload = json.loads(export_showcase.export_showcase(tmp_db, paths / "s.json").read_text())
    assert payload["postings"][0]["sector"] == "Unlabelled"
    assert payload["summary"]["updated"] is None


def test_leak_scan_raises_on_gap_and_employer():
    with pytest.raises(export_showcase.LeakError):
        export_showcase.leak_scan([{"sector": "x", "role": "r", "blurb": "weak on RL"}], set())
    with pytest.raises(export_showcase.LeakError):
        export_showcase.leak_scan(
            [{"sector": "x", "role": "Engineer at Acme", "blurb": "Strong fit."}], {"Acme"}
        )
    export_showcase.leak_scan([{"sector": "x", "role": "r", "blurb": "Strong fit."}], {"Acme"})


def test_redact_names_empty_text():
    assert export_showcase.redact_names("", {"Acme"}) == ""


def test_showcase_main_ok_and_leak(tmp_db, paths, posting_row, monkeypatch, capsys):
    seed(tmp_db, posting_row)
    monkeypatch.setattr(export_showcase.db, "connect", lambda: tmp_db)
    monkeypatch.setattr(export_showcase, "OUT", paths / "default-showcase.json")
    assert export_showcase.main([]) == 0
    assert (paths / "default-showcase.json").exists()
    monkeypatch.setattr("sys.argv", ["export_showcase", str(paths / "argv.json")])
    assert export_showcase.main() == 0
    assert (paths / "argv.json").exists()

    def leaky(*_a, **_k):
        raise export_showcase.LeakError("boom")

    monkeypatch.setattr(export_showcase, "export_showcase", leaky)
    assert export_showcase.main([]) == 1
    assert "refusing to publish" in capsys.readouterr().err


def test_restore_round_trip_and_run_dedupe(tmp_db, tmp_path, posting_row, paths):
    seed(tmp_db, posting_row)
    data = tmp_path / "data.json"
    export.export(tmp_db, data)
    fresh = db.connect(tmp_path / "fresh.db")
    assert restore.restore(fresh, data) == (4, 2)
    assert restore.restore(fresh, data) == (4, 0)  # postings ignored by hash, runs by ts
    runs = fresh.execute("SELECT run_ts FROM run_logs ORDER BY id").fetchall()
    assert len(runs) == 2 and runs[0]["run_ts"] == "2026-09-01T09:00:00+00:00"
    assert fresh.execute("SELECT COUNT(*) AS n FROM postings").fetchone()["n"] == 4


def test_score_trace_survives_export_and_restore_cycles(tmp_db, tmp_path, posting_row):
    trace = {"rule": "steady", "reads": [72, 74]}
    db.insert_posting(tmp_db, posting_row(score_trace=trace))
    tmp_db.commit()
    conn = tmp_db
    for cycle in range(3):
        data = tmp_path / f"data{cycle}.json"
        export.export(conn, data)
        conn = db.connect(tmp_path / f"cycle{cycle}.db")
        restore.restore(conn, data)
    stored = conn.execute("SELECT score_trace FROM postings").fetchone()["score_trace"]
    assert json.loads(stored) == trace
    # a row already encoded twice by an older export comes back as the trace itself
    twice = posting_row(title="Other", score_trace=json.dumps(json.dumps(trace)))
    db.insert_posting(conn, twice)
    row = conn.execute("SELECT score_trace FROM postings WHERE title='Other'").fetchone()
    assert json.loads(row["score_trace"]) == trace


def test_restore_missing_file(tmp_db, tmp_path):
    assert restore.restore(tmp_db, tmp_path / "nope.json") == (0, 0)


def test_restore_preserves_tracking_and_does_not_overwrite_newer_state(
    tmp_db, tmp_path, posting_row
):
    db.insert_posting(tmp_db, posting_row(hash="tracked"))
    row = tmp_db.execute("SELECT id FROM postings").fetchone()
    db.set_status(tmp_db, row["id"], "applied", "Follow up next week")
    tmp_db.execute(
        "UPDATE postings SET verified=?, verified_at=?, alerted=? WHERE hash=?",
        ("verified", "2026-09-04", 1, "tracked"),
    )
    tmp_db.commit()
    original = dict(tmp_db.execute("SELECT * FROM postings").fetchone())
    snapshot = export.export(tmp_db, tmp_path / "snapshot.json")
    fresh = db.connect(tmp_path / "restored.db")
    try:
        restore.restore(fresh, snapshot)
        restored = dict(fresh.execute("SELECT * FROM postings").fetchone())
        assert {k: v for k, v in restored.items() if k != "id"} == {
            k: v for k, v in original.items() if k != "id"
        }
        db.set_status(fresh, restored["id"], "interview", "Newer local note")
        restore.restore(fresh, snapshot)
        latest = fresh.execute("SELECT status, notes FROM postings").fetchone()
        assert tuple(latest) == ("interview", "Newer local note")
    finally:
        fresh.close()


def test_restore_main(tmp_db, tmp_path, posting_row, monkeypatch, capsys):
    seed(tmp_db, posting_row)
    data = tmp_path / "data.json"
    export.export(tmp_db, data)
    fresh = db.connect(tmp_path / "fresh.db")
    monkeypatch.setattr(restore, "DOCS_DATA", data)
    monkeypatch.setattr(restore.db, "connect", lambda: fresh)
    assert restore.main() == 0
    assert "restored 4 postings, 2 runs" in capsys.readouterr().out
