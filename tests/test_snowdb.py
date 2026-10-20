"""Snowflake backend tests against a fake connector (no account needed)."""

import importlib
import os
import sys
from unittest.mock import patch

from sourcer import snowdb


class FakeCursor:
    def __init__(self, store):
        self.store = store
        self.description = None
        self._rows = []

    def execute(self, sql, params=None):
        self.store["executed"].append((sql.strip().split()[0].upper(), sql, params))
        low = sql.lower()
        if low.startswith("select"):
            self.description = [("HASH",)] if "hash" in low else [("N",)]
            self._rows = self.store.get("rows", [])
        else:
            self.description = None

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return FakeCursor(self.store)

    def commit(self):
        self.store["commits"] += 1


def make_conn(rows=None):
    store = {"executed": [], "commits": 0, "rows": rows or []}
    return snowdb.Conn(FakeConn(store)), store


def posting():
    return {
        "hash": "abc",
        "company": "TestCo",
        "title": "AI Engineer",
        "url": "https://x.com/1",
        "score": 80,
        "excluded": False,
        "timing_unclear": True,
        "requirements": ["Python"],
        "tech_stack": ["Python"],
    }


def test_insert_posting_uses_merge_on_hash():
    conn, store = make_conn()
    snowdb.insert_posting(conn, posting())
    verb, sql, params = store["executed"][-1]
    assert verb == "MERGE"
    assert "t.hash = s.hash" in sql
    expected = snowdb.posting_hash("TestCo", "AI Engineer", "")
    assert params[0] == expected and params[1] == expected
    assert len(params) == 28  # merge key + posting values, including tracker metadata


def test_insert_preserves_tracking_values():
    conn, store = make_conn()
    data = {
        **posting(),
        "status": "applied",
        "status_updated": "2026-09-04",
        "notes": "Follow up",
        "verified": "verified",
        "verified_at": "2026-09-03",
    }
    snowdb.insert_posting(conn, data)
    _, sql, params = store["executed"][-1]
    assert "status, status_updated, notes, verified, verified_at" in sql
    assert params[-5:] == ["applied", "2026-09-04", "Follow up", "verified", "2026-09-03"]


def test_seen_hashes_reads_rows():
    conn, _ = make_conn(rows=[("h1",), ("h2",)])
    assert snowdb.seen_hashes(conn) == {"h1", "h2"}


def test_run_log_insert_and_commit():
    conn, store = make_conn()
    snowdb.insert_run_log(conn, {"companies_covered": 2, "est_cost_usd": 0.01})
    verb, _sql, params = store["executed"][-1]
    assert verb == "INSERT"
    assert len(params) == 16
    assert store["commits"] == 1


def test_backend_switch_rebinds_db_interface():
    with patch.dict(os.environ, {"SOURCER_BACKEND": "snowflake"}):
        for m in ("sourcer.db",):
            sys.modules.pop(m, None)
        dbmod = importlib.import_module("sourcer.db")
        assert dbmod.connect is snowdb.connect
        assert dbmod.insert_posting is snowdb.insert_posting
    sys.modules.pop("sourcer.db", None)
    importlib.import_module("sourcer.db")  # restore sqlite binding


def test_check_reports_missing_credentials():
    with patch.dict(os.environ, {}, clear=False):
        for v in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD"):
            os.environ.pop(v, None)
        try:
            snowdb.check()
            raise AssertionError("expected SystemExit")
        except SystemExit as e:
            assert "SNOWFLAKE_ACCOUNT" in str(e)
            assert "signup.snowflake.com" in str(e)
