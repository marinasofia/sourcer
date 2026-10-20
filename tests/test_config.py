"""Profile loading and the start-date parsing rules."""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sourcer import config
from tests.conftest import PROFILE


def test_load_profile_missing_file(tmp_path: Path) -> None:
    assert config.load_profile(tmp_path / "nope.json") is None


def test_load_profile_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "profile.json"
    p.write_text("{not json")
    assert config.load_profile(p) is None


def test_load_profile_non_object_json(tmp_path: Path) -> None:
    p = tmp_path / "profile.json"
    p.write_text("[1, 2]")
    assert config.load_profile(p) is None


def test_load_profile_valid_and_default_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    p = tmp_path / "profile.json"
    p.write_text(json.dumps(PROFILE))
    assert config.load_profile(p) == PROFILE
    monkeypatch.setattr(config, "PROFILE_PATH", p)
    assert config.load_profile() == PROFILE


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("fall 2026", [(2026, 9)]),
        ("2026 fall", [(2026, 9)]),
        ("Winter 2027 or Spring 2027", [(2027, 1), (2027, 3)]),
        ("January 2027", [(2027, 1)]),
        ("starts 2027-02", [(2027, 2)]),
        ("2027 new grad", [(2027, 6)]),
        ("new grad 2028", [(2028, 6)]),
        ("summer 2027 new grad", [(2027, 6)]),
        ("flexible", []),
        ("", []),
        (None, []),
    ],
)
def test_start_terms_patterns(text: str | None, expected: list[tuple[int, int]]) -> None:
    assert config.start_terms(text) == expected  # type: ignore[arg-type]


def test_starts_before_graduation_without_profile_or_grad_year() -> None:
    assert not config.starts_before_graduation("fall 2026", None)
    assert not config.starts_before_graduation("fall 2026", {})
    assert not config.starts_before_graduation("fall 2026", {"grad_year": None})


def test_starts_before_graduation_default_grad_month() -> None:
    profile = {"grad_year": 2027}
    assert config.starts_before_graduation("may 2027", profile)
    assert not config.starts_before_graduation("june 2027", profile)


@pytest.mark.parametrize(
    ("start", "excluded"),
    [
        ("April 2027", True),
        ("May 2027", False),
        ("Summer 2027", False),
        ("Fall 2027", False),
        ("April 2027 or June 2027", False),
        ("flexible", False),
    ],
)
def test_explicit_full_time_availability(start: str, excluded: bool) -> None:
    profile = {"grad_year": 2027, "grad_month": 5, "earliest_start_full_time": "2027-05"}
    assert config.starts_before_graduation(start, profile) is excluded


def test_explicit_availability_without_graduation() -> None:
    profile = {"earliest_start_full_time": "2027-05"}
    assert config.starts_before_graduation("April 2027", profile)


@pytest.mark.parametrize("available", ["", "May 2027", "2027-13", "2027-00", 202705])
def test_invalid_availability_is_rejected(available: object) -> None:
    with pytest.raises(ValueError, match="earliest_start_full_time"):
        config.starts_before_graduation("May 2027", {"earliest_start_full_time": available})


@pytest.mark.parametrize(
    "posting",
    [
        {"title": "Data Scientist Intern, Spring 2027"},
        {"title": "ML Engineer Intern, Summer 2027"},
        {"title": "Junior Data Scientist", "employment_type": "full time"},
        {"title": "Data Scientist", "description": "Full-time. 0-2 years of experience."},
        {"title": "Data Scientist", "description": "Entry-level fulltime position."},
    ],
)
def test_target_internships_and_regular_full_time_roles(posting: dict[str, str]) -> None:
    assert config.target_window_evidence(posting) == "yes"


def test_unclassified_role_does_not_pass_window_filter() -> None:
    assert config.target_window_evidence({"title": "Data Scientist"}) != "yes"


def test_is_senior_title_none_and_exception() -> None:
    assert not config.is_senior_title(None)
    assert not config.is_senior_title("Member of Technical Staff")
    assert config.is_senior_title("Senior Member of Technical Staff")


def test_location_in_scope_none() -> None:
    assert not config.location_in_scope(None)  # type: ignore[arg-type]


def test_project_root_honours_sourcer_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SOURCER_ROOT", str(tmp_path))
    reloaded = importlib.reload(config)
    try:
        assert tmp_path == reloaded.PROJECT_ROOT
        assert tmp_path / "companies.json" == reloaded.COMPANIES_PATH
    finally:
        monkeypatch.delenv("SOURCER_ROOT")
        importlib.reload(config)


def test_is_fresh_is_the_one_freshness_rule() -> None:
    now = datetime(2026, 10, 13, tzinfo=UTC)
    day = lambda n: (now - timedelta(days=n)).date().isoformat()  # noqa: E731
    assert config.is_fresh(day(config.FRESHNESS_DAYS), now=now)
    assert not config.is_fresh(day(config.FRESHNESS_DAYS + 1), now=now)
    assert config.is_fresh(None, now=now) and config.is_fresh("not a date", now=now)  # undated
    # a role still listed on its company's own board is never too old
    assert config.is_fresh(day(400), listed_on_board=True, now=now)
    # the old name stays for callers that import it: dated postings only
    assert config.posting_is_fresh(day(3), now) and not config.posting_is_fresh(None, now)
    assert not config.posting_is_fresh(day(config.FRESHNESS_DAYS + 1), now)
