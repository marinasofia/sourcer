"""Showcase export sanitizer tests: no gap vocabulary may survive."""

from sourcer.export_showcase import (
    FALLBACK,
    GAP_MARKERS,
    derive_type,
    fallback_for,
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
