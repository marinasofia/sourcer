"""The package layout: adapters, reserved stubs, the companies table, and the shims."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sourcer import db, main, snowdb, sources, sqlitedb
from sourcer.apply import prefill, tailor
from sourcer.bot import telegram
from sourcer.sources import careers_page
from tests.test_snowdb import make_conn


class FakeResp:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> Any:
        return self.payload


COMPANY = {"company": "TestCo", "ats": "greenhouse", "slug": "testco"}


def test_greenhouse_adapter_returns_every_posting_unfiltered() -> None:
    payload = {
        "jobs": [
            {"title": "Staff Engineer", "location": {"name": "Tokyo"}, "absolute_url": "u1"},
            {"title": "AI Engineer Intern", "location": {"name": "New York"}, "absolute_url": "u2"},
        ]
    }
    with patch("sourcer.net.get", return_value=FakeResp(payload)):
        out = sources.greenhouse.fetch_postings(COMPANY)
    assert [p["title"] for p in out] == ["Staff Engineer", "AI Engineer Intern"]
    assert "company" not in out[0]  # fetch_company adds it after the location gate


def test_lever_and_ashby_adapters_delegate_to_their_fetchers() -> None:
    lever_payload = [{"text": "Data Intern", "hostedUrl": "u", "createdAt": 1_700_000_000_000}]
    with patch("sourcer.net.get", return_value=FakeResp(lever_payload)):
        assert (
            sources.lever.fetch_postings({**COMPANY, "ats": "lever"})[0]["title"] == "Data Intern"
        )
    ashby_payload = {"jobs": [{"title": " ML Intern ", "location": "NYC", "jobUrl": "u"}]}
    with patch("sourcer.net.get", return_value=FakeResp(ashby_payload)):
        assert sources.ashby.fetch_postings({**COMPANY, "ats": "ashby"})[0]["title"] == "ML Intern"


def test_package_level_fetch_postings_dispatches_on_ats_type() -> None:
    payload = {"jobs": [{"title": "AI Engineer", "location": {"name": "NYC"}, "absolute_url": "u"}]}
    with patch("sourcer.net.get", return_value=FakeResp(payload)):
        assert sources.fetch_postings(COMPANY)[0]["title"] == "AI Engineer"


def test_all_collection_adapters_are_wired() -> None:
    assert careers_page.fetch_postings in sources.FETCHERS.values()
    assert set(sources.FETCHERS) == {
        "greenhouse",
        "lever",
        "ashby",
        "workable",
        "smartrecruiters",
        "rippling",
        "careers_page",
        "workday",
        "eightfold",
        "oracle",
    }


def test_fetch_company_uses_the_public_adapter_contract() -> None:
    seen: list[dict[str, Any]] = []

    def fetch(company: dict[str, Any]) -> list[dict[str, Any]]:
        seen.append(company)
        return []

    entry = {"company": "TestCo", "ats": "custom", "slug": "testco"}
    with patch.dict(sources.FETCHERS, {"custom": fetch}):
        assert sources.fetch_company(entry) == ([], [], 0)
    assert seen == [entry]


def test_apply_stubs_raise_and_bot_dry_run_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(NotImplementedError):
        tailor.tailor({"title": "x"})
    with pytest.raises(NotImplementedError):
        prefill.prefill({"title": "x"}, None)
    monkeypatch.setattr(telegram.config, "DRY_RUN", True)
    monkeypatch.setattr(telegram.config, "TELEGRAM_ALLOWED_CHAT_IDS", frozenset({1}))
    monkeypatch.setattr(telegram.config, "TELEGRAM_ALLOWED_USER_IDS", frozenset({1}))
    telegram.run()


# ---------------------------------------------------------------- companies


def test_companies_table_seeded_from_list_and_idempotent(tmp_db: sqlite3.Connection) -> None:
    companies = [
        {"company": "TestCo", "ats": "greenhouse", "slug": "testco"},
        {"company": "LeverCo", "ats": "lever", "slug": "leverco", "domain": "leverco.com"},
    ]
    assert sqlitedb.upsert_companies(tmp_db, companies) == 2
    assert sqlitedb.upsert_companies(tmp_db, companies) == 0
    rows = sqlitedb.list_companies(tmp_db)
    assert [r["name"] for r in rows] == ["TestCo", "LeverCo"]
    first = rows[0]
    assert (first["ats_type"], first["ats_slug"], first["source"]) == (
        "greenhouse",
        "testco",
        "companies.json",
    )
    assert first["submit_enabled"] == 0
    assert first["quality_signals"] == "{}"
    assert first["added_at"]
    assert first["domain"] is None
    assert rows[1]["domain"] == "leverco.com"


def test_companies_refresh_keeps_operator_columns(tmp_db: sqlite3.Connection) -> None:
    sqlitedb.upsert_companies(tmp_db, [{"company": "TestCo", "ats": "greenhouse", "slug": "tc"}])
    tmp_db.execute(
        "UPDATE companies SET submit_enabled=1, quality_signals='{\"k\": 1}' WHERE name='TestCo'"
    )
    tmp_db.commit()
    sqlitedb.upsert_companies(tmp_db, [{"company": "TestCo", "ats": "ashby", "slug": "tc2"}])
    row = sqlitedb.list_companies(tmp_db)[0]
    assert (row["ats_type"], row["ats_slug"]) == ("ashby", "tc2")
    assert (row["submit_enabled"], row["quality_signals"]) == (1, '{"k": 1}')


def test_company_input_cannot_enable_submission(tmp_db: sqlite3.Connection) -> None:
    company = {
        "company": "TestCo",
        "ats": "greenhouse",
        "slug": "tc",
        "submit_enabled": True,
    }
    with pytest.raises(ValueError, match="private operator state"):
        sqlitedb.upsert_companies(tmp_db, [company])
    assert sqlitedb.list_companies(tmp_db) == []


def test_companies_refresh_revokes_removed_company(tmp_db: sqlite3.Connection) -> None:
    companies = [
        {"company": "KeepCo", "ats": "greenhouse", "slug": "keep"},
        {"company": "RemoveCo", "ats": "lever", "slug": "remove"},
    ]
    sqlitedb.upsert_companies(tmp_db, companies)
    tmp_db.execute("UPDATE companies SET submit_enabled=1")
    tmp_db.commit()
    sqlitedb.upsert_companies(tmp_db, companies[:1])
    flags = {row["name"]: row["submit_enabled"] for row in sqlitedb.list_companies(tmp_db)}
    assert flags == {"KeepCo": 1, "RemoveCo": 0}


def test_companies_table_is_added_to_an_existing_database(tmp_path: Path) -> None:
    old = tmp_path / "old.db"
    raw = sqlite3.connect(old)
    raw.executescript(
        "CREATE TABLE postings (id INTEGER PRIMARY KEY, hash TEXT UNIQUE NOT NULL, "
        "company TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL, "
        "date_scraped TEXT NOT NULL);"
        "CREATE TABLE run_logs (id INTEGER PRIMARY KEY, run_ts TEXT NOT NULL);"
    )
    raw.close()
    conn = sqlitedb.connect(old)
    assert conn.execute("SELECT count(*) FROM companies").fetchone()[0] == 0


def test_build_state_seeds_companies(paths: Path) -> None:
    state = main.build_state(client=MagicMock())
    names = [r["name"] for r in db.list_companies(state["conn"])]
    assert names == [c["company"] for c in state["companies"]]


def test_facade_exposes_companies_functions() -> None:
    assert db.upsert_companies is sqlitedb.upsert_companies
    assert db.list_companies is sqlitedb.list_companies
    assert db.save_company_probe is sqlitedb.save_company_probe


def test_snowflake_upsert_companies_merges_on_name() -> None:
    conn, store = make_conn(rows=[(0,)])
    snowdb.upsert_companies(conn, [{"company": "TestCo", "ats": "greenhouse", "slug": "tc"}])
    merges = [e for e in store["executed"] if e[0] == "MERGE"]
    assert len(merges) == 1
    assert "INTO companies" in merges[0][1]
    assert merges[0][2][0] == "TestCo"
    assert merges[0][2][-1] is False  # submit_enabled defaults to off
    revocations = [e for e in store["executed"] if e[0] == "UPDATE"]
    assert len(revocations) == 1
    assert "submit_enabled=FALSE" in revocations[0][1]
    assert revocations[0][2] == ["TestCo"]
    assert store["commits"] == 1
    assert snowdb.list_companies(conn) == [{"n": 0}]
    assert snowdb._count_companies(snowdb.Conn(MagicMock(cursor=_EmptyCursor))) == 0


def test_snowflake_company_input_cannot_enable_submission() -> None:
    conn, store = make_conn()
    company = {
        "company": "TestCo",
        "ats": "greenhouse",
        "slug": "tc",
        "submit_enabled": True,
    }
    with pytest.raises(ValueError, match="private operator state"):
        snowdb.upsert_companies(conn, [company])
    assert store["executed"] == []


class _EmptyCursor:
    description = None

    def execute(self, sql: str, params: Any = None) -> None:
        pass

    def fetchall(self) -> list[Any]:
        return []
