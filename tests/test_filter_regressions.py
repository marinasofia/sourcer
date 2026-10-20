"""Guardrail: real posting wording the filters once got wrong, in both
directions. Every fix adds the miss here as a drop case and a nearby wording
that must stay in, so a broader pattern cannot quietly empty the board."""

import pytest

from sourcer import config, eligibility, llm
from sourcer.pipeline.filters import check_exclusion

PROFILE = {
    "education": "Bachelor's",
    "in_progress": True,
    "student_visa": True,
    "needs_sponsorship": True,
}


@pytest.mark.parametrize(
    "line",
    [
        # 2026-10-10: three phrasings on the board as fits
        "We do not anticipate providing visa sponsorship/support for this position.",
        "Visa sponsorship is not offered for this role.",
        "This position is not eligible for employment visa sponsorship now or in the future.",
        "Sponsorship will not be provided for this role.",
    ],
)
def test_no_sponsorship_wording_drops(line: str) -> None:
    found = config.eligibility_evidence(line, PROFILE)
    assert found is not None
    assert found[0] == "no_sponsorship"
    assert "sponsor" in found[1].lower()  # the trace quotes the posting's own words


@pytest.mark.parametrize(
    "line",
    [
        "U.S. work authorization.",
        "Applicants must be legally authorized to work in the United States.",
        "Visa sponsorship is available for this role.",
        "We sponsor visas for exceptional candidates.",
    ],
)
def test_work_authorization_alone_stays_in(line: str) -> None:
    assert config.eligibility_conflict(line, PROFILE) is None


def test_residency_clause_names_the_country() -> None:
    text = "This is a remote position open to candidates residing in Canada."
    assert config.description_location_conflict(text) == text
    assert not config.location_in_scope("Remote - CA", text)


def test_residency_clause_in_the_us_stays_in_for_full_time() -> None:
    text = "This is a remote position open to candidates residing in the United States."
    assert config.description_location_conflict(text, wide=True) is None


def test_full_time_us_office_requirement_is_fine_in_the_wide_window() -> None:
    text = "You must work from our San Francisco office three days a week."
    assert config.description_location_conflict(text) is not None  # NYC-only roles
    assert config.description_location_conflict(text, wide=True) is None
    assert config.location_in_scope("San Francisco, CA", text, wide=True)


@pytest.mark.parametrize("loc", ["SF Office", "Charlotte", "Raleigh; Remote", "Salt Lake City"])
def test_us_cities_without_a_state_count_as_us(loc: str) -> None:
    assert config.in_the_us(loc)


def test_foreign_cities_still_do_not() -> None:
    assert not config.in_the_us("London; Toronto")


def test_ny_or_sf_office_is_an_nyc_option() -> None:
    text = "You will need to be able to work out of our NY or SF office during this time."
    assert config.description_location_conflict(text) is None


# 2026-10-10 gold set: phrasings the gates let through to paid model checks


@pytest.mark.parametrize(
    ("line", "years"),
    [
        ("- 5+ years in product management on platform or API products.", 5.0),
        ("3 to 6+ years administering and evolving Ironclad and/or DocuSign.", 3.0),
        ("- 7+ years of quota\u2011carrying sales experience in SaaS, ideally with AI.", 7.0),
        ("Someone with 3+ years closing experience in mid-market or enterprise.", 3.0),
        ("Have a proven track record of 8+ years\u2019 experience in B2B sales.", 8.0),
        ("- 4+ years of software, data, or machine learning engineering experience.", 4.0),
        ("- 5+ years building production software.", 5.0),
    ],
)
def test_more_experience_wordings_are_read(line: str, years: float) -> None:
    assert config.explicit_experience_minimum(line) == years


@pytest.mark.parametrize(
    "line",
    [
        "Founded 25 years ago in New York.",
        "A 2 year rotational program across four teams.",
        "Preferred: 5+ years in consulting.",  # optional before the requirement
        "We have 10 years in business serving clients.",  # no plus or range
    ],
)
def test_years_that_are_not_a_requirement_stay_unread(line: str) -> None:
    assert config.explicit_experience_minimum(line) is None


@pytest.mark.parametrize(
    "line",
    [
        "Crowe is not sponsoring for work authorization at this time.",
        "Authorization to work in the United States without sponsorship now or in the future.",
    ],
)
def test_more_sponsorship_wordings_drop(line: str) -> None:
    assert config.eligibility_conflict(line, PROFILE) == "no_sponsorship"


ON_OPT = {"student_visa": True, "needs_sponsorship": False, "us_citizen": False}


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # OPT covers the start: a plain no, or a work authorization line, is fine
        ("Visa sponsorship is not offered for this role.", None),
        (
            "Employment eligibility to work with American Express in the United States is required "
            "as the company will not pursue visa sponsorship for these positions",
            None,
        ),
        ("Applicants must be legally authorized to work in the United States.", None),
        ("You may opt in to sponsorship updates now or in the future.", None),
        # no sponsorship later, after OPT, rules the candidate out
        ("We are unable to sponsor visas now or in the future.", "student_visa_barred"),
        (
            "Authorization to work in the United States without sponsorship now or in the future.",
            "student_visa_barred",
        ),
        (
            "This position is not eligible for employment visa sponsorship now or in the future.",
            "student_visa_barred",
        ),
        ("We will not sponsor H-1B visas for this role.", "student_visa_barred"),
        ("We do not provide current or future sponsorship.", "student_visa_barred"),
        # an employer that takes OPT outweighs a general no sponsorship line
        (
            "US work authorization required. Open to candidates with OPT. This position does not "
            "offer visa sponsorship, now or in the future.",
            None,
        ),
        ("OPT/CPT candidates welcome. We cannot sponsor now or in the future.", None),
        # an explicit bar on student visas still rules the candidate out
        (
            "Open to candidates with OPT. F-1 visa holders with OPT who will require H-1B status "
            "will not be considered.",
            "student_visa_barred",
        ),
    ],
)
def test_on_opt_only_future_sponsorship_rules_her_out(line: str, expected: str | None) -> None:
    assert config.eligibility_conflict(line, ON_OPT) == expected


def test_opt_holders_not_considered_is_a_student_visa_bar() -> None:
    line = (
        "F-1 visa holders with Optional Practical Training (OPT) who will require H-1B "
        "status to continue employment will not be considered."
    )
    assert config.eligibility_conflict(line, PROFILE) == "student_visa_barred"


def test_internship_or_co_op_is_not_a_school_program() -> None:
    assert not config.school_restricted("Summer 2027 Business Operations Internship/Co-op")
    assert config.school_restricted("AI Co-Op (Northeastern University)")
    assert config.school_restricted("Data Co-op, Spring 2027")


def test_form_answers_are_not_requirements() -> None:
    profile = {**PROFILE, "us_citizen": False}
    answer = "I am a US Citizen or Legal Permanent Resident."
    rule = "Candidate must be a US citizen per contract requirements."
    assert config.eligibility_conflict(answer, profile) is None
    assert config.eligibility_conflict(rule, profile) == "citizenship_required"


@pytest.mark.parametrize(
    ("title", "dropped"),
    [
        ("Deployment Strategist - US Government", True),
        ("Year at Palantir - Forward Deployed Software Engineer, Internship - USG", True),
        ("Forward Deployed Software Engineer - U.S. Government", True),
        ("Deployment Strategist", False),
        ("Government Affairs Analyst", False),
    ],
)
def test_a_us_government_track_title_needs_citizenship(title: str, dropped: bool) -> None:
    found = config.eligibility_evidence("", {"us_citizen": False}, title)
    assert (found is not None and found[0] == "citizenship_required") is dropped
    assert config.eligibility_evidence("", {"us_citizen": True}, title) is None


@pytest.mark.parametrize(
    ("title", "start", "dropped"),
    [
        ("Product Management Intern (Winter 2027)", "January 2027", True),
        ("Product Management Intern (Winter 2027)", None, True),  # the title says when
        ("Data Science Intern, Spring 2027", "Spring 2027", True),
        ("Product Management Intern", "Summer 2027", False),
        ("Product Management Intern", "May 2027", False),
        ("Product Management Intern", None, False),  # unstated stays eligible
    ],
)
def test_an_internship_before_her_availability_is_dropped(
    title: str, start: str | None, dropped: bool
) -> None:
    parsed = {"title": title, "start_date": start, "employment_type": "intern"}
    reason = check_exclusion(parsed, {**PROFILE, "earliest_start_internship": "2027-05"})
    assert bool(reason and reason.startswith("internship starts before availability")) is dropped
    # full time roles keep their own rule, and no availability means no rule
    assert check_exclusion({**parsed, "employment_type": "full time"}, PROFILE) is None
    assert check_exclusion(parsed, PROFILE) is None


@pytest.mark.parametrize(
    ("line", "span"),
    [
        (
            "1-3 years of experience in Analytics, Reporting, Finance Technology "
            "or Data Management.",
            "1 to 3",
        ),
        ("2 to 4 years of relevant experience required.", "2 to 4"),
        ("0-3 years of experience in finance.", None),  # open to someone just starting
        ("1-2 years of experience with SQL.", None),
        ("Preferred: 1-3 years of experience in consulting.", None),
    ],
)
def test_a_range_past_two_years_that_starts_at_one_is_read(line: str, span: str | None) -> None:
    assert config.experience_range_too_high(line) == span


def test_one_to_three_years_is_a_lower_scored_stretch_not_a_drop() -> None:
    text = "Requirements: 1-3 years of experience in Analytics or Business Analysis."
    parsed = {
        "title": "Finance Analytics Associate",
        "description": text,
        "seniority": "entry",
        "years_experience_min": 1,
        "employment_type": "full time",
    }
    assert eligibility._rule_years(parsed, {}) is None
    assert eligibility.level_fit(parsed, {"in_progress": True, "grad_year": 2027}) == "stretch"
    checks = {
        "role_fit": "core",
        "stretch": "none",
        "must_haves": [{"need": "SQL", "covered": "yes"}],
        "rationale": "r",
    }
    plain = llm.score_from_checks(dict(checks), "Requirements: SQL.")["score"]
    out = llm.score_from_checks(dict(checks), text)
    assert out["score"] == plain - llm.STRETCH_PENALTY["senior"]
    assert "asks 1 to 3 years of experience" in out["rationale"]
