"""Showcase export sanitizer tests: no gap vocabulary may survive."""

import pytest

from sourcer import db as _db
from sourcer.export_showcase import (
    FALLBACK,
    GAP_MARKERS,
    LeakError,
    build_postings,
    build_summary,
    derive_type,
    employer_names,
    fallback_for,
    leak_scan,
    normalize,
    redact_names,
    sanitize_blurb,
)


def test_keeps_strengths_drops_gap_sentence():
    r = (
        "Excellent match on core skills: Python, SQL, and agent frameworks. "
        "Minor gap: no explicit TypeScript production experience mentioned. "
        "[timing unclear: start date not stated]"
    )
    out = sanitize_blurb(r)
    assert "Excellent match" in out
    assert "TypeScript" not in out
    assert "timing unclear" not in out
    assert not GAP_MARKERS.search(out)


def test_gap_leading_rationale_falls_back():
    r = "Major gaps: no Kubernetes, missing PhD requirement, weak on RL."
    assert sanitize_blurb(r, 90) == FALLBACK


def test_mid_sentence_marker_falls_back():
    # marker inside the only sentence, not a separate trailing one
    r = "Strong on Python but lacks production deployment experience."
    assert sanitize_blurb(r, 90) == FALLBACK


def test_empty_and_none():
    assert sanitize_blurb("", 90) == FALLBACK
    assert sanitize_blurb(None, 90) == FALLBACK


def test_fallback_band_matches_score():
    # a low score must never inherit the "strong alignment" wording
    assert fallback_for(92) == FALLBACK
    assert "Partial" in fallback_for(60)
    assert "Minor" in fallback_for(25)
    assert "Minor" in fallback_for(None)


def test_banded_fallbacks_survive_the_leak_scan():
    for score in (0, 25, 49, 50, 74, 75, 100):
        assert not GAP_MARKERS.search(fallback_for(score))


def test_low_score_gap_rationale_gets_low_band_blurb():
    r = "Major gaps: no Kubernetes, missing PhD requirement, weak on RL."
    assert sanitize_blurb(r, 25) == fallback_for(25)
    assert sanitize_blurb(r, 25) != FALLBACK


def test_redact_names_scrubs_employer_from_role_titles():
    names = {"Palantir", "OpenAI", "Scale AI"}
    out = redact_names("Year at Palantir - Forward Deployed Engineer", names)
    assert "Palantir" not in out
    assert "[employer]" in out
    assert "Forward Deployed Engineer" in out


def test_redact_names_is_case_insensitive_and_whole_word():
    names = {"Ramp", "OpenAI"}
    assert "openai" not in redact_names("openai applied engineer", names).lower()
    # substring of a longer word must survive
    assert redact_names("Ramping up demand forecasting", names) == ("Ramping up demand forecasting")


def test_semicolon_splitting():
    r = "Strong matches in Python and SQL; gaps are PhD enrollment and R. More text."
    out = sanitize_blurb(r)
    assert out.startswith("Strong matches")
    assert "PhD" not in out


def test_derive_type():
    assert derive_type("intern", None) == "internship"
    assert derive_type("full time", "2027 new grad") == "new grad"
    assert derive_type("full time", None) == "full time"
    assert derive_type("unspecified", None) == "unspecified"
    assert derive_type(None, "summer 2027 internship") == "internship"


# Publication checks: text from boards and the model is hostile, so every
# check runs on folded ASCII and a posting that fails one is dropped.

SECTORS = {"Anthropic": "Frontier AI lab", "Scale AI": "AI data platform", "Glean": "AI product"}


def _row(**over):
    base = {
        "company": "Anthropic",
        "title": "Data Analyst",
        "location": "New York, NY",
        "score": 90,
        "rationale": "Core fit, covers 4 of 4 must haves.",
        "employment_type": "intern",
        "start_date": "Summer 2027",
        "posted_at": "2026-11-01",
    }
    base.update(over)
    return base


def test_normalize_folds_invisible_and_look_alike_characters():
    assert normalize("Anthr\u200bopic") == "Anthropic"
    assert normalize("Anthr\u043epic") == "Anthropic"  # Cyrillic o
    assert normalize("\uff27lean caf\u00e9") == "Glean cafe"  # full width G, accent


def test_hidden_and_spaced_employer_names_are_redacted():
    posts = build_postings(
        [
            _row(title="Engineer at Anthr\u200bopic"),
            _row(title="ScaleAI Fellow", company="Scale AI"),
            _row(title="GleanWork Intern", company="Glean"),
        ],
        SECTORS,
    )
    roles = [p["role"] for p in posts]
    assert roles == ["Engineer at [employer]", "[employer] Fellow", "[employer]Work Intern"]
    leak_scan(posts, SECTORS)


def test_one_letter_off_employer_name_drops_the_posting():
    posts = build_postings(
        [_row(rationale="Core fit. Great match for Anthr\u0430pic team.")],
        SECTORS,
    )
    assert posts == []


def test_links_emails_and_phone_numbers_drop_the_posting():
    for text in (
        "Core fit. Apply at jobs.lever.co/acme today.",
        "Core fit. Mail recruiting at hr@acme.example.",
        "Core fit. Call +1 415 555 0100.",
        "Core fit. See careers.g\u200blean.io for more.",
    ):
        assert build_postings([_row(rationale=text)], SECTORS) == []


def test_gap_words_hidden_with_look_alikes_never_survive():
    posts = build_postings(
        [
            _row(
                rationale="Core fit, covers 2 of 2 must haves. She la\u0441ks Kubernetes, has "
                "n\u200bo experience with Go, falls short on Rust, without SQL."
            )
        ],
        SECTORS,
    )
    assert posts[0]["blurb"] == "Core fit, covers 2 of 2 must haves."


def test_employer_names_come_from_the_database_too(tmp_db, paths, posting_row):
    _db.insert_posting(tmp_db, posting_row(hash="r", company="Ramp", url="r1"))
    tmp_db.execute(
        "INSERT INTO companies (name, domain, added_at) VALUES (?, ?, ?)",
        ("Brightwave Labs", "https://www.brightwave.example/", "2026-11-01"),
    )
    names = employer_names(tmp_db, {"TestCo": "Payments"})
    assert {"TestCo", "Ramp", "Brightwave Labs", "brightwave"} <= names
    posts = build_postings(
        [_row(company="TestCo", title="Ramp Analyst")], {"TestCo": "Payments"}, names=names
    )
    assert posts[0]["role"] == "[employer] Analyst"


def test_private_terms_are_replaced_and_checked():
    posts = build_postings(
        [_row(rationale="Core fit. Built Secretproj, a chat agent.")],
        SECTORS,
        terms=["Secretproj"],
    )
    assert "Secretproj" not in posts[0]["blurb"]
    assert "a private project" in posts[0]["blurb"]
    with pytest.raises(LeakError):
        leak_scan([{**posts[0], "blurb": "Built Secretproj."}], SECTORS, ["Secretproj"])


def test_scraped_junk_rows_are_skipped_and_locations_cleaned():
    posts = build_postings(
        [
            _row(title="Engineer Examplecorp NYC Office Visa $1k - $2k Full-time"),
            _row(title="Analyst", location="Anthropic - Atlanta"),
            _row(title="Analyst", location="Remote"),  # same role again: deduplicated
        ],
        SECTORS,
    )
    assert [(p["role"], p["location"]) for p in posts] == [("Analyst", "Location not listed")]


def test_failure_rate_is_over_postings_judged(tmp_db):
    for failures, new in ((10, 100), (0, 100)):
        _db.insert_run_log(
            tmp_db,
            {
                "run_ts": "2026-11-01T00:00:00+00:00",
                "parse_failures": failures,
                "new_postings": new,
            },
        )
    summary = build_summary(tmp_db)
    assert summary["schema_failure_rate"] == 0.05
    assert (summary["runs"], summary["runs_without_failures"]) == (2, 1)
