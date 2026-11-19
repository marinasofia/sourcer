"""MCP tool functions called directly, model calls faked."""

from __future__ import annotations

import json

from sourcer import db, mcp_server
from sourcer.pipeline import scoring
from tests.conftest import make_client

TEXT = "Python intern posting. Summer 2027 internship in New York, fully remote."
EXTRACT = {
    "employment_type": "intern",
    "employment_quote": "Summer 2027 internship",
    "start_date": "Summer 2027",
    "start_quote": "Summer 2027 internship",
    "work_mode": "remote",
    "work_mode_quote": "fully remote",
    "years_experience_min": None,
    "years_quote": "",
    "seniority": "intern",
    "seniority_quote": "Python intern posting",
    "requirements": ["Python"],
    "tech_stack": ["Python"],
}
PARSE_REPLY = json.dumps(EXTRACT)
SCORE_REPLY = json.dumps({"score": 77, "rationale": "Good Python overlap."})


def test_score_posting_happy_path(paths, monkeypatch):
    monkeypatch.setattr(scoring.llm, "get_client", lambda: make_client([PARSE_REPLY, SCORE_REPLY]))
    out = mcp_server.score_posting(TEXT, title="AI Intern", company="TestCo")
    assert out["score"] == 77 and out["excluded"] is False
    assert out["parsed"]["title"] == "AI Intern" and out["parsed"]["company"] == "TestCo"
    assert out["parsed"]["work_mode"] == "remote" and out["parsed"]["checked"]["work_mode"]
    assert out["est_cost_usd"] > 0


def test_score_posting_defaults_and_exclusion(paths, monkeypatch):
    text = "Data engineer. Requires 7 years of experience with Python."
    senior = {**EXTRACT, "years_experience_min": 7, "years_quote": "Requires 7 years of experience"}
    monkeypatch.setattr(scoring.llm, "get_client", lambda: make_client([json.dumps(senior)]))
    out = mcp_server.score_posting(text)
    assert out["excluded"] is True and out["score"] == 0
    assert "7+ years" in out["exclusion_reason"]
    # no title given: the board's placeholder, never a model's guess
    assert out["parsed"]["title"] == "untitled" and out["parsed"]["company"] == "unknown"


def test_score_posting_failures(paths, monkeypatch):
    monkeypatch.setattr(scoring.llm, "get_client", lambda: make_client(["x", "y"]))
    assert mcp_server.score_posting(TEXT)["error"].startswith("parse failed")
    monkeypatch.setattr(scoring.llm, "get_client", lambda: make_client([PARSE_REPLY, "x", "y"]))
    assert mcp_server.score_posting(TEXT)["error"].startswith("score failed")


def test_lookup_scored_filters(paths, posting_row, monkeypatch, tmp_path):
    conn = db.connect(tmp_path / "l.db")
    db.insert_posting(conn, posting_row(hash="1", company="Acme", title="AI Engineer", score=90))
    db.insert_posting(conn, posting_row(hash="2", company="Beta", title="Data Analyst", score=60))
    db.insert_posting(conn, posting_row(hash="3", company="Acme", title="ML Intern", score=70))
    conn.commit()
    monkeypatch.setattr(mcp_server.db, "connect", lambda: conn)
    rows = mcp_server.lookup_scored()
    assert [r["score"] for r in rows] == [90, 70, 60]
    assert [r["company"] for r in mcp_server.lookup_scored(company="acm")] == ["Acme", "Acme"]
    assert [r["title"] for r in mcp_server.lookup_scored(title_contains="Analyst")] == [
        "Data Analyst"
    ]
    assert mcp_server.lookup_scored(company="Acme", title_contains="Intern")[0]["title"] == (
        "ML Intern"
    )


def test_main_runs_server(monkeypatch):
    ran = {}
    monkeypatch.setattr(mcp_server.mcp, "run", lambda: ran.setdefault("ok", True))
    mcp_server.main()
    assert ran["ok"]
