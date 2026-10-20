"""Storage behavior for feedback and Telegram notification state."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from sourcer import sqlitedb


def test_feedback_requires_existing_posting_and_valid_value(
    tmp_db: sqlite3.Connection, posting_row: Any
) -> None:
    assert sqlitedb.record_feedback(tmp_db, 99, 1) == 0
    with pytest.raises(ValueError):
        sqlitedb.record_feedback(tmp_db, 99, 0)
    sqlitedb.insert_posting(tmp_db, posting_row())
    assert sqlitedb.record_feedback(tmp_db, 1, -1) == 1
    assert sqlitedb.record_feedback(tmp_db, 1, 1) == 1
    assert sqlitedb.list_feedback(tmp_db)[0]["value"] == 1


def test_mark_notified_is_idempotent(tmp_db: sqlite3.Connection, posting_row: Any) -> None:
    sqlitedb.insert_posting(tmp_db, posting_row())
    assert len(sqlitedb.list_digest_postings(tmp_db, 75)) == 1
    sqlitedb.mark_telegram_notified(tmp_db, [1])
    sqlitedb.mark_telegram_notified(tmp_db, [1])
    assert sqlitedb.list_digest_postings(tmp_db, 75) == []
