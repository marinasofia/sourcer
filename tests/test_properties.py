"""Property-based tests for the deterministic policy functions."""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from sourcer import config, llm, sqlitedb
from sourcer.export_showcase import GAP_MARKERS, sanitize_blurb

MONTHS = 12


@settings(deadline=None)
@given(st.text())
def test_location_in_scope_total_and_boolean(text: str) -> None:
    out = config.location_in_scope(text)
    assert out is True or out is False


@settings(deadline=None)
@given(st.text(), st.text())
def test_new_york_always_in_scope(prefix: str, suffix: str) -> None:
    assert config.location_in_scope(f"{prefix} new york {suffix}")


@settings(deadline=None)
@given(st.text(min_size=1), st.text(min_size=1), st.text(min_size=1))
def test_posting_hash_is_deterministic_hex(company: str, title: str, url: str) -> None:
    h = sqlitedb.posting_hash(company, title, url)
    assert h == sqlitedb.posting_hash(company, title, url)
    assert len(h) == 64 and set(h) <= set("0123456789abcdef")


@settings(deadline=None)
@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1))
def test_posting_hash_normalizes_cosmetic_differences(value: str) -> None:
    base = sqlitedb.posting_hash(value, "AI Engineer", "NYC")
    assert base == sqlitedb.posting_hash(f"  {value.upper()}  ", "AI-Engineer", "New York City")
    assert base != sqlitedb.posting_hash(f"{value}x", "AI Engineer", "NYC")


@settings(deadline=None)
@given(st.text())
def test_start_terms_yields_valid_year_month_pairs(text: str) -> None:
    for year, month in config.start_terms(text):
        assert 2000 <= year <= 2099
        assert 1 <= month <= MONTHS


PARSE_OBJECTS = st.fixed_dictionaries(
    {
        "title": st.text(),
        "location": st.text(),
        "requirements": st.lists(st.text()),
        "seniority": st.sampled_from(["intern", "entry", "mid", "senior", "unspecified"]),
        "tech_stack": st.lists(st.text()),
        "employment_type": st.sampled_from(
            ["intern", "part time", "full time", "contract", "unspecified"]
        ),
        "start_date": st.one_of(st.none(), st.text()),
        "years_experience_min": st.one_of(st.none(), st.integers(0, 40), st.floats(0, 40)),
        "work_mode": st.sampled_from(["remote", "hybrid", "onsite", "unspecified"]),
    }
)


@settings(deadline=None)
@given(PARSE_OBJECTS, st.sampled_from(["", "```json\n{}\n```", "```\n{}\n```"]))
def test_extract_json_round_trips_valid_parse_objects(obj: dict, wrapper: str) -> None:
    text = json.dumps(obj)
    wrapped = wrapper.replace("{}", text) if wrapper else text
    data, err = llm.extract_json(llm.unfence(wrapped), llm.PARSE_REQUIRED)
    assert err is None
    assert data == obj


SENIOR_WORDS = (
    "senior", "staff", "principal", "lead", "director", "head of", "manager", "vp",
    "vice president", "sr", "distinguished", "chief",
)  # fmt: skip


@settings(deadline=None)
@given(st.text(alphabet=st.characters(whitelist_categories=("Ll", "Zs")), max_size=30))
def test_member_of_technical_staff_is_not_senior(filler: str) -> None:
    title = f"{filler} member of technical staff {filler}"
    if any(w in f" {title} " for w in SENIOR_WORDS):
        return
    assert not config.is_senior_title(title)


@settings(deadline=None)
@given(st.one_of(st.none(), st.text()), st.one_of(st.none(), st.integers(0, 100)))
def test_sanitized_blurb_never_carries_gap_vocabulary(text: str | None, score: int | None) -> None:
    assert not GAP_MARKERS.search(sanitize_blurb(text, score))
