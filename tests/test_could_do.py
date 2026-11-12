"""Could do: a job she could do well is not lost when her facts match it only loosely."""

from typing import Any

from sourcer import llm

TEXT = "We need someone to run outreach and analyze campaign data for our sales team."


def checks(**extra: Any) -> dict[str, Any]:
    return {
        "role_fit": "other",
        "stretch": "none",
        "must_haves": [
            {"need": "Prospecting", "covered": "no"},
            {"need": "Writing", "covered": "yes"},
        ],
        "rationale": "Built an outreach engine.",
        **extra,
    }


def test_could_do_lifts_a_loosely_matched_job_to_neighbouring_work() -> None:
    strict = llm.score_from_checks(checks(), TEXT)
    lifted = llm.score_from_checks(checks(could_do="yes", could_do_reason="ran CRM outreach"), TEXT)
    b, sp = llm.FIT_BANDS[llm.COULD_DO_FIT]
    assert strict["score"] == 20  # other: 10 + 20 x 0.5
    assert lifted["score"] == round(b + sp * llm.COULD_DO_COVERAGE)
    assert "could do it (ran CRM outreach)" in lifted["rationale"]
    assert (
        lifted["fit_checks"]["could_do"] == "yes"
        and "lifted from 20" in lifted["fit_checks"]["formula"]
    )


def test_maybe_or_no_and_a_stronger_score_are_left_alone() -> None:
    assert llm.score_from_checks(checks(could_do="maybe"), TEXT)["score"] == 20
    assert llm.score_from_checks(checks(could_do="no"), TEXT)["score"] == 20
    core = checks(
        role_fit="core", must_haves=[{"need": "Writing", "covered": "yes"}], could_do="yes"
    )
    assert llm.score_from_checks(core, TEXT)["score"] == 95  # never lowered


def test_avoided_work_stays_low_after_the_lift() -> None:
    """The edge checks still weigh the work she wants and avoids (edge 0 here)."""
    out = llm.score_from_checks(
        checks(
            could_do="yes",
            edge=0,
            edge_quote="run outreach and analyze campaign data",
            coding_gate="no",
        ),
        TEXT,
    )
    assert out["score"] < 50


def test_the_prompt_asks_could_do_separately_from_the_strict_must_haves() -> None:
    assert '"could_do"' in llm.FIT_CHECKS and "judged on ability" in llm.FIT_CHECKS
    assert '"could_do": string' in llm.CHECKS_JSON
