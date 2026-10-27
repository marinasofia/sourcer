"""Shared fixtures. No test touches the network or the Claude API.

``fake_client`` builds an Anthropic client stand-in whose ``messages.create``
returns canned texts in order. ``paths`` points every configured input at a
temporary directory so tests never read or write files outside it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from sourcer import config, llm, score_listings, sqlitedb
from sourcer.apply.recheck import Official
from sourcer.pipeline import scoring, source

SKILLS: dict[str, Any] = {
    "skills": [
        {"skill": "Python", "weight": 10, "category": "Languages", "synonyms": ["python3"]},
        {"skill": "LangGraph", "weight": 10, "category": "AI/ML", "synonyms": ["LangChain"]},
        {"skill": "SQL", "weight": 9, "category": "Data", "synonyms": ["Postgres"]},
    ]
}

# Fixture postings are dated August 2026. Freshness is judged against this
# pinned clock so the suite gives the same answer on any day it runs.
FROZEN_NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _board_recheck_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scoring asks the company's board first; offline, every board says
    "listed, date unknown". Tests of the check itself set their own answer."""
    monkeypatch.setattr(score_listings, "RECHECK", lambda row: (None, Official(None)))


@pytest.fixture(autouse=True)
def _short_fixture_text_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixture postings carry one line of text; the rule that a posting needs
    real text before it is scored has its own test."""
    monkeypatch.setattr(scoring, "MIN_TEXT", 1)


@pytest.fixture(autouse=True)
def _no_fetch_retry_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """A board that fails to connect is tried again after a pause; tests do not wait."""
    monkeypatch.setattr(source, "RETRY_PAUSE_S", 0)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr(config, "utc_now", lambda: FROZEN_NOW)
    return FROZEN_NOW


PROFILE: dict[str, Any] = {
    "education": "Bachelor's",
    "in_progress": True,
    "grad_year": 2027,
    "grad_month": 5,
    "max_years_experience": 2,
}

COMPANIES: list[dict[str, str]] = [
    {"company": "TestCo", "ats": "greenhouse", "slug": "testco", "sector": "Payments"},
    {"company": "LeverCo", "ats": "lever", "slug": "leverco", "sector": "AI labs"},
]

GOOD_PARSE_SCORE: dict[str, Any] = {
    "title": "AI Engineer Intern",
    "location": "New York, NY",
    "requirements": ["Python", "LangGraph"],
    "seniority": "intern",
    "tech_stack": ["Python", "LangGraph"],
    "employment_type": "intern",
    "start_date": "summer 2027",
    "years_experience_min": None,
    "work_mode": "onsite",
    "score": 88,
    "rationale": "Strong Python and LangGraph match.",
}


def worker_replies(
    parse_score: dict[str, Any] | None = None,
    quotes: dict[str, str] | None = None,
    *,
    scored: bool = True,
) -> list[str]:
    """The two worker replies for one posting: the extraction (each fact with
    the posting's words in ``quotes``; a fact without them is read as unknown),
    then the fit score unless the rules stop the posting first."""
    ps = parse_score or GOOD_PARSE_SCORE
    keys = ("employment_type", "start_date", "work_mode", "years_experience_min", "seniority",
            "requirements", "tech_stack")  # fmt: skip
    quote_keys = ("employment_quote", "start_quote", "work_mode_quote", "years_quote",
                  "seniority_quote")  # fmt: skip
    extraction = {k: ps.get(k) for k in keys}
    extraction.update({k: (quotes or {}).get(k, "") for k in quote_keys})
    out = [json.dumps(extraction)]
    if scored:
        out.append(json.dumps({"score": ps["score"], "rationale": ps["rationale"]}))
    return out


def make_client(replies: Iterable[str], *, cache_write: int = 0, cache_read: int = 0) -> MagicMock:
    """A fake client whose messages.create returns the given texts in order."""
    client = MagicMock()
    responses = []
    for text in replies:
        r = MagicMock()
        r.content = [MagicMock(type="text", text=text)]
        r.stop_reason = "end_turn"
        r.usage.input_tokens = 100
        r.usage.output_tokens = 50
        r.usage.cache_creation_input_tokens = cache_write
        r.usage.cache_read_input_tokens = cache_read
        responses.append(r)
    client.messages.create.side_effect = responses
    return client


@pytest.fixture(autouse=True)
def _stable_cost_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite never spends, but rules branch on the cap; pin it so the
    ambient SOURCER_COST_CAP of the machine running the tests cannot change
    outcomes. Tests that need another value override it."""
    monkeypatch.setattr(config, "COST_CAP_USD", 2.5)


@pytest.fixture
def fake_client() -> Callable[..., MagicMock]:
    return make_client


@pytest.fixture
def tmp_db(tmp_path: Path) -> sqlite3.Connection:
    return sqlitedb.connect(tmp_path / "test.db")


@pytest.fixture
def paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point every configured input and output at ``tmp_path``.

    Writes a skills vector, a company list, and a profile there, and sets the
    database path, so any code path that reads ``config.*_PATH`` stays inside
    the temporary directory.
    """
    (tmp_path / "skills_vector.json").write_text(json.dumps(SKILLS))
    (tmp_path / "companies.json").write_text(json.dumps(COMPANIES))
    (tmp_path / "profile.json").write_text(json.dumps(PROFILE))
    monkeypatch.setattr(config, "SKILLS_PATH", tmp_path / "skills_vector.json")
    monkeypatch.setattr(config, "COMPANIES_PATH", tmp_path / "companies.json")
    monkeypatch.setattr(config, "PROFILE_PATH", tmp_path / "profile.json")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "sourcer.db")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def posting_row() -> Callable[..., dict[str, Any]]:
    """Factory for a storable posting dict with sensible defaults."""

    def _make(**over: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "hash": "h-" + str(over.get("title", "AI Engineer")) + str(over.get("url", "u")),
            "company": "TestCo",
            "title": "AI Engineer",
            "url": "https://boards.greenhouse.io/testco/jobs/1",
            "location": "New York, NY",
            "employment_type": "intern",
            "start_date": "summer 2027",
            "requirements": ["Python"],
            "seniority": "intern",
            "tech_stack": ["Python"],
            "score": 80,
            "rationale": "Strong Python match.",
            "timing_unclear": False,
            "excluded": False,
            "exclusion_reason": None,
            "source": "ats",
            "posted_at": "2026-08-20",
        }
        base.update(over)
        return base

    return _make


def current_trace(skills: dict[str, Any]) -> dict[str, Any]:
    """A score trace that today's scorer would have written, so a twin counts as current."""
    basis = llm.score_basis(skills)
    note = {k: basis[k] for k in ("model", "evidence", "scorer")}
    facts = {"model": config.EXTRACT_MODEL, "checked": {}, "quotes": {}}
    return {"input": {**note, "prompt": next(iter(basis["prompts"]))}, "reads": [], "facts": facts}
