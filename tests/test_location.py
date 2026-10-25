"""Location policy, availability rule, senior-title carve-out, title triage.
No API calls; the triage tests use the mocked client from test_parse."""

import json
from datetime import date
from unittest.mock import MagicMock

import pytest

from sourcer import config, llm
from sourcer.eligibility import check_exclusion

PROFILE = {
    "education": "Bachelor's",
    "in_progress": True,
    "grad_year": 2027,
    "grad_month": 5,
    "max_years_experience": 2,
}


# --- location_in_scope: NYC or genuinely remote US, nothing else ---


def test_nyc_variants_pass():
    for loc in ("New York, NY", "NYC", "Brooklyn, New York", "Manhattan, New York, United States"):
        assert config.location_in_scope(loc), loc


def test_long_island_counts_with_nyc_but_is_not_nyc():
    for loc in ("Melville, NY", "Garden City, New York", "Long Island, NY", "Hauppauge,NY"):
        assert config.location_in_scope(loc), loc
        assert config.LONG_ISLAND_RE.search(loc), loc
        assert not config.NYC_RE.search(loc.replace("New York", "")), loc
    # a town name alone is common elsewhere: it needs ", NY"
    for loc in ("Huntington, WV", "Hempstead, TX", "Medford, MA", "Freeport, Maine"):
        assert not config.location_in_scope(loc), loc
    assert not config.LONG_ISLAND_RE.search("New York, NY")


def test_other_us_cities_fail_even_with_country_suffix():
    for loc in (
        "San Francisco, California, United States",
        "Seattle, Washington, United States",
        "Austin, TX",
        "Hybrid in San Francisco, United States",
    ):
        assert not config.location_in_scope(loc), loc


def test_remote_us_passes():
    for loc in ("Remote US", "Remote, United States", "Remote - USA"):
        assert config.location_in_scope(loc), loc


def test_workday_country_codes_mark_remote_as_foreign():
    for loc in ("BEL - Remote", "GBR - Remote", "Remote - Belgium", "Brussels; Remote"):
        assert not config.location_in_scope(loc), loc
    for loc in ("USA - Remote", "Remote, can start anytime"):  # capitals only: prose "can" stays in
        assert config.location_in_scope(loc), loc


def test_summer_2027_and_2027_graduate_roles_are_in_scope_anywhere_in_the_us():
    for p in (
        {"title": "ML Intern", "start_date": "Summer 2027"},
        {"title": "2027 New Grad Software Engineer"},
        {"title": "Data Scientist", "description": "For students graduating in May 2027."},
    ):
        assert config.starts_in_wide_window(p), p
    for p in (
        {"title": "Data Scientist"},
        {"title": "Intern", "start_date": "Fall 2026"},
        {"title": "New Grad Engineer"},
        {"title": "Data Analyst Co-op"},
        {"title": "Sales Engineering Intern", "start_date": "Spring 2027"},
    ):
        assert not config.starts_in_wide_window(p), p
    for loc in ("San Francisco, CA", "Austin, TX", "Seattle", "Boston, Massachusetts"):
        assert config.location_in_scope(loc, wide=True), loc
        assert not config.location_in_scope(loc), loc
    for loc in ("London, UK", "Toronto, Canada", "BEL - Remote", ""):
        assert not config.location_in_scope(loc, wide=True), loc


def test_bare_remote_defaults_in():
    # ambiguity resolves toward recall; the work_mode belt catches lies
    assert config.location_in_scope("Remote")
    assert config.location_in_scope("San Francisco; Remote")


def test_foreign_only_plus_remote_fails():
    # the old Ashby bug: isRemote appended "Remote US" to foreign postings
    for loc in (
        "Tokyo, Japan; Remote",
        "Sydney, Australia; Remote",
        "London, UK; Remote",
        "Dublin, Ireland; Remote",
    ):
        assert not config.location_in_scope(loc), loc


def test_foreign_remote_variant_fails():
    assert not config.location_in_scope("Remote - Australia")


def test_empty_location_fails():
    assert not config.location_in_scope("")
    assert not config.location_in_scope(None or "")


# --- start_terms / starts_before_graduation ---


def test_season_start_terms():
    assert config.start_terms("fall 2026") == [(2026, 9)]
    assert config.start_terms("summer 2027") == [(2027, 6)]
    assert (2027, 6) in config.start_terms("2027 new grad")


def test_multi_term_uses_every_resolvable():
    terms = config.start_terms("Fall 2026, Winter 2027, Summer 2027")
    assert (2026, 9) in terms and (2027, 6) in terms


def test_pre_grad_full_time_start_excluded():
    assert config.starts_before_graduation("fall 2026", PROFILE)
    assert config.starts_before_graduation("january 2027", PROFILE)


def test_post_grad_and_ambiguous_starts_pass():
    assert not config.starts_before_graduation("summer 2027", PROFILE)
    assert not config.starts_before_graduation("2027 new grad", PROFILE)
    assert not config.starts_before_graduation(None, PROFILE)
    assert not config.starts_before_graduation("flexible", PROFILE)


def test_any_viable_term_keeps_the_posting():
    # one joinable cohort is enough
    assert not config.starts_before_graduation("Fall 2026, Summer 2027", PROFILE)


# --- senior-title carve-out ---


def test_member_of_technical_staff_not_senior():
    assert not config.is_senior_title("Member of Technical Staff")
    assert not config.is_senior_title("Member of Technical Staff - Research")


def test_staff_and_senior_titles_still_excluded():
    for t in (
        "Staff Engineer",
        "Senior Data Scientist",
        "Lead ML Engineer",
        "Senior Member of Technical Staff",
    ):
        assert config.is_senior_title(t), t


# --- check_exclusion belt: in-person outside NYC ---


def base_parsed(**over):
    d = {
        "title": "AI Engineer",
        "location": "Remote US",
        "employment_type": "full time",
        "start_date": None,
        "years_experience_min": None,
        "work_mode": "remote",
    }
    d.update(over)
    return d


def test_hybrid_outside_nyc_excluded():
    reason = check_exclusion(base_parsed(work_mode="hybrid", location="San Francisco; Remote"))
    assert reason and "in-person outside NYC" in reason


def test_hybrid_in_nyc_allowed():
    assert check_exclusion(base_parsed(work_mode="hybrid", location="New York, NY")) is None


def test_remote_work_mode_never_location_excluded():
    assert (
        check_exclusion(base_parsed(work_mode="remote", location="San Francisco; Remote")) is None
    )


def test_unknown_location_with_hybrid_not_excluded():
    # no location string -> cannot place it -> recall wins
    assert check_exclusion(base_parsed(work_mode="hybrid", location="")) is None


def test_full_time_pre_grad_start_excluded():
    reason = check_exclusion(base_parsed(start_date="fall 2026"), PROFILE)
    assert reason and "before graduation" in reason


def test_full_time_pre_grad_start_kept_without_profile():
    # no onboarded profile: the availability rule has nothing to compare to
    assert check_exclusion(base_parsed(start_date="fall 2026"), None) is None
    assert check_exclusion(base_parsed(start_date="fall 2026")) is None


def test_full_time_post_grad_start_allowed():
    assert check_exclusion(base_parsed(start_date="summer 2027"), PROFILE) is None


def test_explicit_may_availability_reaches_exclusion_policy():
    profile = {**PROFILE, "earliest_start_full_time": "2027-05"}
    assert check_exclusion(base_parsed(start_date="May 2027"), profile) is None
    assert "before availability" in check_exclusion(base_parsed(start_date="April 2027"), profile)
    assert check_exclusion(base_parsed(start_date="Summer 2027"), profile) is None


def test_internship_seasons_and_seniority_keep_existing_protections():
    profile = {**PROFILE, "earliest_start_full_time": "2027-05"}
    for season in ("Spring 2027", "Summer 2027"):
        assert (
            check_exclusion(base_parsed(employment_type="intern", start_date=season), profile)
            is None
        )
    assert check_exclusion(
        base_parsed(title="Senior Data Scientist", start_date="May 2027"), profile
    )


def test_intern_pre_grad_start_allowed():
    # the availability rule is full-time-only; internships during school are fine
    assert check_exclusion(base_parsed(employment_type="intern", start_date="fall 2026")) is None


# --- title triage ---


def make_client(replies):
    client = MagicMock()
    responses = []
    for text in replies:
        r = MagicMock()
        r.content = [MagicMock(type="text", text=text)]
        r.usage.cache_creation_input_tokens = 0
        r.usage.cache_read_input_tokens = 0
        r.stop_reason = "end_turn"
        r.usage.input_tokens = 100
        r.usage.output_tokens = 20
        responses.append(r)
    client.messages.create.side_effect = responses
    return client


TITLES = [
    "Deployment Strategist",
    "Account Executive",
    "Member of Technical Staff",
    "Payroll Specialist",
]


def test_triage_returns_matched_indices():
    client = make_client([json.dumps({"match": [1, 3]})])
    out = llm.triage_titles(client, TITLES, llm.Usage())
    assert out == [0, 2]
    assert client.messages.create.call_count == 1  # one batched call


def test_triage_ignores_out_of_range_and_dupes():
    client = make_client([json.dumps({"match": [1, 1, 99, 0, -2]})])
    assert llm.triage_titles(client, TITLES, llm.Usage()) == [0]


def test_triage_failure_admits_every_title():
    client = make_client(["not json", "still not json"])
    assert llm.triage_titles(client, TITLES, llm.Usage()) == list(range(len(TITLES)))


def test_triage_empty_input_no_call():
    client = make_client([])
    assert llm.triage_titles(client, [], llm.Usage()) == []
    assert client.messages.create.call_count == 0


# --- expanded ROLE_RE ---


def test_role_re_new_variants():
    for t in (
        "Member of Technical Staff",
        "Deployment Strategist",
        "Solutions Architect",
        "Implementation Engineer",
        "Customer Engineer",
        "Research Engineer",
        "Solutions Engineer",
        "Finance Analytics & Innovation Associate",
        "Insights Analyst",
        "AI Automation Intern",
        "Intelligent Automation Analyst",
        "AI Analyst",
    ):
        assert config.ROLE_RE.search(t), t


def test_role_re_still_rejects_unrelated():
    for t in (
        "Account Executive",
        "Payroll Specialist",
        "Office Manager",
        "Marketing Automation Manager",
    ):
        assert not config.ROLE_RE.search(t), t


@pytest.mark.parametrize(
    "description",
    [
        "You will need to be able to work out of our SF office during this time.",
        "You must work in our San Francisco office three days per week.",
        "Candidates are required to work from our Seattle office.",
        "Interns are expected to work at our Boston office.",
        "You will be required to work in Austin.",
        "This internship is onsite in San Francisco.",
        "This role will be hybrid in Seattle.",
        "This position requires working from London.",
        "This role requires relocation to Chicago.",
        "You must relocate to San Francisco.",
        "Applicants must live in California.",
        "Candidates must reside in Canada, even when working remotely.",
        "You must be based in London.",
        "Working from our SF office is required.",
        "Attendance at our San Francisco office is mandatory.",
        "You must work from SF and NYC offices.",
        "You must work from SF and NYC or remotely from Canada.",
        "You must work from SF or remotely from Canada.",
        "You must work from SF or remotely from California.",
        "You must work from SF or remote work is not permitted.",
        "You may work remotely. You must reside in California.",
        "You must work from SF. Our company also has a New York office.",
        "No prior experience is required, but you must work from Seattle.",
        "<p>You will need to be able to work out of our <strong>SF</strong> office.</p>",
        "&lt;p&gt;You must work in San Francisco.&lt;/p&gt;",
    ],
)
def test_explicit_description_location_overrides_remote_metadata(description):
    assert not config.location_in_scope("San Francisco; Remote", description)
    # Misleading NYC metadata cannot override an explicit incompatible requirement.
    assert not config.location_in_scope("New York, NY; Remote", description)


@pytest.mark.parametrize(
    "description",
    [
        "",
        "We are headquartered in San Francisco. This is a remote US internship.",
        "You can visit our San Francisco office if you like.",
        "You may work from SF or remotely.",
        "You must work from SF or remotely.",
        "You must work from SF or you may work remotely in the United States.",
        "You must work from our SF office or our NYC office.",
        "You must work from our New York or San Francisco office.",
        "You must work from our NYC office.",
        "This role is hybrid in New York with a team in San Francisco.",
        "You must work from our NYC office but may visit San Francisco.",
        "You must work from NYC and travel to SF occasionally.",
        "You must work from NYC during the program. We also have an SF office.",
        "Working from our San Francisco office is optional.",
        "Working from our SF office is not required.",
        "Working from SF is no longer required.",
        "You must work from SF, remote work is also available.",
        "You do not need to work from San Francisco.",
        "You don't need to work from San Francisco.",
        "You no longer need to work from San Francisco.",
        "You are not required to work from Seattle.",
        "For San Francisco employees, the salary range is $50 to $60 per hour.",
        "This is a remote US role. You must work in Python and SQL.",
        "You must work with our San Francisco team.",
    ],
)
def test_description_check_preserves_remote_and_nyc_options(description):
    assert config.location_in_scope("Remote US", description)
    assert config.location_in_scope("New York, NY", description)


def test_unfamiliar_location_wording_still_needs_review():
    assert config.location_in_scope("Remote", "You must work from Exampletown.")
    assert not config.location_in_scope("Seattle", "Remote work is allowed.")


def test_full_time_roles_anywhere_in_the_us_when_the_profile_says_so(tmp_path, monkeypatch):
    profile = tmp_path / "profile.json"
    monkeypatch.setattr(config, "PROFILE_PATH", profile)
    job = {"title": "Machine Learning Engineer", "employment_type": "full_time"}
    intern = {"title": "Machine Learning Intern", "start_date": "Fall 2026"}
    assert not config.starts_in_wide_window(job)  # no profile file
    profile.write_text('{"full_time_anywhere_us": true}')
    assert config.starts_in_wide_window(job)
    assert not config.starts_in_wide_window(intern)
    assert config.location_in_scope("Austin, TX", wide=config.starts_in_wide_window(job))
    assert not config.location_in_scope("London, UK", wide=config.starts_in_wide_window(job))


def test_a_graduate_program_counts_anywhere_in_the_us():
    for p in (
        {"title": "Technology Analyst Program", "location": "Sandy, UT"},
        {"title": "Management Development Program - Forward Deployed Product Analyst"},
        {"title": "Analyst", "description": "An 18-month program for recent graduates."},
        {"title": "Associate", "description": "A rotational program for recent graduates."},
    ):
        assert config.is_grad_program(p), p
    for p in (
        {"title": "Program Manager, Data"},
        {"title": "Program Analyst"},
        {"title": "Data Analyst", "description": "You will support our loyalty program."},
        {"title": "Program Intern", "employment_type": "intern"},
    ):
        assert not (config.is_full_time(p) and config.is_grad_program(p)), p


def test_an_undated_internship_seen_before_the_summer_is_a_summer_role():
    fall = date(2026, 10, 25)
    assert config.undated_intern({"title": "Sales Engineering Intern (Bellevue, WA)"}, fall)
    assert config.undated_intern({"title": "Intern - Business Intelligence"}, fall)
    for p in (
        {"title": "Data Analyst Co-Op"},
        {"title": "Operations Intern Spring 2027"},
        {"title": "Part-time Data Intern"},
        {"title": "Intern", "start_date": "Fall 2026"},
        {"title": "Data Analyst"},
    ):
        assert not config.undated_intern(p, fall), p
    spring = "This internship runs from January 25 through April 16 in our SF office."
    assert not config.undated_intern({"title": "Data Intern", "description": spring}, fall)
    old = "Join us for Summer 2026."
    assert not config.undated_intern({"title": "Data Intern", "description": old}, fall)
    assert not config.undated_intern({"title": "Sales Intern"}, date(2027, 7, 1))  # a later summer


def test_a_count_of_locations_is_in_scope_for_a_wide_role_only():
    for loc in ("20 Locations", "Multiple Locations", "3 locations"):
        assert config.location_in_scope(loc, wide=True), loc
        assert not config.location_in_scope(loc), loc
