"""Central configuration and the deterministic policy rules.

Settings are loaded once at import time. The project .env supplies defaults;
existing process environment variables take precedence.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime
from html import unescape
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Inputs and exports resolve relative to the project root: the checkout when
# running from source, or SOURCER_ROOT for an installed package or a container.
PROJECT_ROOT = Path(os.environ.get("SOURCER_ROOT", Path(__file__).resolve().parent.parent))
load_dotenv(PROJECT_ROOT / ".env", override=False)
DB_PATH = Path(os.environ.get("SOURCER_DB", PROJECT_ROOT / "data" / "sourcer.db"))
COMPANIES_PATH = Path(os.environ.get("SOURCER_COMPANIES", PROJECT_ROOT / "companies.json"))
SKILLS_PATH = Path(os.environ.get("SOURCER_SKILLS", PROJECT_ROOT / "skills_vector.json"))
PROFILE_PATH = Path(os.environ.get("SOURCER_PROFILE", PROJECT_ROOT / "profile.json"))
# The candidate's approved facts as plain text (one fact per line, grouped by
# job or project). When present, fit is judged against it instead of the
# weighted skills list. Written by the operator's own tooling; optional.
EVIDENCE_PATH = Path(os.environ.get("SOURCER_EVIDENCE", PROJECT_ROOT / "data" / "evidence.txt"))
# A ceiling, not a budget: the whole file should fit. Facts past it are never
# seen by scoring, so it sits well above a full evidence bank (about 16k chars).
EVIDENCE_MAX_CHARS = int(os.environ.get("SOURCER_EVIDENCE_MAX_CHARS", "40000"))
# Score fit with checks against the evidence file (1) or with the weighted
# skills list (unset). Off by default until an operator turns it on.
EVIDENCE_SCORING = os.environ.get("SOURCER_EVIDENCE_SCORING", "") == "1"
# The model for evidence fit checks; empty means MODEL.
SCORE_MODEL = os.environ.get("SOURCER_SCORE_MODEL", "")
# Fit scores in this range get a second check and keep the lower score.
STEADY_BAND = (
    int(os.environ.get("SOURCER_STEADY_LOW", "70")),
    int(os.environ.get("SOURCER_STEADY_HIGH", "90")),
)


def load_evidence() -> str:
    """The candidate's evidence text, or "" when there is none."""
    try:
        return EVIDENCE_PATH.read_text().strip()[:EVIDENCE_MAX_CHARS]
    except OSError:
        return ""


def scoring_evidence() -> str:
    """The evidence scoring uses: empty unless evidence scoring is on."""
    return load_evidence() if EVIDENCE_SCORING else ""


MODEL = os.environ.get("SOURCER_MODEL", "claude-haiku-4-5-20251001")
# The worker that only reads a posting's facts (dates, place, level), each
# with the posting's own words; code checks the words before any rule uses them.
EXTRACT_MODEL = os.environ.get("SOURCER_EXTRACT_MODEL", "claude-haiku-5-5")
# "api" (Messages API, needs ANTHROPIC_API_KEY) or "claude-code" (the claude
# CLI on the operator's subscription login; single operator, no batches).
LLM_BACKEND = os.environ.get("SOURCER_LLM", "api")
LOG_LEVEL = os.environ.get("SOURCER_LOG_LEVEL", "INFO")
DRY_RUN = os.environ.get("SOURCER_DRY_RUN", "1").strip().lower() not in {"0", "false", "no"}
TELEGRAM_TOKEN = os.environ.get("SOURCER_TELEGRAM_TOKEN", "").strip()


def _integer_allowlist(name: str) -> frozenset[int]:
    values = os.environ.get(name, "")
    try:
        return frozenset(int(value.strip()) for value in values.split(",") if value.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must contain comma-separated integer IDs") from exc


TELEGRAM_ALLOWED_CHAT_IDS = _integer_allowlist("SOURCER_TELEGRAM_ALLOWED_CHAT_IDS")
TELEGRAM_ALLOWED_USER_IDS = _integer_allowlist("SOURCER_TELEGRAM_ALLOWED_USER_IDS")
TELEGRAM_SCORE_THRESHOLD = int(os.environ.get("SOURCER_TELEGRAM_SCORE_THRESHOLD", "75"))

# Haiku 4.5 pricing, USD per million tokens. Overridable if pricing changes.
PRICE_IN_PER_MTOK = float(os.environ.get("SOURCER_PRICE_IN", "1.00"))
PRICE_OUT_PER_MTOK = float(os.environ.get("SOURCER_PRICE_OUT", "5.00"))

# Prompt caching only engages above a per-model minimum prefix length; below it
# the API silently declines to cache (cache_creation_input_tokens stays 0, no
# error). Haiku 4.5's minimum is 4096 tokens. We gate the cache_control marker
# on a conservative character estimate so a prefix that cannot cache is not
# marked as if it does; grow the system prompt past this and it turns itself on.
CACHE_MIN_TOKENS = int(os.environ.get("SOURCER_CACHE_MIN_TOKENS", "4096"))
CACHE_MIN_CHARS = CACHE_MIN_TOKENS * 4  # ~4 chars/token, deliberately conservative

REQUEST_TIMEOUT = 20
# The one freshness window, in days: see is_fresh.
FRESHNESS_DAYS = int(os.environ.get("SOURCER_FRESHNESS_DAYS", "30"))
# Admission limit on estimated spend per run or scoring request. A call already
# in flight may exceed the estimate; provider billing limits are separate.
COST_CAP_USD = float(os.environ.get("SOURCER_COST_CAP", "2.50"))
# Admission limit on wall time: no new model call once the process has run this
# many minutes, so a large run still finishes and saves what it scored. Unset
# means no limit; the cloud run sets it to stay inside the job timeout.
RUN_MINUTES = float(os.environ.get("SOURCER_RUN_MINUTES", "inf"))
# Community listings scored per run after collection, inside the same cost cap.
LISTING_SCORE_LIMIT = int(os.environ.get("SOURCER_LISTING_SCORE_LIMIT", "10"))
MAX_DESCRIPTION_CHARS = 8000  # truncate very long postings before sending to the LLM

# Pre-filter applied in the source node so we only pay LLM cost for postings
# that are plausibly in scope. Title must look like a target role AND the
# location must pass location_in_scope. Everything filtered here never enters
# the pipeline and is counted in run_logs as prefiltered.
ROLE_RE = re.compile(
    r"(ai engineer|machine learning|ml engineer|ml researcher|applied ai|applied scientist"
    r"|data scien|data analy|analytics engineer|data engineer|business intelligen"
    r"|forward deployed|ai solutions|solutions engineer"
    r"|member of technical staff|deployment strategist|solutions architect"
    r"|implementation engineer|customer engineer|research engineer|ai specialist"
    r"|engineer,\s*ai"
    # product
    r"|product manag|product analyst|product operations|product associate|associate product"
    r"|\bapm\b|technical product"
    # strategy and operations
    r"|business operations|bizops|biz ops|strategy (?:&|and) operations|strategy analyst"
    r"|operations analyst|business analyst|growth (?:analyst|associate|operations)"
    r"|chief of staff|rotational"
    # solutions and consulting
    r"|sales engineer|solutions consultant|technical consultant|customer success engineer"
    r"|implementation (?:specialist|consultant|analyst)|technical account"
    # analytics and AI automation under other names
    r"|\banalytics\b|insights analyst|reporting analyst|decision scien"
    r"|\bai analyst|ai automation|automation (?:analyst|engineer|specialist|developer)"
    r"|intelligent automation|process automation|\brpa\b|ai operations|ai strategy)",
    re.I,
)

TARGET_WINDOW_RE = re.compile(
    r"\b(intern(?:ship)?s?|new[ -]?grad(?:uate)?s?|entry[ -]?level|early[ -]?career"
    r"|junior|graduate\s+(?:program|programme)|campus\s+(?:hire|recruit))\b",
    re.I,
)
MAX_ENTRY_YEARS = 2
_YEAR_WORDS = {
    word: value
    for value, word in enumerate(
        ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")
    )
}
_YEAR_NUMBER = r"\d+(?:\.\d+)?|zero|one|two|three|four|five|six|seven|eight|nine|ten"
EXPERIENCE_RE = re.compile(
    rf"\b(?P<minimum>{_YEAR_NUMBER})(?:\s*(?:-|to)\s*(?P<top>{_YEAR_NUMBER}))?\s*\+?\s*years?['\u2019]?\s+"
    # after "of", any few words may name the field: "3+ years of professional software
    # engineering experience"; without "of", only the usual qualifiers
    r"(?:of\s+(?:[\w/&.+#,\u2010\u2011-]+\s+){0,6}?"
    r"|(?:(?:relevant|professional|industry|work|related|practical|hands-on|closing|selling)\s+){0,3})"
    r"experience\b"
    # a "+" or a range marks a requirement even without the word experience:
    # "5+ years in product management", "3 to 6+ years administering DocuSign"
    rf"|\b(?P<stated>{_YEAR_NUMBER})\s*(?:\+|(?:-|to)\s*(?P<stated_top>{_YEAR_NUMBER})\s*\+?)\s*years?['\u2019]?\s+"
    r"(?:in|building|selling|closing|administering|working|leading|managing|shipping"
    r"|developing|designing|running|owning)\b",
    re.I,
)
MID_LEVEL_RE = re.compile(r"\bmid[ -]?(?:level|senior)\b", re.I)


def requirement_lines(description: str) -> Iterator[tuple[str, bool]]:
    """Each requirement-sized line of a description with whether it is
    optional (a preferred or nice-to-have section, or an optional line with
    no mandatory wording). Shared by every rule that reads requirements."""
    text = unescape(description).replace("\u2013", "-").replace("\u2014", "-")
    text = re.sub(r"</?(?:p|li|div|br)\b[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]*>", " ", text)
    # "U.S." must not end a sentence for the splitter below
    text = re.sub(r"\bU\.S\.(?:A\.)?", "US", text, flags=re.I)
    # nor may a degree: "a M.S. degree in Statistics", "Ph.D. required"
    text = re.sub(r"\b([BM])\.(S|A|Sc)\.", r"\1\2", text)
    text = re.sub(r"\bPh\.\s?D\.", "PhD", text, flags=re.I)
    optional_section = False
    for line in re.split(r"[;\n]|(?<=[.!?])\s+", text):
        if re.fullmatch(r"\s*(?:preferred qualifications|nice to have|bonus)\s*:?\s*", line, re.I):
            optional_section = True
        elif re.fullmatch(
            r"\s*(?:requirements|required qualifications|what you need|what you.ll bring)\s*:?\s*",
            line,
            re.I,
        ):
            optional_section = False
        optional_line = _OPTIONAL_WORD_RE.search(line)
        mandatory_line = re.search(
            r"\b(required|must|minimum|at least)\b",
            re.sub(r"\bnot required\b", "", line, flags=re.I),
            re.I,
        )
        yield line, bool((optional_section or optional_line) and not mandatory_line)


_OPTIONAL_WORD_RE = re.compile(r"\b(preferred|nice.to.have|bonus|not required|ideally)\b", re.I)


def _optional_after(line: str, position: int) -> bool:
    """The line is optional only through an "ideally" after ``position``:
    "7+ years of sales experience, ideally in SaaS" still requires the seven
    years, while "4 years of experience preferred" stays optional."""
    words = list(_OPTIONAL_WORD_RE.finditer(line))
    return bool(words) and all(
        w.group(0).lower() == "ideally" and w.start() > position for w in words
    )


def _years(token: str) -> float:
    token = token.lower()
    return float(_YEAR_WORDS[token]) if token in _YEAR_WORDS else float(token)


def experience_range_too_high(description: str) -> str | None:
    """A required range that starts at a year or more and runs past
    MAX_ENTRY_YEARS ("1-3 years of experience"): a role for someone already
    working: kept as a stretch and scored lower, not dropped.
    "0-3 years" and "1-2 years" are entry level. Returns the range as read
    ("1 to 3"), or None."""
    for line, optional in requirement_lines(description):
        for match in EXPERIENCE_RE.finditer(line):
            if optional and not _optional_after(line, match.end()):
                continue
            low, top = match["minimum"] or match["stated"], match["top"] or match["stated_top"]
            if top and _years(low) >= 1 and MAX_ENTRY_YEARS < _years(top) <= 15:
                return f"{_years(low):g} to {_years(top):g}"
    return None


def explicit_experience_minimum(description: str) -> float | None:
    """Read common explicit experience requirements without a model.

    Optional experience is not a hard requirement. Education alternatives on
    one line use their lowest stated minimum; other requirements accumulate.
    This is a bounded prefilter, not a complete job-description parser.
    """
    requirements = []
    for line, optional in requirement_lines(description):
        values = []
        for match in EXPERIENCE_RE.finditer(line):
            if optional and not _optional_after(line, match.end()):
                continue
            years = _years(match["minimum"] or match["stated"])
            if years <= 15:  # "30+ years of experience serving clients" is the company's history
                values.append(years)
        if values:
            # A degree alternative is not an additional experience requirement.
            alternatives = re.search(r"\bor\b", line, re.I) and re.search(
                r"bachelor|master|ph\.?d", line, re.I
            )
            requirements.append(min(values) if alternatives else max(values))
        elif re.search(
            r"\bno\s+(?:prior\s+|professional\s+|work\s+)*experience\s+(?:is\s+)?(?:required|necessary)\b",
            line,
            re.I,
        ):
            requirements.append(0.0)
    return max(requirements) if requirements else None


# Explicit, mandatory eligibility requirements read from the description.
# Citizenship and sponsorship rules act only when the profile says so
# ("us_citizen": false, "needs_sponsorship": true, "student_visa": true);
# unset means no opinion.
_PHD_RE = re.compile(r"\b(ph\.?\s?d|doctorate|doctoral degree)\b", re.I)
_MASTERS_RE = re.compile(
    r"\b(master['\u2019]?s|m\.s\.|msc|graduate degree)\b"
    r"|(?-i:\bMS\b)(?=\s+(?:degree|in|or)\b)",  # "MS Excel" is not a degree
    re.I,
)
_BACHELOR_RE = re.compile(
    r"\b(bachelor['\u2019]?s|b\.s\.|b\.a\.|undergraduate|or equivalent)\b|(?-i:\bB[SA]\b)", re.I
)
# "required", or a line written as the requirement itself: "Requirements:
# Recently graduated with a M.S. degree", "must hold a master's"
_DEGREE_REQUIRED_RE = re.compile(
    r"\b(required|must have|requires?)\b|^\W*(?:requirements|minimum qualifications)\s*:"
    r"|\b(?:graduated|graduate) with\b|\bmust (?:hold|possess|be pursuing)\b",
    re.I,
)
_CLEARANCE_RE = re.compile(
    r"\b(?:active|current)\s+(?:secret|top secret|ts/sci|ts|security)\b[^.]{0,20}\bclearance\b"
    r"|\b(?:secret|top secret|ts/sci|security) clearance (?:is )?required\b"
    # "Active Full Scope Poly Level clearance", "TS/SCI with CI poly"
    r"|\b(?:active|current)\s+(?:full[ -]scope|ci|counterintelligence)\s+poly(?:graph)?\b"
    r"[^.\n]{0,20}\bclearance\b|\b(?:full[ -]scope|ci) poly(?:graph)?\b",
    re.I,
)
_CITIZEN_RE = re.compile(
    r"\b(?:u\.?s\.?|united states) citizen(?:ship)?\b|\bu\.?\s?s\.?\s?persons?\b"
    r"|\bability to obtain (?:a )?(?:security )?clearance\b",
    re.I,
)
# Up to four words between the negation and "sponsor": "do not anticipate
# providing visa sponsorship", "not eligible for employment visa sponsorship".
_NO_SPONSOR_RE = re.compile(
    r"\b(?:not|unable to|cannot|can't|will not|won't|does not|do not)\s+"
    r"(?:[\w-]+\s+){0,4}sponsor(?:ship|ing|ed)?\b|\bno (?:visa )?sponsorship\b"
    # "authorization to work in the US without sponsorship now or in the future",
    # "without the need for current or future visa sponsorship"
    r"|\bwithout\s+(?:the\s+need\s+for\s+|requiring\s+)?(?:\w+\s+){0,4}sponsorship\b"
    r"|\bsponsorship\s+(?:is|will)\s+not\s+(?:be\s+)?(?:offered|provided|available)\b",
    re.I,
)
# "Not eligible: F-1 (including OPT or CPT) ... now or in the future": a
# posting that names student visas among those it will not hire. Saying
# nothing about sponsorship is fine; only this explicit wording rules out a
# candidate whose profile sets student_visa.
_VISA = r"(?-i:\bF-?1\b|\bOPT\b|\bCPT\b)"
_FUTURE = r"\b(?:now or in the future|current or future)\b"
_STUDENT_VISA_BARRED_RE = re.compile(
    rf"sponsor[^\n]{{0,400}}?{_FUTURE}[^\n]{{0,600}}?{_VISA}|{_VISA}[^\n]{{0,600}}?{_FUTURE}"
    # "F-1 visa holders with OPT who will require H-1B status ... will not be considered"
    rf"|{_VISA}[^\n]{{0,300}}?\b"
    r"(?:will not be considered|are not eligible|is not eligible|ineligible)\b",
    re.I,
)
# For a candidate on OPT, OPT covers the start, so "we do not sponsor" or "must
# be authorized to work in the US" is fine. A posting that will not sponsor later (H-1B after
# OPT) is not: "unable to sponsor now or in the future", "will not sponsor H-1B".
_LATER_RE = re.compile(r"\bfuture\b|\bH-?1-?B\b", re.I)

# "1 to 3 years at a top consulting, private equity, or banking firm": years at a
# kind of employer, which a profile without that background cannot meet
PRIOR_FIRM_RE = re.compile(
    r"\b\d+\s*(?:\+|to|-)?\s*\d*\s*\+?\s*years?\b[^.\n]{0,40}?\bat an?\s+"
    r"(?:top|leading|elite|tier[- ]?(?:1|one)|top[- ]tier|bulge[- ]bracket|premier)\b[^.\n]{0,40}?"
    r"\b(?:consulting|private equity|banking|investment bank\w*|hedge fund|venture capital|strategy)\b",
    re.I,
)


# "Open to candidates with OPT", "OPT/CPT candidates welcome": the employer
# takes candidates on OPT, which outweighs a general "no sponsorship now or later"
_OPT_WELCOME_RE = re.compile(
    r"\bopen to (?:candidates|applicants|students)?\s*(?:with|on|holding)?\s*(?:OPT|CPT|F-?1)\b"
    r"|\b(?:OPT|CPT|F-?1)(?:\s*/\s*(?:OPT|CPT))?\s+(?:candidates|students|holders)?\s*"
    r"(?:are\s+)?(?:welcome|encouraged to apply)"
    r"|\b(?:we|will)\s+(?:accept|consider)\s+(?:candidates\s+(?:on|with)\s+)?(?:OPT|CPT|F-?1)\b",
    re.I,
)


def welcomes_opt(description: str) -> bool:
    return bool(_OPT_WELCOME_RE.search(description))


def future_sponsorship_bar(description: str) -> str | None:
    """The sentence that declines sponsorship now or later, else None."""
    for sentence in re.split(r"(?<=[.!?;])\s+|\n", description):
        if _NO_SPONSOR_RE.search(sentence) and _LATER_RE.search(sentence):
            return sentence.strip()
    return None


_STUDENT_VISA_EXPLICIT_RE = re.compile(
    rf"{_VISA}[^\n]{{0,300}}?\b"
    r"(?:will not be considered|are not eligible|is not eligible|ineligible)\b",
    re.I,
)
# Task based gig work sold as jobs: "Chemistry - AI Training - Phoenix",
# "Data AI Annotator - Flexible Hours". A company's own staff roles stay in.
GIG_TITLE_RE = re.compile(
    r"\bAI\s+(?:trainer|tutor)s?\b|[-\u2013|]\s*AI\s+training\s*(?:$|[-\u2013|(,])"
    r"|\bannotator\b|\bflexible hours\b|\btask[- ]based\b"
    r"|\bfreelance\b|\bdata contributor\b",
    re.I,
)
STAFFING_AGENCY_RE = re.compile(
    r"\b(jobot|cybercoders|robert half|insight global|teksystems|randstad|kforce"
    r"|aerotek|apex systems|motion recruitment|dice|hired by|staffing|recruiting agency)\b",
    re.I,
)


def _status_conflict(line: str, profile: dict[str, Any] | None) -> str | None:
    """Clearance, sponsorship, citizenship, or a graduation range she is outside."""
    if _CLEARANCE_RE.search(line) and not (profile or {}).get("has_clearance"):
        return "clearance_required"
    if _NO_SPONSOR_RE.search(line) and (profile or {}).get("needs_sponsorship") is True:
        return "no_sponsorship"
    # "I am a US Citizen or Legal Permanent Resident." is an application form
    # answer copied onto the page, not a requirement
    first_person = re.match(r"\s*I\s+(?:am|have|will)\b", line)
    if _CITIZEN_RE.search(line) and not first_person and (profile or {}).get("us_citizen") is False:
        return "citizenship_required"
    if outside_graduation_window(line, profile):
        return "graduation_window"
    return None


def eligibility_conflict(description: str, profile: dict[str, Any] | None) -> str | None:
    """Reason code when a mandatory line rules the candidate out, else None.
    Kept for callers that still import it; ``eligibility.eligible`` is the rule."""
    found = eligibility_evidence(description, profile)
    return found[0] if found else None


# A title on a US government track: these teams hire US citizens who can hold
# a clearance, and the boards often cut that line from the stored text.
GOVERNMENT_TRACK_RE = re.compile(r"\b(?:U\.?S\.? Government|USG)\b", re.I)


def eligibility_evidence(
    description: str, profile: dict[str, Any] | None, title: str = ""
) -> tuple[str, str] | None:
    """(reason code, the posting's own words that triggered it), else None.
    The quote goes into the trace, so every drop can be checked by eye."""
    not_citizen = (profile or {}).get("us_citizen") is False or (profile or {}).get(
        "needs_sponsorship"
    ) is True
    if not_citizen and GOVERNMENT_TRACK_RE.search(title or ""):
        # the posting's own requirement line when it has one (a clearance, a
        # citizenship line); the title alone only when the text says nothing
        found = eligibility_evidence(description, profile)
        return found or (
            "citizenship_required",
            _quote(f"the title names a US government team, which hires US citizens: {title}"),
        )
    education = str((profile or {}).get("education") or "").lower()
    has_grad_degree = "master" in education or "phd" in education or "doctor" in education
    if (profile or {}).get("student_visa") is True:
        barred = _STUDENT_VISA_BARRED_RE.search(description or "")
        if barred and welcomes_opt(description or ""):
            # it takes OPT: only a line that names students as not eligible still counts
            barred = _STUDENT_VISA_EXPLICIT_RE.search(description or "")
        # needing sponsorship now, any "no sponsorship" line is the rule (below)
        future = (
            (profile or {}).get("needs_sponsorship") is not True
            and not welcomes_opt(description or "")
            and future_sponsorship_bar(description or "")
        )
        if barred or future:
            return "student_visa_barred", _quote(barred.group(0) if barred else str(future))
    for line, optional in requirement_lines(description or ""):
        if optional:
            continue
        if (profile or {}).get("top_firm_experience") is False and PRIOR_FIRM_RE.search(line):
            return "prior_firm_required", _quote(line)
        status = _status_conflict(line, profile)
        if status:
            return status, _quote(line)
        if (
            profile
            and not has_grad_degree
            and _DEGREE_REQUIRED_RE.search(line)
            and not _BACHELOR_RE.search(line)
        ):
            if _PHD_RE.search(line):
                return "phd_required", _quote(line)
            if _MASTERS_RE.search(line):
                return "masters_required", _quote(line)
    return None


def _quote(text: str, limit: int = 240) -> str:
    """A trace-sized excerpt: whitespace collapsed, cut at ``limit``."""
    return " ".join(text.split())[:limit]


def target_window_evidence(posting: dict[str, Any]) -> str:
    """ "yes" (student or entry evidence), "no" (senior title or too many years
    required), or "unknown" (no signal either way; the parse step reads the
    requirements and the exclusion rules decide)."""
    title = str(posting.get("title") or "")
    if is_senior_title(title) or MID_LEVEL_RE.search(title):
        return "no"
    years = explicit_experience_minimum(str(posting.get("description") or ""))
    structured_years = posting.get("years_experience_min")
    if isinstance(structured_years, (int, float)) and not isinstance(structured_years, bool):
        years = max(years or 0, structured_years)
    if years is not None:
        return "no" if years > MAX_ENTRY_YEARS else "yes"
    text = " ".join(
        str(posting.get(key) or "")
        for key in ("title", "employment_type", "start_date", "description")
    )
    return "yes" if TARGET_WINDOW_RE.search(text) else "unknown"


def utc_now() -> datetime:
    """The clock for freshness decisions. Tests pin it so fixtures never age out."""
    return datetime.now(UTC)


MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
# a program term a posting names: "Summer 2026", "June 2026", "autumn 2026"
TERM_RE = re.compile(rf"\b(?:(summer|fall|autumn|spring|winter)|({MONTHS}))\s+(20\d\d)\b", re.I)
TERM_END = {"spring": 5, "summer": 8, "fall": 12, "autumn": 12, "winter": 2}


def program_ended(text: str, today: date | None = None) -> str | None:
    """The latest program term a text names, when it is already over:
    "Duration: June to August 2026" read in October 2026 -> "8/2026"."""
    today = today or utc_now().date()
    ends = []
    for season, month, year in TERM_RE.findall(text or ""):
        end = TERM_END[season.lower()] if season else MONTHS.split("|").index(month.lower()) + 1
        ends.append((int(year), end))
    if ends and max(ends) < (today.year, today.month):
        y, m = max(ends)
        return f"{m}/{y}"
    return None


# A paging bound only, not a freshness rule: paged boards (Workday, Eightfold,
# Oracle) stop reading roles posted more than this many days ago, so a 2,000
# job board costs a bounded number of requests. Whether a role is fresh enough
# to show is is_fresh's call.
LISTING_SCAN_DAYS = int(os.environ.get("SOURCER_LISTING_SCAN_DAYS", "365"))


def posting_age_days(posted_at: str | None, now: datetime | None = None) -> int | None:
    """Whole days since the posting date, or None when there is no usable date."""
    if not posted_at:
        return None
    try:
        posted = datetime.fromisoformat(str(posted_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    return ((now or utc_now()).date() - posted.date()).days


def age_penalty(posted_at: str | None, now: datetime | None = None) -> int:
    """Points taken off a role's rank for its age: none inside the freshness
    window, then 3 plus 1 for each further month, at most 10. An old role still
    shows, below a fresh one of the same fit."""
    age = posting_age_days(posted_at, now)
    if age is None or age <= FRESHNESS_DAYS:
        return 0
    return min(10, 3 + (age - FRESHNESS_DAYS) // 30)


def is_fresh(
    posted_at: str | None, *, listed_on_board: bool = False, now: datetime | None = None
) -> bool:
    """The one freshness rule. A role still listed on the company's own board
    is never too old: big companies keep evergreen roles up for months, so age
    only lowers its rank (age_penalty). Otherwise an undated posting counts as
    fresh, and a dated one must be no older than FRESHNESS_DAYS."""
    if listed_on_board:
        return True
    age = posting_age_days(posted_at, now)
    return age is None or 0 <= age <= FRESHNESS_DAYS


def posting_is_fresh(posted_at: str | None, now: datetime | None = None) -> bool:
    """Kept for callers that still import it: a dated posting inside the
    window. New code calls is_fresh."""
    return posting_age_days(posted_at, now) is not None and is_fresh(posted_at, now=now)


# Location policy: the operator is NYC-based and cannot relocate or commute to
# another city. A posting is geographically in scope only if it offers NYC or
# genuinely remote US work; hybrid or onsite anywhere else means commuting to
# another city, which is not possible. Uniform rule, internships included.
# The five boroughs plus the commuter cities across the Hudson.
NYC_RE = re.compile(
    r"\b(new york|nyc|brooklyn|manhattan|queens|bronx|staten island|long island city"
    r"|jersey city|hoboken)\b",
    re.I,
)
# Metros the candidate will work in, from SOURCER_METROS ("nyc", "nyc,sf").
# A posting in any of them is in scope; NYC_RE stays NYC only (a ranker feature).
METRO_TERMS = {
    "nyc": (
        "new york", "nyc", "brooklyn", "manhattan", "queens", "bronx", "staten island",
        "long island city", "jersey city", "hoboken",
    ),
    "sf": (
        "san francisco", "sf", "bay area", "oakland", "berkeley", "emeryville", "palo alto",
        "mountain view", "menlo park", "redwood city", "san mateo", "foster city",
        "burlingame", "sunnyvale", "santa clara", "cupertino", "san jose",
    ),
}  # fmt: skip
METROS = tuple(
    m.strip().lower()
    for m in os.environ.get("SOURCER_METROS", "nyc").split(",")
    if m.strip().lower() in METRO_TERMS
) or ("nyc",)
# Long Island (Nassau and Suffolk) is New York State, not New York City, but
# it is a commute from the city, so it counts with NYC. Town names alone are too
# common elsewhere ("Huntington, WV"), so a town needs ", NY" after it.
_LONG_ISLAND_TOWNS = (
    "melville", "hauppauge", "islandia", "ronkonkoma", "bohemia", "farmingdale", "bethpage",
    "plainview", "syosset", "woodbury", "jericho", "hicksville", "westbury", "garden city",
    "mineola", "uniondale", "hempstead", "lake success", "great neck", "manhasset",
    "port washington", "new hyde park", "valley stream", "rockville centre", "freeport",
    "huntington", "commack", "smithtown", "stony brook", "islip", "central islip", "bay shore",
    "brentwood", "deer park", "babylon", "lindenhurst", "patchogue", "medford", "riverhead",
    "glen cove", "oyster bay", "massapequa", "levittown", "east meadow", "long beach",
)  # fmt: skip
_LONG_ISLAND = (
    r"long island|nassau county|suffolk county|(?:"
    + "|".join(map(re.escape, _LONG_ISLAND_TOWNS))
    + r"),\s*(?:ny|new york)"
)
LONG_ISLAND_RE = re.compile(r"\b(" + _LONG_ISLAND + r")\b", re.I)
_TARGET_TERMS = tuple(t for m in METROS for t in METRO_TERMS[m])
TARGET_RE = re.compile(
    r"\b("
    + "|".join(map(re.escape, _TARGET_TERMS))
    + ("|" + _LONG_ISLAND if "nyc" in METROS else "")
    + r")\b",
    re.I,
)
REMOTE_RE = re.compile(r"\bremote\b", re.I)
US_EVIDENCE_RE = re.compile(r"\b(us|usa|u\.s\.a?\.?|united states|america)\b", re.I)
# Countries that mark a location token as non-US. Deliberately explicit, not
# exhaustive: an unrecognized city defaults to in-scope and the post-parse
# work_mode check catches the rest.
FOREIGN_RE = re.compile(
    r"\b(canada|ontario|toronto|vancouver|uk|united kingdom|london|ireland|dublin"
    r"|germany|berlin|munich|france|paris|netherlands|amsterdam|poland|warsaw"
    r"|spain|madrid|portugal|lisbon|india|bangalore|bengaluru|hyderabad|mumbai"
    r"|japan|tokyo|korea|seoul|singapore|australia|sydney|melbourne|brazil"
    r"|s[aã]o paulo|mexico|israel|tel aviv|uae|dubai|switzerland|zurich"
    r"|belgium|brussels|italy|sweden|stockholm|denmark|copenhagen|norway|oslo"
    r"|finland|helsinki|austria|czech|prague|romania|bucharest|ukraine|kyiv"
    r"|greece|philippines|manila|china|beijing|shanghai|hong kong|taiwan"
    r"|argentina|buenos aires|colombia|bogot[aá]|chile|santiago|new zealand|south africa"
    # Workday's country codes, as in "BEL - Remote"; capitals only, so prose never matches
    r"|(?-i:CAN|GBR|IRL|DEU|FRA|NLD|POL|ESP|PRT|IND|JPN|KOR|SGP|AUS|BRA|MEX|ISR|ARE|CHE"
    r"|BEL|ITA|SWE|DNK|NOR|FIN|AUT|CZE|ROU|HUN|UKR|GRC|PHL|CHN|HKG|TWN|ARG|COL|CHL|NZL"
    r"|ZAF|LUX|EST|LTU|LVA|BGR|HRV|SRB|SVK|MYS|IDN|THA|VNM|TUR|EGY|NGA|KEN|SAU|QAT))\b",
    re.I,
)


# Only explicit work/residency requirements override board metadata. Mentions of
# headquarters, salary bands, or optional office visits are not requirements.
_OUTSIDE_TERMS = (
    "sf", "san francisco", "bay area", "california", "seattle", "boston", "austin", "chicago",
    "los angeles", "san diego", "palo alto", "mountain view", "san jose", "denver", "atlanta",
    r"washington\s*,?\s*dc", "miami", "dallas", "houston", "philadelphia",
)  # fmt: skip
# Places outside every target metro; with SF targeted, California is not outside.
_OUTSIDE = [
    t for t in _OUTSIDE_TERMS
    if t not in _TARGET_TERMS and not (t == "california" and "sf" in METROS)
]  # fmt: skip
_OUTSIDE_NYC_RE = re.compile(r"\b(" + "|".join(_OUTSIDE) + r")\b|" + FOREIGN_RE.pattern, re.I)
_REQUIRED_LOCATION_RE = re.compile(
    r"\b(?:must|(?:will\s+)?need to|(?:are|is|will be)\s+(?:required|expected) to)\s+"
    r"(?:be able to\s+)?(?:work\s+(?:out of|from|at|in)|commute to|relocate to|"
    r"live in|reside in|be based in)\s+"
    r"|\b(?:this|the)\s+(?:role|position|internship)\s+"
    r"(?:is|will be)\s+(?:strictly\s+)?(?:on[ -]?site|in[ -]?person|hybrid)\s+"
    r"(?:in|at|based in)\s+"
    r"|\b(?:this|the)\s+(?:role|position|internship)\s+requires\s+"
    r"(?:working\s+(?:out of|from|at|in)|relocation to|residency in)\s+"
    # "a remote position open to candidates residing in Canada"
    r"|\bopen\s+(?:only\s+)?to\s+(?:candidates|applicants)\s+"
    r"(?:residing|living|based|located)\s+in\s+",
    re.I,
)
_OFFICE_REQUIREMENT_RE = re.compile(
    r"\b(?:work(?:ing)?\s+(?:out of|from|at|in)|attendance at)\s+"
    r"(?P<place>.+?)\s+(?:is\s+)?(?:required|mandatory)\b",
    re.I,
)
_REMOTE_OPTION_RE = re.compile(
    r"\bor\s+(?:(?:you\s+)?(?:can|may)\s+)?(?:work\s+)?remote(?:ly)?\b"
    r"|\bremote(?:ly)?\s+(?:work\s+)?(?:is\s+)?(?:also\s+)?(?:allowed|available|optional)\b",
    re.I,
)


_TITLE_REGION_RE = re.compile(
    (r"\bwest coast\b|" if "sf" not in METROS else "") + _OUTSIDE_NYC_RE.pattern, re.I
)


def title_location_conflict(title: str, wide: bool = False) -> bool:
    """A title that names a place outside NYC, e.g. "FDE (West Coast)" or
    "Data Scientist, London", is scoped there whatever the location list says.
    For a role in the wide window (see ``starts_in_wide_window``) only a
    foreign place in the title conflicts."""
    title = title or ""
    if wide:
        return bool(FOREIGN_RE.search(title))
    return bool(_TITLE_REGION_RE.search(title)) and not TARGET_RE.search(title)


# Anywhere in the US is in scope for roles that start in summer 2027 or are
# written for 2027 graduates: a candidate graduating in 2027 would move for
# those. Other roles keep the metro and remote rules. SOURCER_WIDE_US_YEAR
# moves the year.
WIDE_YEAR = int(os.environ.get("SOURCER_WIDE_US_YEAR", "2027"))
_WIDE_START_RE = re.compile(
    rf"\b(?:(?:summer|may|june|july|august|september|fall)\s+(?:of\s+)?{WIDE_YEAR}"
    rf"|{WIDE_YEAR}\s+(?:summer|new[ -]?grad\w*|graduates?|university|early[ -]?career|intern\w*)"
    rf"|class of {WIDE_YEAR}"
    rf"|graduat\w*\s+(?:in\s+|by\s+)?(?:(?:may|spring|june|summer)\s+)?{WIDE_YEAR}"
    rf"|new[ -]?grad\w*\s+\(?{WIDE_YEAR})\b",
    re.I,
)
_US_STATES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida"
    "|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland"
    "|massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada"
    "|new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio"
    "|oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee"
    "|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming"
    "|district of columbia|san francisco|bay area|silicon valley|los angeles|seattle"
    "|boston|chicago|austin|denver|atlanta|palo alto|mountain view|menlo park|sunnyvale"
    "|san jose|san diego|redmond|pittsburgh|philadelphia|miami|dallas|houston"
    # cities boards write without a state: "SF Office", "Charlotte"
    "|sf|nyc|brooklyn|manhattan|jersey city|hoboken|oakland|san mateo|redwood city"
    "|foster city|santa clara|cupertino|irvine|plano|raleigh|charlotte|nashville"
    "|salt lake city|phoenix|portland|minneapolis|detroit|baltimore|columbus|cleveland"
    r"|st\.? louis|kansas city"
)
US_PLACE_RE = re.compile(
    rf"\b(?:{_US_STATES})\b|,\s*(?-i:A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[DLNA]|K[SY]|LA|M[EDAINSOT]"
    r"|N[EVHJMYCD]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[TA]|W[AVIY])\b",
    re.I,
)


def starts_in_wide_window(posting: dict[str, Any]) -> bool:
    """A summer 2027 internship, a role written for 2027 graduates, or any
    full time role when the profile says "full_time_anywhere_us": true."""
    if names_target_start(posting) or undated_intern(posting):
        return True
    return is_full_time(posting) and (
        is_grad_program(posting) or bool(_profile_flag("full_time_anywhere_us"))
    )


# a term or a year in an internship's title or dates: it names its own start
_OTHER_TERM_RE = re.compile(
    r"\b(spring|fall|autumn|winter|year[- ]round|part[- ]time|co-?op|20\d\d)\b", re.I
)
# dates outside the summer in its text: "from January 25 through April 16"
_OFF_SUMMER_DATES_RE = re.compile(
    r"\b(?:january|february|march|april|september|october|november|december)"
    r"(?:\s+\d{1,2})?(?:,?\s+20\d\d)?\s+(?:through|to|until)\b"
    r"|\b(?:spring|fall|autumn|winter)\s+(?:semester|term|quarter|session)\b",
    re.I,
)


def undated_intern(posting: dict[str, Any], today: date | None = None) -> bool:
    """An internship that names no term or year, seen before June of the wide
    year: hiring for it now means the coming summer ("Sales Engineering
    Intern" in October 2026 is a summer 2027 role). Its text may still say
    otherwise: a spring term or another year's summer keeps it out."""
    if is_full_time(posting) or (today or date.today()) >= date(WIDE_YEAR, 6, 1):
        return False
    head = " ".join(str(posting.get(k) or "") for k in ("title", "start_date"))
    text = str(posting.get("description") or "")[:8000]
    other_year = any(int(m.group(3)) != WIDE_YEAR for m in TERM_RE.finditer(text))
    return not (_OTHER_TERM_RE.search(head) or _OFF_SUMMER_DATES_RE.search(text) or other_year)


# A program of several months for recent graduates ("Technology Analyst Program",
# "an 18-month development program for recent graduates") is a new graduate
# role even when it never names the year. "Program Manager" is a job title.
_PROGRAM_TITLE_RE = re.compile(
    r"\bprogram\b(?!\s+(?:manager|management|coordinator|analyst|specialist|director|lead|officer|"
    r"administrator|assistant|associate))",
    re.I,
)
_PROGRAM_TEXT_RE = re.compile(
    r"\b\d{1,2}[- ]month\s+(?:\w+\s+){0,2}program\b"
    r"|\b(?:rotational|graduate|new grad\w*|early careers?|development|leadership|analyst)"
    r"\s+program\b"
    r"[^.\n]{0,80}\b(?:recent|new|university|college)\s+grad",
    re.I,
)


def is_grad_program(posting: dict[str, Any]) -> bool:
    """A multi month program for new or recent graduates, by title or text."""
    if _PROGRAM_TITLE_RE.search(str(posting.get("title") or "")):
        return True
    return bool(_PROGRAM_TEXT_RE.search(str(posting.get("description") or "")[:8000]))


def names_target_start(posting: dict[str, Any]) -> bool:
    """The posting itself names a summer 2027 start or 2027 graduates, in its
    title, start date or text. No profile setting widens it."""
    text = " ".join(str(posting.get(k) or "") for k in ("title", "start_date"))
    description = str(posting.get("description") or "")[:8000]
    return bool(_WIDE_START_RE.search(text) or _WIDE_START_RE.search(description))


# the board's own word for an internship or co-op; a title that says it wins
# over a model reading of "full time"
INTERN_TITLE_RE = re.compile(r"\b(intern(?:ship)?s?|co-?op)\b", re.I)


def is_full_time(posting: dict[str, Any]) -> bool:
    """Not an internship or co-op, by employment type or title."""
    if str(posting.get("employment_type") or "").lower() == "intern":
        return False
    return not INTERN_TITLE_RE.search(str(posting.get("title") or ""))


_profile_cache: dict[tuple[Path, float], dict[str, Any]] = {}


def _profile_flag(key: str) -> Any:
    """One profile setting, read again only when the file changes."""
    try:
        stamp = (PROFILE_PATH, PROFILE_PATH.stat().st_mtime)
    except OSError:
        return None
    if stamp not in _profile_cache:
        _profile_cache.clear()
        _profile_cache[stamp] = load_profile() or {}
    return _profile_cache[stamp].get(key)


# "20 Locations": a board that hides its list of places behind a count
UNNAMED_PLACES_RE = re.compile(r"^\s*(?:\d+|multiple|several|various)\s+locations?\b", re.I)


def in_the_us(location: str) -> bool:
    """Some location option names a US place and none of them is only foreign."""
    tokens = [t.strip() for t in re.split(r"[;|]", location or "") if t.strip()]
    return any(
        US_PLACE_RE.search(t) or US_EVIDENCE_RE.search(t)
        for t in tokens
        if not FOREIGN_RE.search(t)
    )


# Programs open to one school's students: "AI Co-Op (Northeastern University)".
# A school in parentheses names who may apply; a school elsewhere in the title
# is usually the employer. Co-op terms are run through a school.
_SCHOOL_IN_TITLE_RE = re.compile(r"\([^)]*\b(?:university|college)\b[^)]*\)|\bco-?op\b", re.I)


def school_restricted(title: str, school: str | None = None) -> bool:
    """A title tied to one school's program, unless it is the candidate's school.
    "Internship/Co-op" offers co-op as an alternative, not a school's program."""
    match = _SCHOOL_IN_TITLE_RE.search(title or "")
    if not match:
        return False
    if re.fullmatch(r"co-?op", match.group(0), re.I) and INTERN_TITLE_RE.search(
        re.sub(r"\bco-?op\b", "", title, flags=re.I)
    ):
        return False
    return not (school and school.lower() in (title or "").lower())


def description_location_conflicts(description: str, wide: bool = False) -> bool:
    """Detect common mandatory office/residency conflicts without a model.

    This bounded recognizer does not infer location from company boilerplate.
    Unknown places and unfamiliar wording still require downstream review.
    """
    return description_location_conflict(description, wide) is not None


def description_location_conflict(description: str, wide: bool = False) -> str | None:
    """The clause that requires working or living outside the target metros,
    quoted for the trace, else None. With ``wide`` (a role she would move
    for) only a place outside the US conflicts."""
    outside = FOREIGN_RE if wide else _OUTSIDE_NYC_RE
    text = unescape(description).replace("\u2019", "'")
    text = re.sub(r"</?(?:p|li|div|br)\b[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]*>", " ", text)
    for clause in re.split(r"[;\n]|(?<=[.!?])\s+", text):
        places = []
        for match in _REQUIRED_LOCATION_RE.finditer(clause):
            # Negation applies to this requirement, not the whole description.
            if re.search(r"\b(?:not|never|no longer)\s*$|n't\s*$", clause[: match.start()], re.I):
                continue
            places.append(clause[match.end() :])
        places.extend(
            match["place"]
            for match in _OFFICE_REQUIREMENT_RE.finditer(clause)
            if not re.search(r"\b(?:not|never|no longer)\s*$", match["place"], re.I)
        )
        for candidate in places:
            # Keep genuine alternatives; a separate mandatory clause still wins.
            place = re.split(
                r",?\s+(?:but|although|while|during|where|which|with)\b"
                r"|\s+and\s+(?:travel|visit|attend)\b",
                candidate,
                maxsplit=1,
                flags=re.I,
            )[0]
            remote_option = _REMOTE_OPTION_RE.search(place)
            if remote_option and not re.search(
                outside.pattern + r"|\bnot\b", place[remote_option.start() :], re.I
            ):
                continue
            if not outside.search(place):
                continue
            if (
                # "our NY or SF office": a bare NY counts here, in a place clause
                (TARGET_RE.search(place) or ("nyc" in METROS and re.search(r"(?-i:\bNY\b)", place)))
                and re.search(r"\bor\b", place, re.I)
                and not re.search(r"\band\b", place, re.I)
            ):
                continue
            return _quote(clause)
    return None


def location_in_scope(location: str, description: str = "", wide: bool = False) -> bool:
    """A target metro (NYC by default, see SOURCER_METROS), or remote with no
    exclusively-foreign footprint. With ``wide`` (a role in the wide window,
    see ``starts_in_wide_window``) any US place is in scope too.

    The location string may join several options with ';'. Any target metro option is
    enough. Remote counts when at least one location token is not clearly
    foreign (ambiguous bare "Remote" stays in: recall over precision, the
    description checks reject common explicit conflicts before model calls).
    """
    if description_location_conflicts(description, wide):
        return False
    loc = location or ""
    if TARGET_RE.search(loc) or (wide and (in_the_us(loc) or UNNAMED_PLACES_RE.search(loc))):
        return True
    if not REMOTE_RE.search(loc):
        return False
    tokens = [t.strip() for t in re.split(r"[;|]", loc) if t.strip()]
    non_remote = [t for t in tokens if not REMOTE_RE.search(t)]
    if non_remote and all(FOREIGN_RE.search(t) for t in non_remote):
        return False  # e.g. "Tokyo, Japan; Remote": remote for Japan residents
    remote_tokens = [t for t in tokens if REMOTE_RE.search(t)]
    if any(US_EVIDENCE_RE.search(t) for t in remote_tokens):
        return True
    return not any(FOREIGN_RE.search(t) for t in remote_tokens)


# Deterministic hard-exclusion signals (explicit only, per spec).
SENIOR_TITLE_RE = re.compile(
    r"\b(senior|staff|principal|lead|director|head of|manager|vp|vice president"
    r"|sr\.?|distinguished|chief)\b",
    re.I,
)
# Career ladder levels above the first rung: "Engineer II", "Analyst III",
# "Data Scientist 2", "(L4)". Level I and 1 stay entry level.
LEVEL_TITLE_RE = re.compile(
    r"\b(?:ii|iii|iv|ll|lll)\b"  # "ll" is a common typo for II
    r"|\b(?:engineer|scientist|analyst|developer|architect|researcher)\s*,?\s*[2-9]\b"
    r"|\(\s*l[4-9]\s*\)|\bfounding\b|\bgroup product\b",
    re.I,
)
# Titles the senior regex false-positives on. "Member of Technical Staff" is
# an entry-level-friendly title at AI labs despite containing "staff".
# "Product Manager" is an entry title at many companies (APM, PM I); senior
# PMs still carry Senior, Lead, Principal, Group or a level. "Chief of Staff"
# counts only as an associate or rotational program.
SENIOR_EXCEPTIONS_RE = re.compile(
    r"member of technical staff"
    r"|(?:associate |junior |rotational )?product manager"
    r"|(?:associate |analyst,? )?chief of staff (?:associate|analyst|program|rotation\w*)"
    r"|associate chief of staff",
    re.I,
)


# Early career titles: "Program Manager Intern", "Team Lead, Rotational
# Program". Manager or lead there names the work, not the level.
EARLY_TITLE_RE = re.compile(
    r"\b(intern(?:ship)?s?|co-?op|new grad(?:uate)?|university grad(?:uate)?|campus|apprentice\w*"
    r"|rotational|fellowship)\b",
    re.I,
)
# "Analyst II" usually asks 0 to 2 years; "Engineer II" stays a level.
_ANALYST_TWO_RE = re.compile(r"\banalyst,?\s*(?:ii|ll|2)\b", re.I)


def is_senior_title(title: str) -> bool:
    """SENIOR_TITLE_RE with known false positives carved out. Single source
    of truth for every exclusion site (pipeline, capture, backlog, batch)."""
    title = _ANALYST_TWO_RE.sub("analyst", title or "")
    cleaned = SENIOR_EXCEPTIONS_RE.sub("", title)
    if EARLY_TITLE_RE.search(title):
        cleaned = re.sub(r"\b(manager|lead)\b", "", cleaned, flags=re.I)
    # levels read the full title: "Group Product Manager" must not lose its level
    # when the product manager exception is carved out
    return bool(SENIOR_TITLE_RE.search(cleaned) or LEVEL_TITLE_RE.search(title))


IMMEDIATE_START_RE = re.compile(r"immediate\s+start|start\s+immediately", re.I)

# Start-date availability uses the operator's explicit full-time start month,
# with the month after graduation as the fallback. Only stated dates exclude.
SEASON_START = {"winter": 1, "spring": 3, "summer": 6, "fall": 9, "autumn": 9}
MONTH_START = {
    m: i + 1
    for i, m in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ]
    )
}


_MONTH_NAMES = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
    "|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec"
)


def start_terms(start_date: str) -> list[tuple[int, int]]:
    """Every (year, month) a start-date string explicitly resolves to.
    Empty list = nothing resolvable (ambiguous strings are never excluded)."""
    sd = (start_date or "").lower()
    out = []
    for m in re.finditer(r"(winter|spring|summer|fall|autumn)[\s,]*(20\d\d)", sd):
        out.append((int(m.group(2)), SEASON_START[m.group(1)]))
    for m in re.finditer(r"(20\d\d)[\s,]*(winter|spring|summer|fall|autumn)", sd):
        out.append((int(m.group(1)), SEASON_START[m.group(2)]))
    # "January 2027", "January 4, 2027", "Jan. 4th 2027"
    for m in re.finditer(
        rf"\b({_MONTH_NAMES})\b\.?(?:\s+\d{{1,2}}(?:st|nd|rd|th)?)?,?\s*(20\d\d)", sd
    ):
        out.append((int(m.group(2)), MONTH_START.get(m.group(1)) or _SHORT_MONTHS[m.group(1)]))
    for m in re.finditer(r"(20\d\d)-(0[1-9]|1[0-2])\b", sd):
        out.append((int(m.group(1)), int(m.group(2))))
    if not out:
        # "2027 new grad" / "new grad 2027": class-year programs start in the
        # summer after graduation of that class.
        for m in re.finditer(r"(20\d\d)\s*new grad|new grad\s*(20\d\d)", sd):
            out.append((int(m.group(1) or m.group(2)), 6))
    return out


def starts_before(start_text: str, available: str) -> bool:
    """Every start term the text names falls before ``available`` (YYYY-MM)."""
    if not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", available):
        raise ValueError("an availability date must be YYYY-MM")
    cutoff = (int(available[:4]), int(available[5:]))
    terms = start_terms(start_text)
    return bool(terms) and all(t < cutoff for t in terms)


def starts_before_graduation(start_date: str, profile: dict | None) -> bool:
    """Compare explicit start terms with the operator's full-time availability.

    An explicit YYYY-MM overrides the conservative month-after-graduation
    fallback. Unknown posting dates stay eligible for manual timing review.
    The function name is retained for callers using the original API.
    """
    if not profile:
        return False
    available = profile.get("earliest_start_full_time")
    if available is not None:
        if not isinstance(available, str) or not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", available):
            raise ValueError("earliest_start_full_time must be YYYY-MM")
        year, month = available.split("-")
        cutoff = (int(year), int(month))
    elif profile.get("grad_year"):
        cutoff = (profile["grad_year"], profile.get("grad_month", 5) + 1)
    else:
        return False
    terms = start_terms(start_date)
    return bool(terms) and all(t < cutoff for t in terms)


_WHEN_WORDS = "|".join(
    [*MONTH_START, "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov",
     "dec", "winter", "spring", "summer", "fall", "autumn"]
)  # fmt: skip
_WHEN = rf"(?:({_WHEN_WORDS})\s+)?(20\d\d)"
GRAD_WINDOW_RE = re.compile(
    rf"graduat\w*[^.;\n]{{0,60}}?\b(?:between|from)\s+{_WHEN}\s+(?:and|to|through|until)\s+{_WHEN}",
    re.I,
)
_SHORT_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
_SEASON_END = {"winter": 2, "spring": 5, "summer": 8, "fall": 12, "autumn": 12}


def _month(word: str | None, *, end: bool) -> int:
    w = (word or "").lower()
    if w in MONTH_START:
        return MONTH_START[w]
    if w in _SHORT_MONTHS:
        return _SHORT_MONTHS[w]
    if w in SEASON_START:
        return _SEASON_END[w] if end else SEASON_START[w]
    return 12 if end else 1


def graduation_window(text: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """The graduation dates a posting accepts, as (year, month) bounds, when
    it states a range ("graduation date between December 2027 and June 2028")."""
    m = GRAD_WINDOW_RE.search(text or "")
    if not m:
        return None
    start = (int(m.group(2)), _month(m.group(1), end=False))
    end = (int(m.group(4)), _month(m.group(3), end=True))
    return (start, end) if start <= end else None


_DATE_RE = re.compile(rf"\b{_WHEN}\b", re.I)
# the words around a graduation sentence that make its dates a bound, not a list
_AT_LEAST_RE = re.compile(
    r"\b(?:or later|or after|and later|and after|after|no earlier than)\b", re.I
)
_AT_MOST_RE = re.compile(r"\b(?:or earlier|or before|before|by|no later than|prior to)\b", re.I)
_CLASS_OF_RE = re.compile(r"\bclass(?:es)? of\s+(20\d\d)(?:\s*(?:,|or|and|/)\s*(20\d\d))?", re.I)
_STANDINGS = ("freshman", "sophomore", "junior", "senior")
_STANDING_RE = re.compile(
    r"\b(current|rising)\s+(freshm[ae]n|sophomores?|juniors?|seniors?)\b"
    r"(?:\s*(?:,|or|and|/)\s*(?:(current|rising)\s+)?(freshm[ae]n|sophomores?|juniors?|seniors?)\b)*",
    re.I,
)
_RETURN_RE = re.compile(
    r"\breturn(?:ing)?\s+to\s+(?:school|campus|(?:your|their|an?)\s+(?:studies|program|university|degree"
    r"|academic program))\b|\bcontinu\w+\s+(?:your|their)\s+(?:studies|education|degree)\s+"
    r"(?:after|following)\b|\benrolled\b[^.]{0,80}\b(?:after|following)\s+the\s+internship\b",
    re.I,
)


def _grad_sentences(text: str) -> Iterator[str]:
    """The text right after each mention of graduating, up to the sentence end."""
    for m in re.finditer(r"graduat\w*", text or "", re.I):
        yield re.split(r"[.;\n]", text[m.start() : m.start() + 200])[0]


def _listed_window(text: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """The span of graduation dates a posting lists, as (year, month) bounds:
    "graduation date of December 2027 or May 2028" -> 12/2027 to 5/2028,
    "graduating in 2028 or later" -> 1/2028 onward. None when it lists none."""
    for s in _grad_sentences(text):
        found = list(_DATE_RE.finditer(s))
        # the date must follow "graduat..." closely, as in "graduation date of", "graduating in"
        if not found or found[0].start() > 35 or "," in s[: found[0].start()]:
            continue
        dates = [
            (int(m.group(2)), _month(m.group(1), end=False), _month(m.group(1), end=True))
            for m in found
        ]
        lo = min((y, m0) for y, m0, _ in dates)
        hi = max((y, m1) for y, _, m1 in dates)
        # a bound word counts only right around the dates, not anywhere in the sentence
        near = s[max(0, found[0].start() - 12) : found[-1].end() + 15]
        if _AT_LEAST_RE.search(near):
            hi = (9999, 12)
        elif _AT_MOST_RE.search(near):
            lo = (0, 1)
        return lo, hi
    for a, b in _CLASS_OF_RE.findall(text or ""):
        years = [int(a), *([int(b)] if b else [])]
        return (min(years), 1), (max(years), 12)
    return None


def _standing(profile: dict[str, Any], today: date) -> int:
    """Her class now: 0 freshman to 3 senior, from the year she graduates."""
    year_ends = today.year + (1 if today.month >= 6 else 0)  # the spring this school year ends
    return max(0, min(3, 3 - (int(profile["grad_year"]) - year_ends)))


def outside_graduation_window(
    text: str, profile: dict[str, Any] | None, today: date | None = None
) -> str | None:
    """Reason when the posting's graduation rules leave the candidate out:
    a stated range or list of graduation dates, a class standing ("current
    juniors or sophomores"), or a rule that she return to school after the
    internship when she graduates before it ends."""
    if not profile or not profile.get("grad_year"):
        return None
    grad = (int(profile["grad_year"]), int(profile.get("grad_month") or 5))
    you = f"you graduate {grad[1]}/{grad[0]}"
    reason = _window_reason(text, grad, you)
    if reason is None and profile.get("in_progress", True):
        reason = _standing_reason(text, _standing(profile, today or utc_now().date()), you)
    if reason is None and _RETURN_RE.search(text or ""):
        later = [(int(y), _month(w, end=True)) for w, y in _DATE_RE.findall(text or "")]
        if any(d > grad for d in later):
            reason = f"must return to school after the internship ({you})"
    return reason


def _window_reason(text: str, grad: tuple[int, int], you: str) -> str | None:
    window = graduation_window(text) or _listed_window(text)
    if window is None or window[0] <= grad <= window[1]:
        return None
    (sy, sm), (ey, em) = window
    if ey == 9999:
        return f"graduation {sm}/{sy} or later ({you})"
    if sy == 0:
        return f"graduation by {em}/{ey} ({you})"
    return f"graduation window {sm}/{sy} to {em}/{ey} ({you})"


def _standing_reason(text: str, mine: int, you: str) -> str | None:
    """ "current juniors or sophomores" when she is a senior, and the like."""
    for m in _STANDING_RE.finditer(text or ""):
        words = r"(current|rising)?\s*(freshm|sophomore|junior|senior)"
        named = re.findall(words, m.group(0), re.I)
        accepted, prefix = set(), m.group(1).lower()
        for when, word in named:
            rank = _STANDINGS.index(word.lower().replace("freshm", "freshman"))
            accepted.add(rank - 1 if (when or prefix).lower() == "rising" else rank)
        if accepted and mine not in accepted:
            return f"open to {m.group(0).strip()} ({you}, so a {_STANDINGS[mine]} now)"
    return None


# Candidate profile: drives level fit and the graduation availability rule.
# Absent profile means everything fits (generic behaviour before onboarding).
Profile = dict[str, Any]


def load_profile(path: Path | None = None) -> Profile | None:
    """The candidate profile, or None when the file is missing or invalid."""
    try:
        data = json.loads((path or PROFILE_PATH).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def candidate_targets(profile: Profile | None = None) -> tuple[str, str]:
    """The work the candidate wants and the work she avoids, in her own
    words from the profile ("target_work", "avoid_work"). Plain text about the
    work, never a list of titles, since titles change. Empty when unset."""
    p = profile if profile is not None else load_profile() or {}
    return str(p.get("target_work") or "").strip(), str(p.get("avoid_work") or "").strip()
