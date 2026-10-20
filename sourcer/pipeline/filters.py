"""Pipeline policy: the timing flag here, and the eligibility rules from
``sourcer.eligibility`` (re-exported so pipeline code imports one place).

Exclusions read explicit signals only and score the posting 0 with the
reason logged. Ambiguous employment_type or start_date never excludes: the
posting is scored normally on skill match, with ``timing_unclear`` set and
what is missing noted.
"""

from __future__ import annotations

from ..eligibility import (
    EXCLUSION_RULES,
    Rule,
    _rule_contract,
    _rule_degree_in_title,
    _rule_eligibility,
    _rule_graduation_window,
    _rule_immediate_full_time,
    _rule_in_person_outside_nyc,
    _rule_internship_before_availability,
    _rule_school_program,
    _rule_senior_title,
    _rule_starts_before_graduation,
    _rule_title_outside_nyc,
    _rule_years,
    check_exclusion,
    eligible,
    level_fit,
)
from .state import Posting

ExclusionRule = Rule

__all__ = [
    "EXCLUSION_RULES",
    "ExclusionRule",
    "_rule_contract",
    "_rule_degree_in_title",
    "_rule_eligibility",
    "_rule_graduation_window",
    "_rule_immediate_full_time",
    "_rule_in_person_outside_nyc",
    "_rule_internship_before_availability",
    "_rule_school_program",
    "_rule_senior_title",
    "_rule_starts_before_graduation",
    "_rule_title_outside_nyc",
    "_rule_years",
    "check_exclusion",
    "eligible",
    "level_fit",
    "timing_flag",
]


def timing_flag(parsed: Posting) -> tuple[bool, str]:
    """Returns (timing_unclear, note) for postings that are not excluded."""
    et = (parsed.get("employment_type") or "unspecified").lower()
    sd = parsed.get("start_date")
    missing = []
    if et in ("unspecified", ""):
        missing.append("employment type not stated")
    if not sd and et not in ("intern", "part time"):
        missing.append("start date not stated")
    if missing:
        return True, "; ".join(missing)
    return False, ""
