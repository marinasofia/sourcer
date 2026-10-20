"""Telegram boundary tests. No test opens a network connection."""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import Mock, patch

import pytest
import requests

from sourcer import sqlitedb
from sourcer.bot.telegram import (
    Settings,
    TelegramClient,
    handle_update,
    send_morning_digest,
    send_weekly_report,
    weekly_report_text,
)
from sourcer.preferences import adjusted_score, title_adjustment


def settings(*, dry_run: bool = True) -> Settings:
    return Settings("secret", frozenset({10}), frozenset({20}), dry_run, 75)


def update(data: str, *, chat_id: int = 10, user_id: int = 20) -> dict[str, Any]:
    return {
        "callback_query": {
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}},
            "data": data,
        }
    }


def test_settings_fail_closed_without_both_allowlists() -> None:
    with pytest.raises(ValueError, match="allowlists"):
        Settings("token", frozenset({1}), frozenset()).validate()


def test_client_never_sends_in_dry_run() -> None:
    client = TelegramClient(settings())
    assert client.send(10, "preview") is False
    with pytest.raises(PermissionError):
        client.send(11, "blocked")


def test_unauthorized_callback_cannot_mutate_state(
    tmp_db: sqlite3.Connection, posting_row: Any
) -> None:
    sqlitedb.insert_posting(tmp_db, posting_row())
    client = TelegramClient(settings())
    assert handle_update(tmp_db, client, update("feedback:1:1", user_id=999)) is None
    assert sqlitedb.list_feedback(tmp_db) == []


def test_feedback_and_shortlist_callbacks_are_bounded(
    tmp_db: sqlite3.Connection, posting_row: Any
) -> None:
    sqlitedb.insert_posting(tmp_db, posting_row())
    client = TelegramClient(settings(dry_run=False), ApiTransport([{}, {}]))
    assert handle_update(tmp_db, client, update("feedback:1:1")) == "feedback"
    assert sqlitedb.list_feedback(tmp_db)[0]["value"] == 1
    assert handle_update(tmp_db, client, update("status:shortlist:1")) == "shortlist"
    row = tmp_db.execute("SELECT status FROM postings WHERE id=1").fetchone()
    assert row["status"] == "shortlist"


def test_digest_dry_run_previews_without_marking_sent(
    tmp_db: sqlite3.Connection, posting_row: Any
) -> None:
    sqlitedb.insert_posting(tmp_db, posting_row(score=80))
    preview = send_morning_digest(tmp_db, TelegramClient(settings()), 10)
    assert preview[0]["adjusted_score"] == 80
    row = tmp_db.execute("SELECT telegram_notified FROM postings").fetchone()
    assert row["telegram_notified"] == 0


def test_preference_learning_waits_then_caps() -> None:
    sparse = [{"title": "ML Researcher", "value": 1}]
    assert title_adjustment("ML Engineer", sparse) == 0
    feedback = [{"title": "ML Researcher", "value": 1} for _ in range(10)]
    assert title_adjustment("ML Scientist", feedback) == 5
    assert adjusted_score({"title": "ML Scientist", "score": 98}, feedback) == 100


def test_weekly_report_handles_zero_applications() -> None:
    text = weekly_report_text(
        {"companies_added": 2, "sources": [{"source": "ashby", "seen": 4, "applied": 0}]}
    )
    assert "Companies added: 2" in text
    assert "0% response rate" in text


class ApiResponse:
    def __init__(self, result: Any) -> None:
        self.result = result

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return {"ok": True, "result": self.result}


class ApiTransport:
    def __init__(self, results: list[Any]) -> None:
        self.results = iter(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> ApiResponse:
        self.calls.append((url, kwargs))
        return ApiResponse(next(self.results))


def test_live_client_sends_and_reads_updates_without_query_secrets() -> None:
    transport = ApiTransport([{}, [{"update_id": 2}]])
    client = TelegramClient(settings(dry_run=False), transport)
    assert client.send(10, "hello", {"inline_keyboard": []}) is True
    assert client.updates(2) == [{"update_id": 2}]
    assert "secret" in transport.calls[0][0]
    assert transport.calls[0][1]["json"]["chat_id"] == 10
    assert transport.calls[1][1]["json"]["offset"] == 2


def test_successful_digest_marks_only_sent_postings(
    tmp_db: sqlite3.Connection, posting_row: Any
) -> None:
    sqlitedb.insert_posting(tmp_db, posting_row(score=80))
    transport = ApiTransport([{}])
    client = TelegramClient(settings(dry_run=False), transport)
    preview = send_morning_digest(tmp_db, client, 10)
    assert len(preview) == 1
    assert sqlitedb.list_digest_postings(tmp_db, 75) == []


def test_weekly_report_sends_aggregate_metrics(tmp_db: sqlite3.Connection) -> None:
    transport = ApiTransport([{}])
    text = send_weekly_report(tmp_db, TelegramClient(settings(dry_run=False), transport), 10)
    assert "Weekly Sourcer report" in text
    assert len(transport.calls) == 1


def test_dry_run_url_input_cannot_probe_or_write(tmp_db: sqlite3.Connection) -> None:

    message = {"message": {"from": {"id": 20}, "chat": {"id": 10}, "text": "https://example.com"}}
    with patch("sourcer.bot.telegram._add_company_url") as intake:
        assert handle_update(tmp_db, TelegramClient(settings()), message) is None
        intake.assert_not_called()
    assert sqlitedb.list_companies(tmp_db) == []


def test_telegram_errors_never_expose_token() -> None:

    transport = Mock()
    transport.post.side_effect = requests.ConnectionError("https://api.telegram.org/botsecret")
    with pytest.raises(RuntimeError) as error:
        TelegramClient(settings(dry_run=False), transport).send(10, "hello")
    assert "secret" not in str(error.value)
    assert error.value.__suppress_context__


def test_partial_digest_failure_does_not_resend_successes(
    tmp_db: sqlite3.Connection, posting_row: Any
) -> None:

    sqlitedb.insert_posting(tmp_db, posting_row(company="A"))
    sqlitedb.insert_posting(tmp_db, posting_row(company="B"))
    transport = Mock()
    transport.post.side_effect = [ApiResponse({}), RuntimeError("offline")]
    with pytest.raises(RuntimeError):
        send_morning_digest(tmp_db, TelegramClient(settings(dry_run=False), transport), 10)
    rows = sqlitedb.list_digest_postings(tmp_db, 75)
    assert [row["company"] for row in rows] == ["B"]


def test_digest_drops_stale_rows_but_keeps_undated_ones(
    tmp_db: sqlite3.Connection, posting_row: Any
) -> None:
    sqlitedb.insert_posting(tmp_db, posting_row(score=80, title="Undated", posted_at=None))
    sqlitedb.insert_posting(
        tmp_db, posting_row(score=80, title="Old", posted_at="2000-01-01", url="https://x/old")
    )
    preview = send_morning_digest(tmp_db, TelegramClient(settings()), 10)
    assert [p["title"] for p in preview] == ["Undated"]
