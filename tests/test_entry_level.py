"""Entry-level admission must reject explicit experience requirements before AI."""

from collections import Counter
from unittest.mock import patch

import pytest

from sourcer import config, sources
from sourcer.pipeline.filters import check_exclusion
from tests.conftest import FROZEN_NOW


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5+ years of experience in Solutions Engineering.", 5),
        ("4+ years of relevant experience", 4),
        ("At least three years of professional experience", 3),
        ("0-2 years of experience", 0),
        ("Two to four years of work experience", 2),
        ("2.5 years of relevant experience", 2.5),
        ("<p><strong>4+</strong> years of experience</p>", 4),
        ("&lt;p&gt;4+ years of experience&lt;/p&gt;", 4),
        ("3 years of experience with Python; 1 year of experience with SQL", 3),
        ("Bachelor's and 4 years of experience or master's and 2 years of experience", 2),
        ("Requirements\n1 year of experience\nPreferred qualifications:\n4 years of experience", 1),
        (
            "Preferred qualifications:\n4 years of experience\n"
            "What you need:\n3 years of experience",
            3,
        ),
        ("4 years of experience preferred; 2 years of experience required", 2),
        ("3 years of experience required, master's preferred", 3),
        ("4 years of experience not required", None),
        ("No prior experience required", 0),
        ("Our company was founded 10 years ago.", None),
        ("4 years of experience preferred", None),
        ("", None),
    ],
)
def test_explicit_experience(text, expected):
    assert config.explicit_experience_minimum(text) == expected


@pytest.mark.parametrize(
    ("title", "description", "admit"),
    [
        ("AI Solutions Engineer", "5+ years of experience. Full-time benefits.", False),
        ("AI Solutions Strategist", "4+ years of relevant experience. Full-time benefits.", False),
        ("Data Analyst Intern", "Three years of experience required", False),
        ("Data Scientist", "Full-time position", False),
        ("Data Scientist", "A university degree and full-time availability", False),
        ("Data Scientist", "Start in summer 2027", False),
        ("Senior Data Scientist", "Entry-level benefits", False),
        ("Mid-level Data Scientist", "1 year of experience", False),
        ("Data Science Intern, Spring 2027", "Python and SQL", True),
        ("Data Science Intern, Summer 2027", "Python and SQL", True),
        ("Entry-Level Data Scientist", "Full-time", True),
        ("Data Scientist", "0-2 years of experience", True),
        ("Data Scientist", "No professional experience required", True),
    ],
)
def test_student_admission(title, description, admit):
    assert (
        config.target_window_evidence({"title": title, "description": description}) == "yes"
    ) is admit


def test_structured_experience_cannot_bypass_admission():
    assert (
        config.target_window_evidence({"title": "Data Analyst Intern", "years_experience_min": 4})
        == "no"
    )


def test_collection_removes_overqualified_roles_before_model_triage():
    def posting(title, description):
        return {
            "title": title,
            "description": description,
            "location": "New York, NY",
            "url": "https://jobs.example.com/1",
            "posted_at": FROZEN_NOW.isoformat(),
        }

    raw = [
        posting("AI Solutions Engineer", "5+ years of experience"),
        posting("AI Solutions Strategist", "4+ years of relevant experience"),
        posting("Deployment Wizard", "4+ years of experience. Internship."),
        posting("Data Scientist", "Entry-level. 0-2 years of experience."),
        posting("Data Science Intern", "Summer 2027 internship"),
    ]
    with patch("sourcer.sources.fetch_postings", return_value=raw):
        kept, unsure, dropped = sources.fetch_company({"company": "Example"})
    # every title that passes the gates goes to the title check
    assert not kept
    assert [job["title"] for job in unsure] == ["Data Scientist", "Data Science Intern"]
    assert dropped == 3


@pytest.mark.parametrize("years", [2.5, 3, 4, 5])
def test_parsed_experience_is_a_hard_exclusion(years):
    posting = {"title": "Data Scientist", "years_experience_min": years, "seniority": "entry"}
    assert check_exclusion(posting, {"max_years_experience": 2})
    assert check_exclusion(posting, None)


@pytest.mark.parametrize("years", [0, 1, 2])
def test_parsed_entry_level_experience_is_allowed(years):
    posting = {"title": "Data Scientist", "years_experience_min": years, "seniority": "entry"}
    assert check_exclusion(posting, {"max_years_experience": 2}) is None


def test_stricter_profile_limit_and_mid_level_work():
    assert check_exclusion(
        {"title": "Data Scientist", "years_experience_min": 1}, {"max_years_experience": 0}
    )
    assert check_exclusion({"title": "Data Scientist", "seniority": "mid"})


@pytest.mark.parametrize(
    "title",
    [
        "Machine Learning Engineer II (Servicing ML)",
        "Data Analyst III",
        "Data Scientist 2",
        "Software Engineer (L5)",
        "Founding Forward Deployed Engineer",
    ],
)
def test_level_and_founding_titles_are_senior(title: str) -> None:
    assert config.is_senior_title(title)


@pytest.mark.parametrize(
    "title",
    [
        "Data Analyst I",
        "Data Scientist, Summer 2027 Intern",
        "Member of Technical Staff",
        "AI Engineer",
        "Associate AI Engineer, Finance Transformation",
    ],
)
def test_entry_titles_stay_entry(title: str) -> None:
    assert not config.is_senior_title(title)


def test_title_scoped_outside_nyc_is_a_location_conflict() -> None:
    assert config.title_location_conflict("Forward Deployed Engineer (West Coast)")
    assert config.title_location_conflict("Data Scientist, London")
    assert not config.title_location_conflict("Data Scientist, New York or London")
    assert not config.title_location_conflict("Data Engineer, GTM")


def test_entry_evidence_is_three_way() -> None:
    assert config.target_window_evidence({"title": "Data Science Intern"}) == "yes"
    assert config.target_window_evidence({"title": "Senior Data Scientist"}) == "no"
    assert (
        config.target_window_evidence(
            {"title": "Data Engineer", "description": "Requirements: 5+ years of experience"}
        )
        == "no"
    )
    assert config.target_window_evidence({"title": "Data Engineer, GTM"}) == "unknown"


def test_role_titles_without_level_signal_go_to_parse() -> None:
    raw = [
        {
            "title": "Data Engineer, GTM",
            "location": "New York, NY",
            "url": "u1",
            "description": "Build pipelines.",
            "posted_at": FROZEN_NOW.isoformat(),
        },
        {
            "title": "Deployment Wizard",
            "location": "New York, NY",
            "url": "u2",
            "description": "Build pipelines.",
            "posted_at": FROZEN_NOW.isoformat(),
        },
        {
            "title": "Forward Deployed Engineer (West Coast)",
            "location": "Remote US",
            "url": "u3",
            "description": "internship",
            "posted_at": FROZEN_NOW.isoformat(),
        },
    ]
    with patch("sourcer.sources.fetch_postings", return_value=raw):
        kept, unsure, dropped = sources.fetch_company({"company": "TestCo"})
    # every title goes to the title check, level wording or not: no list of
    # role names decides
    assert not kept
    assert [p["title"] for p in unsure] == ["Data Engineer, GTM", "Deployment Wizard"]
    assert dropped == 1


STUDENT = {"education": "Bachelor's", "in_progress": True}
IBM_VISA = (
    "IBM will not be providing Visa sponsorship for these positions now or in the future. "
    "Therefore, to be considered for this position, you must have the ability to work without "
    "a need for current or future Visa sponsorship. Individuals with temporary visas such as E, "
    "F-1 (including those with OPT or CPT), H-1, H-2, L-1, B, J or TN, or who need sponsorship "
    "for work authorization now or in the future, are not eligible for hire."
)
VISA = {**STUDENT, "student_visa": True}


@pytest.mark.parametrize(
    ("line", "profile", "expected"),
    [
        ("Requirements: PhD in Computer Science required.", STUDENT, "phd_required"),
        ("Master's degree in Statistics required.", STUDENT, "masters_required"),
        ("Bachelor's or Master's degree required.", STUDENT, None),
        ("Preferred qualifications:\nPhD in a related field.", STUDENT, None),
        ("PhD required.", {"education": "Master's"}, None),
        ("Must hold an active Top Secret clearance.", STUDENT, "clearance_required"),
        ("Secret clearance required.", {**STUDENT, "has_clearance": True}, None),
        ("Must be a U.S. citizen.", {**STUDENT, "us_citizen": False}, "citizenship_required"),
        ("Must be a U.S. citizen.", STUDENT, None),  # unknown status: no opinion
        ("We do not sponsor visas.", {**STUDENT, "needs_sponsorship": True}, "no_sponsorship"),
        ("We do not sponsor visas.", STUDENT, None),
        ("Build data pipelines in SQL.", STUDENT, None),
        (IBM_VISA, VISA, "student_visa_barred"),
        (IBM_VISA, STUDENT, None),  # unknown status: no opinion
        ("We are unable to sponsor visas at this time.", VISA, None),
        ("You may opt in to sponsorship updates now or in the future.", VISA, None),
    ],
)
def test_eligibility_conflicts_read_only_mandatory_lines(
    line: str, profile: dict, expected: str | None
) -> None:
    assert config.eligibility_conflict(line, profile) == expected


def test_fetch_company_counts_every_drop_reason() -> None:
    now = FROZEN_NOW.isoformat()
    raw = [
        {
            "title": "Data Scientist",
            "location": "NYC",
            "url": "u1",
            "description": "internship",
            "posted_at": now,
        },
        {"title": "Data Scientist", "location": "NYC", "url": "u2", "description": "x"},
        {
            "title": "Data Scientist",
            "location": "NYC",
            "url": "u3",
            "description": "x",
            "posted_at": "2026-01-01",
        },
        {
            "title": "Data Scientist",
            "location": "Paris, France",
            "url": "u4",
            "description": "internship",
            "posted_at": now,
        },
        {
            "title": "Data Scientist",
            "location": "NYC",
            "url": "u5",
            "description": "Requirements: PhD required.",
            "posted_at": now,
        },
        {
            "title": "Senior Barista",
            "location": "NYC",
            "url": "u6",
            "description": "x",
            "posted_at": now,
        },
    ]
    counts: Counter[str] = Counter()
    with patch("sourcer.sources.fetch_postings", return_value=raw):
        _, kept, dropped = sources.fetch_company(
            {"company": "TestCo"}, profile=STUDENT, reasons=counts
        )
    # u3 is old but still listed on the company's board, so it stays open
    # u2 has no date but is listed, so it stays open too
    assert [p["url"] for p in kept] == ["u1", "u2", "u3"]
    assert dict(counts) == {
        "location": 1,
        "phd_required": 1,
        "above_entry": 1,
    }
    assert dropped == 3


def test_staffing_agency_boards_are_dropped() -> None:
    raw = [
        {
            "title": "Data Analyst Intern",
            "location": "NYC",
            "url": "u1",
            "description": "internship",
            "posted_at": FROZEN_NOW.isoformat(),
        }
    ]
    counts: Counter[str] = Counter()
    with patch("sourcer.sources.fetch_postings", return_value=raw):
        kept, _, _ = sources.fetch_company({"company": "Jobot"}, reasons=counts)
    assert kept == [] and counts == {"staffing_agency": 1}


@pytest.mark.parametrize(
    "title",
    [
        "Associate Product Manager",
        "Product Manager, Payments",
        "Product Analyst",
        "Business Operations Associate",
        "Strategy & Operations Analyst",
        "Chief of Staff Associate",
        "Sales Engineer, New Grad",
        "Technical Consultant",
        "Implementation Specialist",
    ],
)
def test_new_role_families_match_and_are_not_senior(title: str) -> None:
    assert config.ROLE_RE.search(title)
    assert not config.is_senior_title(title)


@pytest.mark.parametrize(
    "title",
    [
        "Senior Product Manager",
        "Lead Product Manager",
        "Group Product Manager",
        "Principal Product Manager",
        "Engineering Manager",
        "Product Manager II",
        "Product Manager ll \u2013 Code Intelligence",
        "Chief of Staff",
    ],
)
def test_senior_product_and_manager_titles_stay_senior(title: str) -> None:
    assert config.is_senior_title(title)


@pytest.mark.parametrize(
    ("title", "school", "restricted"),
    [
        ("AI & Data Co-Op (Northeastern University), Spring 2027", None, True),
        ("Data Intern (Hudson College)", "Hudson", False),
        ("Data Intern (Hudson College)", None, True),
        ("Data Analyst, Columbia University Irving Medical Center", None, False),
        ("Cooperative Data Intern", None, False),
        ("Machine Learning Intern", None, False),
    ],
)
def test_school_restricted_programs(title: str, school: str | None, restricted: bool) -> None:
    assert config.school_restricted(title, school) is restricted


GRAD_2027 = {"grad_year": 2027, "grad_month": 5}
YOURS = "(you graduate 5/2027)"


@pytest.mark.parametrize(
    "line,reason",
    [
        ("Currently pursuing a bachelor's with an anticipated graduation date between "
         "December 2027 and June 2028", f"graduation window 12/2027 to 6/2028 {YOURS}"),
        ("Expected graduation between Fall 2026 and Spring 2027", None),
        ("Graduating from December 2026 to May 2027", None),
        ("Must graduate between 2028 and 2029", f"graduation window 1/2028 to 12/2029 {YOURS}"),
        ("Graduation in May 2027 preferred", None),
    ],
)  # fmt: skip
def test_graduation_window(line: str, reason: str | None) -> None:
    assert config.outside_graduation_window(line, GRAD_2027) == reason
    assert config.outside_graduation_window(line, None) is None


@pytest.mark.parametrize(
    "text,reason",
    [
        # a real internship posting that bars seniors
        ("A current junior (graduation date of December 2027 or May 2028) or current sophomore "
         "(graduation date of December 2028 or May 2029) who is fueled by collaboration.",
         f"graduation window 12/2027 to 5/2029 {YOURS}"),
        ("Class of 2028 students only.", f"graduation window 1/2028 to 12/2028 {YOURS}"),
        ("Rising seniors with a graduation date of 2028 or later.",
         f"graduation 1/2028 or later {YOURS}"),
        ("Open to current juniors and sophomores.",
         "open to current juniors and sophomores (you graduate 5/2027, so a senior now)"),
        ("Must be returning to school after the internship in Fall 2027.",
         f"must return to school after the internship {YOURS}"),
        ("Open to students graduating in 2027 or 2028.", None),
        ("Candidates must be graduating in December 2026 or May 2027.", None),
        ("Recent graduates or students graduating by June 2027.", None),
        ("Current juniors and seniors are welcome to apply.", None),
        ("You will graduate with a bachelor's degree, helped by mentors from 2026 cohort.", None),
    ],
)  # fmt: skip
def test_graduation_rules_read_lists_class_standing_and_return(
    text: str, reason: str | None
) -> None:
    profile = {**GRAD_2027, "in_progress": True}
    assert config.outside_graduation_window(text, profile) == reason  # clock pinned to Aug 2026


def test_graduation_window_excludes_in_both_gates() -> None:
    text = "Qualifications\nAnticipated graduation date between December 2027 and June 2028"
    assert config.eligibility_conflict(text, GRAD_2027) == "graduation_window"
    assert check_exclusion({"description": text, "title": "Summer Associate"}, GRAD_2027)


def test_years_with_a_named_field_are_read_and_rule_out_the_role() -> None:
    # Dataminr's Forward Deployed Engineer, scored 92 on 2026-10-07 before its text was read
    line = "3+ years of professional software engineering experience delivering production systems"
    assert config.explicit_experience_minimum(line) == 3
    assert config.explicit_experience_minimum("10 years old company with great experience") is None
    assert check_exclusion({"title": "Forward Deployed Engineer", "description": line}, GRAD_2027)


def test_a_companys_own_history_is_not_a_requirement() -> None:
    line = "We bring 30+ years of experience serving financial institutions."
    assert config.explicit_experience_minimum(line) is None


def test_gig_work_titles_are_dropped_and_staff_roles_kept() -> None:
    def posting(title: str) -> dict[str, str]:
        return {
            "title": title,
            "location": "New York, NY",
            "url": title,
            "description": "",
            "posted_at": FROZEN_NOW.isoformat(),
        }

    raw = [
        posting("Chemistry - Graduates - AI Training - Charlotte, US"),
        posting("Data AI Annotator - Flexible Hours"),
        posting("Machine Learning Engineer, AI Training Infrastructure"),
    ]
    counts: Counter[str] = Counter()
    with patch("sourcer.sources.fetch_postings", return_value=raw):
        kept, unsure, _ = sources.fetch_company({"company": "Prolific"}, reasons=counts)
    assert counts["gig_work"] == 2
    assert [p["title"] for p in kept + unsure] == [
        "Machine Learning Engineer, AI Training Infrastructure"
    ]
