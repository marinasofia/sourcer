"""A scheduled restart preserves company discovery and user preferences."""

from pathlib import Path
from typing import Any

from sourcer import export, restore, sqlitedb


def test_snapshot_round_trip_remaps_feedback_and_keeps_notification_state(
    tmp_path: Path, posting_row: Any
) -> None:
    original = sqlitedb.connect(tmp_path / "original.db")
    sqlitedb.insert_posting(original, posting_row())
    sqlitedb.record_feedback(original, 1, 1)
    sqlitedb.mark_telegram_notified(original, [1])
    sqlitedb.save_discovered_companies(
        original,
        [
            {
                "name": "TestCo",
                "source": "builtin",
                "quality_status": "passed",
                "quality_signals": {"employee_count": 50},
            }
        ],
    )
    sqlitedb.save_company_probe(
        original,
        {
            "name": "TestCo",
            "ats_type": "ashby",
            "ats_slug": "testco",
            "urls_tried": [],
        },
    )
    saved_company = sqlitedb.list_companies(original)[0]
    path = export.export(original, tmp_path / "state.json")
    restored = sqlitedb.connect(tmp_path / "restored.db")
    # Force a different posting ID on restore.
    sqlitedb.insert_posting(restored, posting_row(company="Other"))
    restore.restore(restored, path)
    assert sqlitedb.list_feedback(restored)[0]["posting_id"] == 2
    assert sqlitedb.list_feedback(restored)[0]["value"] == 1
    assert restored.execute("SELECT telegram_notified FROM postings WHERE id=2").fetchone()[0]
    company = sqlitedb.list_companies(restored)[0]
    assert company["ats_type"] == "ashby"
    assert company["quality_status"] == "passed"
    assert company["added_at"] == saved_company["added_at"]
    assert company["submit_enabled"] == 0
    restore.restore(restored, path)
    assert len(sqlitedb.list_feedback(restored)) == 1
