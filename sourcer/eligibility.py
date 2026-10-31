"""Is a posting open to the candidate? One question, one function: ``eligible``.

Every rule reads explicit signals only: the title, the model's structured
reading of the posting, and the posting's own words when its text is at hand.
A rule that reads the text quotes it, so every drop can be checked by eye.
Ambiguity never drops a posting: an unstated start date or employment type is
flagged by ``pipeline.filters.timing_flag`` and scored as usual.

Two rule sets, one order:
- ``EXCLUSION_RULES`` run when a posting is scored. A level misfit is not one
  of them: it is graded by ``level_fit``, stored, and archived by the output
  node, so the board keeps the model's read of the role.
- ``BOARD_RULES`` add the board checks a stored row is held to (its location,
  and the level grade), for rescreening and for anything that decides
  whether to show or tailor a saved posting.

``RULES`` is the one map from a rule's code to the label a page shows.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from . import config
from .config import Profile

Posting = Mapping[str, Any]


@dataclass(frozen=True)
class Verdict:
    """``ok`` False names the ``rule`` that failed, the ``reason`` in plain
    words, and the posting's own words in ``quote`` when a rule read them."""

    ok: bool
    rule: str = ""
    reason: str = ""
    quote: str = ""


OPEN = Verdict(True)

RULES: dict[str, str] = {
    "senior_title": "Senior title",
    "seniority": "Above entry level",
    "years": "Years of experience",
    "immediate_full_time": "Immediate start",
    "contract": "Contract role",
    "in_person_outside_nyc": "In person outside NYC",
    "title_outside_nyc": "Title names another place",
    "school_program": "Another school's program",
    "degree_in_title": "Graduate degree program",
    "starts_before_graduation": "Starts before graduation",
    "internship_before_availability": "Internship before availability",
    "graduation_window": "Graduation date",
    "no_sponsorship": "Visa",
    "prior_firm_required": "Prior employer",
    "student_visa_barred": "Visa",
    "citizenship_required": "Citizenship",
    "clearance_required": "Clearance",
    "masters_required": "Degree",
    "phd_required": "Degree",
    "location_required": "Location in the text",
    "location": "Location",
    "level_misfit": "Level",
}


def _no(rule: str, reason: str, quote: str = "") -> Verdict:
    return Verdict(False, rule, reason, quote)


Rule = Callable[[Posting, Profile | None], Verdict | None]


def level_fit(parsed: Posting, profile: Profile | None) -> str:
    """fit | stretch | misfit against the candidate's education status.

    For a student (in_progress) graduating in grad_year:
    misfit: senior/mid seniority, years above max, or a new grad program
            for a class year BEFORE graduation (cannot start full time yet)
    fit:    internships, new grad programs for the graduation year or later,
            entry level roles asking <= 2 years
    stretch: everything ambiguous in between
    """
    if not profile:
        return "fit"
    sen = (parsed.get("seniority") or "unspecified").lower()
    yrs = parsed.get("years_experience_min")
    et = (parsed.get("employment_type") or "").lower()
    sd = (parsed.get("start_date") or "").lower()
    max_yrs = profile.get("max_years_experience", 2)
    grad_year = profile.get("grad_year")

    if sen in ("senior", "mid") or (yrs is not None and yrs > max_yrs):
        return "misfit"
    years_in_start = [int(y) for y in re.findall(r"20\d\d", sd)]
    if (
        profile.get("in_progress")
        and grad_year
        and "new grad" in sd
        and years_in_start
        and max(years_in_start) < grad_year
    ):
        return "misfit"  # class year program the candidate cannot join
    if et == "intern" or "intern" in sd or "new grad" in sd or et == "part time":
        return "fit"
    if config.experience_range_too_high(str(parsed.get("description") or "")):
        return "stretch"  # "1-3 years": open, but written for someone already working
    return "fit" if sen == "entry" and (yrs or 0) <= config.MAX_ENTRY_YEARS else "stretch"


# ---------- the rules, in the order they run ----------


def _rule_senior_title(parsed: Posting, profile: Profile | None) -> Verdict | None:
    del profile
    if config.is_senior_title(parsed["title"]):
        return _no("senior_title", f"senior-level title: {parsed['title']!r}")
    if config.MID_LEVEL_RE.search(parsed["title"]) or parsed.get("seniority") in {"mid", "senior"}:
        return _no("seniority", "seniority above entry level")
    return None


def _rule_years(parsed: Posting, profile: Profile | None) -> Verdict | None:
    # the model's reading, and the posting's own words when the text is at hand
    stated = config.explicit_experience_minimum(str(parsed.get("description") or ""))
    found = [y for y in (parsed.get("years_experience_min"), stated) if y is not None]
    yrs = max(found) if found else None
    limit = min(
        config.MAX_ENTRY_YEARS, (profile or {}).get("max_years_experience", config.MAX_ENTRY_YEARS)
    )
    if yrs is not None and yrs > limit:
        return _no("years", f"requires {yrs:g}+ years experience")
    return None


def _rule_immediate_full_time(parsed: Posting, profile: Profile | None) -> Verdict | None:
    del profile
    et = (parsed.get("employment_type") or "").lower()
    sd = (parsed.get("start_date") or "").lower()
    if et == "full time" and config.IMMEDIATE_START_RE.search(sd):
        return _no("immediate_full_time", "full time with immediate start")
    return None


FULL_TIME_TEXT_RE = re.compile(
    r"\bfull[\s-]?time\b|\b(?:40|forty)\s*(?:hours|hrs)(?:\s*(?:a|per|/)\s*week)?\b", re.I
)


def _rule_contract(parsed: Posting, profile: Profile | None) -> Verdict | None:
    """A contract role, unless it is full time and starts in the summer of the
    target year or later: that is a first job like any other."""
    del profile
    if (parsed.get("employment_type") or "").lower() != "contract":
        return None
    text = " ".join(str(parsed.get(k) or "") for k in ("title", "start_date", "description"))
    if FULL_TIME_TEXT_RE.search(text) and config.names_target_start(dict(parsed)):
        return None
    return _no("contract", "contract role")


def _rule_in_person_outside_nyc(parsed: Posting, profile: Profile | None) -> Verdict | None:
    """Belt check for the location prefilter, which sees only the board's
    location string. The parsed work_mode comes from the description: a
    posting that claimed remote but is hybrid or onsite somewhere other than
    NYC means commuting to another city."""
    del profile
    wm = (parsed.get("work_mode") or "").lower()
    loc = parsed.get("location") or ""
    if config.starts_in_wide_window(dict(parsed)) and (
        config.in_the_us(loc) or config.UNNAMED_PLACES_RE.search(loc)
    ):
        return None  # a summer 2027 or 2027 graduate role: anywhere in the US
    if wm in ("hybrid", "onsite") and loc and not config.TARGET_RE.search(loc):
        return _no("in_person_outside_nyc", f"in-person outside NYC ({wm}: {loc[:48]})")
    return None


def _rule_title_outside_nyc(parsed: Posting, profile: Profile | None) -> Verdict | None:
    del profile
    wide = config.starts_in_wide_window(dict(parsed))
    if config.title_location_conflict(parsed.get("title") or "", wide):
        return _no("title_outside_nyc", f"title scoped outside NYC: {parsed['title']!r}")
    return None


def _rule_school_program(parsed: Posting, profile: Profile | None) -> Verdict | None:
    if config.school_restricted(parsed.get("title") or "", (profile or {}).get("school")):
        return _no("school_program", f"program for another school: {parsed['title']!r}")
    return None


_GRAD_DEGREE_TITLE_RE = re.compile(r"\b(masters?|master'?s|mba|ph\.?\s?d|doctoral)\b", re.I)


def _rule_degree_in_title(parsed: Posting, profile: Profile | None) -> Verdict | None:
    """A title written for master's, MBA or PhD students ("Campus Graduate
    Masters Engineer"), when the candidate has no graduate degree."""
    education = str((profile or {}).get("education") or "").lower()
    if not profile or any(d in education for d in ("master", "mba", "phd", "doctor")):
        return None
    if _GRAD_DEGREE_TITLE_RE.search(parsed.get("title") or ""):
        return _no("degree_in_title", f"title is for graduate degree students: {parsed['title']!r}")
    return None


def _rule_starts_before_graduation(parsed: Posting, profile: Profile | None) -> Verdict | None:
    """Respect explicit full time availability, falling back to graduation.
    Only explicit start dates exclude; unstated stays flagged timing unclear."""
    et = (parsed.get("employment_type") or "").lower()
    sd = (parsed.get("start_date") or "").lower()
    if et == "full time" and config.starts_before_graduation(sd, profile):
        boundary = (
            "availability" if profile and profile.get("earliest_start_full_time") else "graduation"
        )
        return _no(
            "starts_before_graduation",
            f"full-time start before {boundary} ({parsed.get('start_date')})",
        )
    return None


def _rule_internship_before_availability(
    parsed: Posting, profile: Profile | None
) -> Verdict | None:
    """An internship that starts, by its own start date or title, before the
    profile's ``earliest_start_internship`` (YYYY-MM): a winter or spring term
    she cannot take while in school. Unstated dates stay eligible."""
    available = (profile or {}).get("earliest_start_internship")
    if not available or config.is_full_time(dict(parsed)):
        return None
    # the title and the stated start date together: either may carry the term
    when = " ; ".join(str(parsed.get(k)) for k in ("start_date", "title") if parsed.get(k))
    if config.starts_before(when, str(available)):
        return _no(
            "internship_before_availability", f"internship starts before availability ({when[:60]})"
        )
    return None


def _rule_graduation_window(parsed: Posting, profile: Profile | None) -> Verdict | None:
    """A stated graduation range that does not include the candidate's date."""
    reason = config.outside_graduation_window(str(parsed.get("description") or ""), profile)
    return _no("graduation_window", reason) if reason else None


def _full_time(parsed: Posting) -> bool:
    """A full time role: said so, or not an internship by its title."""
    kind = str(parsed.get("employment_type") or "").lower().replace("_", " ").replace("-", " ")
    if kind:
        return kind == "full time"
    return not re.search(r"\bintern(?:ship)?s?\b|\bco-?op\b", str(parsed.get("title") or ""), re.I)


def _rule_eligibility(parsed: Posting, profile: Profile | None) -> Verdict | None:
    """Read mandatory eligibility from the original text, never the model summary."""
    description = str(parsed.get("description") or "")
    found = config.eligibility_evidence(description, profile, str(parsed.get("title") or ""))
    if found:
        return _no(found[0], f'{found[0]}: "{found[1]}"', found[1])
    if (profile or {}).get("full_time_needs_future_sponsorship") is True and _full_time(parsed):
        # a full time role that will not sponsor ends when the student visa's work time does
        for line, optional in config.requirement_lines(description):
            if (
                not optional
                and config._NO_SPONSOR_RE.search(line)
                and not config.welcomes_opt(description)
            ):
                quote = config._quote(line)
                return _no("no_sponsorship", f'no_sponsorship: "{quote}"', quote)
    wide = config.starts_in_wide_window(dict(parsed))
    quote = config.description_location_conflict(description, wide)
    if quote:
        return _no("location_required", f'location required outside scope: "{quote}"', quote)
    return None


def _rule_location(parsed: Posting, profile: Profile | None) -> Verdict | None:
    """The stored location, for a row already on the board."""
    del profile
    loc = parsed.get("location") or ""
    if not config.location_in_scope(loc, wide=config.starts_in_wide_window(dict(parsed))):
        return _no("location", f"location out of scope ({loc[:48]})")
    return None


def _rule_level(parsed: Posting, profile: Profile | None) -> Verdict | None:
    if profile and level_fit(parsed, profile) == "misfit":
        return _no("level_misfit", "level misfit for profile")
    return None


EXCLUSION_RULES: tuple[Rule, ...] = (
    _rule_senior_title,
    _rule_years,
    _rule_immediate_full_time,
    _rule_contract,
    _rule_in_person_outside_nyc,
    _rule_title_outside_nyc,
    _rule_school_program,
    _rule_degree_in_title,
    _rule_starts_before_graduation,
    _rule_internship_before_availability,
    _rule_graduation_window,
    _rule_eligibility,
)
BOARD_RULES: tuple[Rule, ...] = (*EXCLUSION_RULES, _rule_location, _rule_level)


def eligible(
    posting: Posting, profile: Profile | None = None, rules: tuple[Rule, ...] = BOARD_RULES
) -> Verdict:
    """The first rule the posting fails, or ``OPEN``. ``profile`` enables the
    rules about the candidate (graduation, availability, visa, level); without one they
    are inert, so callers that have a profile must pass it."""
    for rule in rules:
        verdict = rule(posting, profile)
        if verdict is not None:
            return verdict
    return OPEN


def check_exclusion(parsed: Posting, profile: Profile | None = None) -> str | None:
    """The scoring time exclusion reason, or None. Kept for callers that still import it."""
    return eligible(parsed, profile, EXCLUSION_RULES).reason or None
