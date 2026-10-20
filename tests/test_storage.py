"""sqlite backend, storage facade, and Snowflake backend additions. No account needed."""

from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from sourcer import db, snowdb, sqlitedb

# ----------------------------------------------------------------- sqlite


def test_connect_migrates_old_schema(tmp_path: Path) -> None:
    old = tmp_path / "old.db"
    raw = sqlite3.connect(old)
    raw.executescript(sqlitedb.SCHEMA)
    raw.close()
    conn = sqlitedb.connect(old)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(postings)")}
    assert {c for c, _ in sqlitedb.MIGRATIONS} <= cols
    # a second connect is a no-op
    sqlitedb.connect(old)


def test_connect_creates_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "h.db"
    sqlitedb.connect(target)
    assert target.exists()


def test_set_status_updates_and_rejects_unknown(
    tmp_db: sqlite3.Connection, posting_row: Any
) -> None:
    sqlitedb.insert_posting(tmp_db, posting_row())
    pid = tmp_db.execute("SELECT id FROM postings").fetchone()["id"]
    assert sqlitedb.set_status(tmp_db, pid, "applied", "sent") == 1
    row = tmp_db.execute("SELECT status, notes, status_updated FROM postings").fetchone()
    assert row["status"] == "applied" and row["notes"] == "sent" and row["status_updated"]
    # a None note keeps the previous note
    sqlitedb.set_status(tmp_db, pid, "interview")
    assert tmp_db.execute("SELECT notes FROM postings").fetchone()["notes"] == "sent"
    assert sqlitedb.set_status(tmp_db, 9999, "applied") == 0
    with pytest.raises(ValueError, match="status must be one of"):
        sqlitedb.set_status(tmp_db, pid, "bogus")


def test_find_scored_twin_ignores_case_and_whitespace(
    tmp_db: sqlite3.Connection, posting_row: Any
) -> None:
    sqlitedb.insert_posting(tmp_db, posting_row(company="  TestCo ", title="AI Engineer  "))
    twin = sqlitedb.find_scored_twin(tmp_db, "testco", "ai engineer")
    assert twin is not None and twin["score"] == 80
    assert sqlitedb.find_scored_twin(tmp_db, "Other", "ai engineer") is None
    sqlitedb.insert_posting(tmp_db, posting_row(title="Unscored", score=None))
    assert sqlitedb.find_scored_twin(tmp_db, "TestCo", "Unscored") is None


def test_insert_posting_deduplicates_normalized_identity(
    tmp_db: sqlite3.Connection, posting_row: Any
) -> None:
    sqlitedb.insert_posting(tmp_db, posting_row(hash="first"))
    sqlitedb.insert_posting(tmp_db, posting_row(hash="second", url="https://mirror.example/job"))
    rows = tmp_db.execute("SELECT title, date_scraped FROM postings").fetchall()
    assert len(rows) == 1 and rows[0]["title"] == "AI Engineer"
    assert rows[0]["date_scraped"]


def test_insert_posting_keeps_supplied_date_scraped(
    tmp_db: sqlite3.Connection, posting_row: Any
) -> None:
    sqlitedb.insert_posting(tmp_db, posting_row(date_scraped="2026-01-01T00:00:00+00:00"))
    assert (
        tmp_db.execute("SELECT date_scraped FROM postings").fetchone()[0].startswith("2026-01-01")
    )


def test_insert_run_log_with_run_ts_and_string_json(tmp_db: sqlite3.Connection) -> None:
    sqlitedb.insert_run_log(
        tmp_db,
        {"companies_covered": 3, "node_metrics": '{"source": {}}', "exclusions": "[]"},
        run_ts="2026-08-01T09:00:00+00:00",
    )
    sqlitedb.insert_run_log(tmp_db, {"node_metrics": {"a": 1}})
    rows = tmp_db.execute("SELECT run_ts, node_metrics FROM run_logs ORDER BY id").fetchall()
    assert rows[0]["run_ts"] == "2026-08-01T09:00:00+00:00"
    assert rows[0]["node_metrics"] == '{"source": {}}'
    assert rows[1]["node_metrics"] == '{"a": 1}'
    assert rows[1]["run_ts"] != rows[0]["run_ts"]


def test_seen_hashes_and_posting_hash(tmp_db: sqlite3.Connection, posting_row: Any) -> None:
    row = posting_row()
    h = sqlitedb.posting_hash(row["company"], row["title"], row["location"])
    sqlitedb.insert_posting(tmp_db, row)
    assert sqlitedb.seen_hashes(tmp_db) == {h}
    assert len(h) == 64


# ----------------------------------------------------------------- facade


def test_select_backend() -> None:
    assert db._select("sqlite") is sqlitedb
    assert db._select("snowflake") is snowdb
    with pytest.raises(ValueError, match="SOURCER_BACKEND must be one of"):
        db._select("postgres")


def test_facade_exposes_sqlite_by_default() -> None:
    assert db.BACKEND == "sqlite"
    assert db.connect is sqlitedb.connect
    assert db.find_scored_twin is sqlitedb.find_scored_twin
    assert db.STATUSES == sqlitedb.STATUSES == snowdb.STATUSES
    assert db.posting_hash("a", "b", "c") == snowdb.posting_hash("a", "b", "c")


# -------------------------------------------------------------- snowflake


class FakeCursor:
    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store
        self.description: list[tuple[str]] | None = None
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.store["executed"].append((sql, params))
        low = sql.strip().lower()
        if low.startswith("select"):
            self.description = self.store.get("description", [("N",)])
            self._rows = self.store.get("rows", [])
        else:
            self.description = None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class FakeRaw:
    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.store)

    def commit(self) -> None:
        self.store["commits"] += 1

    def close(self) -> None:
        self.closed = True


def make_conn(
    rows: list[tuple[Any, ...]] | None = None, description: Any = None
) -> tuple[snowdb.Conn, dict[str, Any]]:
    store: dict[str, Any] = {"executed": [], "commits": 0, "rows": rows or []}
    if description:
        store["description"] = description
    return snowdb.Conn(FakeRaw(store)), store


@pytest.fixture
def fake_connector(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """A stand-in for snowflake.connector inserted into sys.modules."""
    store: dict[str, Any] = {"executed": [], "commits": 0, "rows": []}
    connector = types.SimpleNamespace(
        paramstyle="pyformat", connect=MagicMock(return_value=FakeRaw(store))
    )
    pkg = types.ModuleType("snowflake")
    pkg.connector = connector  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "snowflake", pkg)
    monkeypatch.setitem(sys.modules, "snowflake.connector", connector)  # type: ignore[arg-type]
    for k, v in {
        "SNOWFLAKE_ACCOUNT": "acct",
        "SNOWFLAKE_USER": "u",
        "SNOWFLAKE_PASSWORD": "p",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SNOWFLAKE_WAREHOUSE", raising=False)
    connector.store = store  # type: ignore[attr-defined]
    return connector


def test_connect_sets_qmark_and_runs_ddl(fake_connector: types.SimpleNamespace) -> None:
    conn = snowdb.connect(Path("ignored"))
    assert fake_connector.paramstyle == "qmark"
    kwargs = fake_connector.connect.call_args.kwargs
    assert kwargs["account"] == "acct" and kwargs["warehouse"] == "COMPUTE_WH"
    assert kwargs["database"] == "SOURCER" and kwargs["schema"] == "PUBLIC"
    executed = [sql for sql, _ in fake_connector.store["executed"]]
    assert executed == snowdb.DDL
    conn.close()
    assert conn.raw.closed


def test_rows_accessors() -> None:
    assert snowdb.Rows().fetchone() is None
    rows = snowdb.Rows([{"a": 1}])
    assert rows.fetchone() == {"a": 1}
    assert rows.fetchall() == [{"a": 1}]


def test_conn_execute_lowercases_columns_and_executemany() -> None:
    conn, store = make_conn(rows=[(1, "x")], description=[("ID",), ("TITLE",)])
    out = conn.execute("SELECT id, title FROM postings")
    assert out.fetchone() == {"id": 1, "title": "x"}
    result = conn.executemany("UPDATE postings SET alerted=1 WHERE id=?", [(1,), (2,)])
    assert result == []
    updates = [p for sql, p in store["executed"] if sql.startswith("UPDATE")]
    assert updates == [[1], [2]]
    conn.commit()
    assert store["commits"] == 1


def test_find_scored_twin_and_set_status() -> None:
    conn, store = make_conn(rows=[(90, "great")], description=[("SCORE",), ("RATIONALE",)])
    twin = snowdb.find_scored_twin(conn, "TestCo", "AI Engineer")
    assert twin == {"score": 90, "rationale": "great"}
    sql, params = store["executed"][-1]
    assert "lower(trim(company))" in sql and params == ["TestCo", "AI Engineer"]
    assert snowdb.set_status(conn, 7, "applied", "note") == 1
    sql, params = store["executed"][-1]
    assert sql.startswith("UPDATE postings SET status=?")
    assert params[0] == "applied" and params[2] == "note" and params[3] == 7
    assert store["commits"] == 1
    with pytest.raises(ValueError, match="status must be one of"):
        snowdb.set_status(conn, 7, "nope")


def test_insert_posting_defaults_and_date_scraped() -> None:
    conn, store = make_conn()
    snowdb.insert_posting(
        conn,
        {
            "hash": "h",
            "company": "C",
            "title": "T",
            "url": "u",
            "date_scraped": "2026-01-01T00:00:00",
        },
    )
    _sql, params = store["executed"][-1]
    assert params[8] == "[]" and params[11] == "[]"  # requirements, tech_stack
    assert params[14] is False and params[15] is False  # timing_unclear, excluded
    assert params[17] == "2026-01-01T00:00:00"
    assert params[18] == "ats" and params[20] is False


def test_check_reports_version_and_counts(
    fake_connector: types.SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Raw(FakeRaw):
        def cursor(self) -> FakeCursor:
            cur = FakeCursor(self.store)
            orig = cur.execute

            def execute(sql: str, params: Any = None) -> None:
                orig(sql, params)
                low = sql.lower()
                if "current_version" in low:
                    cur.description, cur._rows = [("V",)], [("9.1",)]
                elif "from postings" in low:
                    cur.description, cur._rows = [("N",)], [(12,)]
                elif "from run_logs" in low:
                    cur.description, cur._rows = [("N",)], [(3,)]

            cur.execute = execute  # type: ignore[method-assign]
            return cur

    fake_connector.connect.return_value = Raw(fake_connector.store)
    assert snowdb.check() == "connected: Snowflake 9.1; postings=12 run_logs=3"


def test_check_with_empty_results_uses_defaults(fake_connector: types.SimpleNamespace) -> None:
    fake_connector.store["description"] = [("V",)]
    assert snowdb.check() == "connected: Snowflake unknown; postings=0 run_logs=0"


def test_main_check_and_usage(
    fake_connector: types.SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_connector.store["description"] = [("V",)]
    assert snowdb.main(["check"]) == 0
    assert "connected: Snowflake" in capsys.readouterr().out
    assert snowdb.main([]) == 2
    assert "SOURCER_BACKEND=snowflake" in capsys.readouterr().err


def test_description_is_stored_and_filled_in_once(tmp_db, posting_row):
    row = posting_row(description="")
    sqlitedb.insert_posting(tmp_db, row)
    assert tmp_db.execute("SELECT description FROM postings").fetchone()[0] is None
    sqlitedb.insert_posting(tmp_db, {**row, "description": "Build pipelines in SQL."})
    sqlitedb.insert_posting(tmp_db, {**row, "description": "a later rewrite"})
    assert tmp_db.execute("SELECT description FROM postings").fetchone()[0] == (
        "Build pipelines in SQL."
    )


def test_a_connection_waits_for_another_writer_and_reads_alongside_one(tmp_path: Path) -> None:
    conn = sqlitedb.connect(tmp_path / "s.db")
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == sqlitedb.BUSY_SECONDS * 1000
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
