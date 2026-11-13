"""Public showcase export: ranked postings with strengths-only rationales.

  python -m sourcer.export_showcase [out_path]

Writes docs/showcase.json for the public dashboard. Per posting: the
employer's sector label, role, location, score, derived type, start date,
and a sanitized one-line blurb.

Employers are never named publicly. The showcase exists to demonstrate that
the pipeline scores consistently, not to publish a ranking of named
companies, so each posting carries the sector label from companies.json
instead of the company name, and the posting URL is dropped because it
re-identifies the employer.

The blurb sanitizer is deterministic and fail-closed:

1. Sentences are kept only up to the first sentence containing gap
   vocabulary; "[timing unclear ...]" suffixes are stripped.
2. If the survivor still contains gap vocabulary, it is replaced with a
   neutral fallback chosen by score band, so a low-scoring posting never
   carries a blurb claiming a strong match.
3. Every check runs on text folded to plain ASCII, so look-alike letters
   and invisible characters cannot hide a name or a gap word.
4. A posting whose text still carries an employer name, a private term, a
   link, an email, a phone number or gap vocabulary is dropped (and counted,
   never logged in full). After building the payload, everything kept is
   re-scanned; any hit raises, so CI fails instead of publishing a leak.

Never included: employer names, posting URLs, skill gap data, statuses,
excluded postings, exclusion reasons, requirements, tech stacks.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import config, db, log

logger = log.get_logger(__name__)

OUT = config.PROJECT_ROOT / "docs" / "showcase.json"

GAP_MARKERS = re.compile(
    r"\b(gap|gaps|missing|misses|lack|lacks|lacking|absent|weak|weakness"
    r"|however|though|but\b|limited|no (?:explicit|evidence|mention|direct"
    r"|demonstrated|experience|specific)|not (?:explicitly|directly|stated"
    r"|mentioned|demonstrated)|doesn'?t|underutilized|below|short of"
    r"|falls? short|without|unclear|criticality|non-negotiable)\b",
    re.I,
)
FALLBACK = "Strong alignment with the weighted skills vector on core requirements."

# Score-banded fallbacks. Used when the sanitizer cannot keep any sentence of
# the model's rationale. Wording here must stay clear of GAP_MARKERS or the
# leak scan will reject it.
FALLBACKS: tuple[tuple[int, str], ...] = (
    (75, FALLBACK),
    (50, "Partial alignment with the weighted skills vector."),
    (0, "Minor overlap with the weighted skills vector."),
)

# The public page shows a short, curated slice, not the whole database: the
# best scored postings from employers that carry a public sector label.
SHOWCASE_LIMIT = int(os.environ.get("SOURCER_SHOWCASE_LIMIT", "60"))
MAX_ROLE_CHARS = 90
MAX_LOCATION_CHARS = 60
NO_LOCATION = "Location not listed"

# Aggregator boards sometimes paste a whole listing page into one field. Text
# like this in a title or location means the row was scraped badly and may
# carry other employers' names, so it is never published.
SCRAPE_JUNK = re.compile(r"\$\s?\d|\bvisa\b|full[- ]time\b.*full[- ]time|\boffice visa\b", re.I)

# Anything that looks like a link, a domain, an email or a phone number must
# never be published. Checked on folded ASCII text.
CONTACT_PATTERNS = (
    re.compile(r"://|\bwww\.", re.I),
    re.compile(
        r"\b[a-z0-9][a-z0-9-]*\.(?:com|io|ai|co|org|net|dev|app|xyz|so|me|us|uk|gg|ly"
        r"|sh|tech|jobs|careers|work|team|inc|hr|to|cc|tv|biz|info)\b",
        re.I,
    ),
    re.compile(r"@"),
    # nine or more digits: a phone number, but not a year range like 2025-2026
    re.compile(r"(?<!\d)(?:\d[\s().+-]?){9,}"),
)

# Letters from other scripts that look like Latin ones. NFKD does not fold
# these, and a single swapped letter is enough to dodge a name match.
CONFUSABLES = str.maketrans(
    "\u0430\u0435\u043e\u0440\u0441\u0443\u0445\u0456\u0458\u0455\u04cf\u0501\u051b\u051d"
    "\u0410\u0412\u0415\u041a\u041c\u041d\u041e\u0420\u0421\u0422\u0425\u0406\u0408\u0405"
    "\u03b1\u03bf\u03c1\u03b9\u03ba\u03bd\u03c4\u03c5\u03c7"
    "\u0391\u0392\u0395\u0396\u0397\u0399\u039a\u039c\u039d\u039f\u03a1\u03a4\u03a5\u03a7",
    "aeopcyxijsldqwABEKMHOPCTXIJSaopikvtuxABEZHIKMNOPTYX",
)

# Fields that carry text from the posting or the model, as opposed to dates and numbers.
TEXT_FIELDS = ("sector", "role", "location", "blurb")

SELECT_SHOWCASE = (
    "SELECT company, title, location, score, rationale, employment_type, start_date, posted_at "
    "FROM postings WHERE excluded=0 AND score IS NOT NULL AND source='ats' "
    "ORDER BY score DESC, company"
)


def normalize(text: str | None) -> str:
    """Fold text to plain ASCII before any check or publication.

    NFKC turns full width and stylised letters into plain ones, format
    characters (zero width spaces and joiners) are removed, look-alike
    Cyrillic and Greek letters become Latin ones, accents are stripped, and
    anything still outside ASCII is dropped. A name or a gap word therefore
    cannot slip past a word match by hiding or swapping a character.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text)
    visible = "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")
    latin = unicodedata.normalize("NFKD", visible.translate(CONFUSABLES))
    plain = "".join(ch for ch in latin if unicodedata.category(ch) != "Mn")
    ascii_only = plain.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_only).strip()


def name_pattern(names: Iterable[str]) -> re.Pattern[str] | None:
    """One pattern that finds any name, ignoring spaces and punctuation.

    "Scale AI" also matches "ScaleAI", "scale-ai" and "Scale.AI": each name is
    reduced to its letters and digits, and any run of separators may sit
    between them. Word boundaries keep "Ramp" from matching "ramped", while a
    camel case join ("GleanWork") still counts as a boundary.
    """
    parts = set()
    for n in names:
        core = re.sub(r"[^a-z0-9]", "", normalize(n).lower())
        if len(core) >= 2:
            parts.add(r"[\W_]*".join(re.escape(c) for c in core))
    if not parts:
        return None
    alternation = "|".join(sorted(parts, key=len, reverse=True))
    # A camel case join counts as a boundary too, so "GleanWork" still names Glean.
    edge = r"(?-i:(?<=[a-z])(?=[A-Z]))"
    return re.compile(rf"(?:\b|{edge})(?:{alternation})(?:\b|{edge})", re.I)


def private_terms() -> list[str]:
    """Extra words to strip from public text, such as private project names.

    Read from SOURCER_SHOWCASE_REDACT (comma separated) so the list itself
    never has to live in the public repository.
    """
    raw = os.environ.get("SOURCER_SHOWCASE_REDACT", "")
    return [t.strip() for t in raw.split(",") if t.strip()]


FUZZY_MIN_CHARS = 7


def _deletions(word: str) -> set[str]:
    return {word[:i] + word[i + 1 :] for i in range(len(word))} | {word}


def near_names(names: Iterable[str]) -> dict[str, str]:
    """Index long names by every one letter deletion.

    Two words one edit apart (a swapped, missing or extra letter) always
    share a one deletion variant, so "Anthrapic" is caught as "Anthropic".
    Only names of 7 or more letters take part, which keeps ordinary words
    from colliding with short company names.
    """
    index: dict[str, str] = {}
    for n in names:
        core = re.sub(r"[^a-z0-9]", "", normalize(n).lower())
        if len(core) >= FUZZY_MIN_CHARS:
            for variant in _deletions(core):
                index.setdefault(variant, core)
    return index


def fuzzy_hit(text: str, index: dict[str, str]) -> bool:
    if not index:
        return False
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        if len(token) >= FUZZY_MIN_CHARS - 1 and any(v in index for v in _deletions(token)):
            return True
    return False


def redact_terms(text: str, terms: Iterable[str]) -> str:
    pattern = name_pattern(terms)
    return pattern.sub("a private project", text) if pattern and text else text


def clean_location(location: str | None, names: Iterable[str] | re.Pattern[str] | None) -> str:
    text = redact_names(normalize(location), names)
    if (
        not text
        or len(text) > MAX_LOCATION_CHARS
        or SCRAPE_JUNK.search(text)
        or "[employer]" in text
    ):
        return NO_LOCATION
    return text


def publishable_row(row: Any) -> bool:
    title = normalize(row["title"])
    return bool(title) and len(title) <= MAX_ROLE_CHARS and not SCRAPE_JUNK.search(title)


class LeakError(RuntimeError):
    """A published string carried gap vocabulary or an employer name."""


def fallback_for(score: int | None) -> str:
    value = max(score or 0, 0)
    return next(text for floor, text in FALLBACKS if value >= floor)


def sanitize_blurb(rationale: str | None, score: int | None = None) -> str:
    text = re.sub(r"\[timing unclear[^\]]*\]", "", normalize(rationale)).strip()
    sentences = re.split(r"(?<=[.;])\s+", text)
    kept = []
    for s in sentences:
        if GAP_MARKERS.search(s):
            break
        kept.append(s)
    out = " ".join(kept).strip()
    if not out or GAP_MARKERS.search(out):
        return fallback_for(score)
    return out


def load_sectors() -> dict[str, str]:
    """company name -> public sector label, from companies.json."""
    entries = json.loads(config.COMPANIES_PATH.read_text())
    return {e["company"]: e.get("sector", "Unlabelled") for e in entries}


def _domain_stem(domain: str | None) -> str:
    host = re.sub(r"^(?:https?://)?(?:www\.)?", "", (domain or "").strip().lower())
    stem = host.split("/")[0].split(".")[0]
    return stem if len(stem) >= 4 else ""


def employer_names(conn: db.Connection, sectors: dict[str, str]) -> set[str]:
    """Every employer name the payload must not carry.

    companies.json only lists the seed companies; discovery adds more to the
    companies table, and postings can name companies neither knows. All of
    them count, plus each company's website domain stem ("scaleai").
    """
    names = set(sectors)
    try:
        for row in db.list_companies(conn):
            company = dict(row)
            names.add(str(company.get("name") or ""))
            names.add(_domain_stem(company.get("domain")))
    except Exception:  # an older database without the table still exports
        logger.warning("companies table unavailable; using companies.json names only")
    for row in conn.execute("SELECT DISTINCT company FROM postings").fetchall():
        names.add(str(dict(row).get("company") or ""))
    return {n for n in names if n.strip()}


def redact_names(text: str, names: Iterable[str] | re.Pattern[str] | None) -> str:
    """Strip employer names out of free text.

    Employers routinely put their own name in the role title ("Year at
    <Company> - Forward Deployed Software Engineer"), and the model
    occasionally repeats it in a rationale, so dropping the company column
    alone does not anonymize the payload.
    """
    if not text:
        return text
    pattern = names if isinstance(names, re.Pattern) or names is None else name_pattern(names)
    if pattern is not None:
        text = pattern.sub("[employer]", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def derive_type(employment_type: str | None, start_date: str | None) -> str:
    et = (employment_type or "").lower()
    sd = (start_date or "").lower()
    if et == "intern" or "intern" in sd:
        return "internship"
    if "new grad" in sd or "graduate" in sd:
        return "new grad"
    if et in ("full time", "part time"):
        return et
    return "unspecified"


def check_record(
    p: dict[str, Any],
    names: re.Pattern[str] | None,
    terms: re.Pattern[str] | None = None,
    near: dict[str, str] | None = None,
) -> str | None:
    """Why this public record must not be published, or None when it is clean."""
    if GAP_MARKERS.search(normalize(p["blurb"])):
        return "gap vocabulary"
    free_text = normalize(" ".join(str(p.get(k) or "") for k in TEXT_FIELDS))
    if any(pattern.search(free_text) for pattern in CONTACT_PATTERNS):
        return "link or contact detail"
    haystack = normalize(" ".join(str(v) for v in p.values() if v))
    # "[employer]" is the redaction marker, not a name.
    haystack = haystack.replace("[employer]", " ")
    if names is not None and names.search(haystack):
        return "employer name"
    if near and fuzzy_hit(haystack, near):
        return "near match for an employer name"
    if terms is not None and terms.search(haystack):
        return "private term"
    return None


def build_postings(
    rows: Iterable[Any],
    sectors: dict[str, str],
    terms: Iterable[str] = (),
    limit: int | None = None,
    names: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    all_names = set(sectors) | set(names or ())
    name_re = name_pattern(all_names)
    near = near_names(all_names)
    term_list = list(terms)
    term_re = name_pattern(term_list)
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    dropped = 0
    for r in rows:
        if limit is not None and len(out) >= limit:
            break
        sector = sectors.get(r["company"], "Unlabelled")
        if sector == "Unlabelled" or not publishable_row(r):
            continue
        # One card per role per sector: the same opening listed in ten cities
        # is one result, and the first row is the best scored because rows
        # arrive sorted by score.
        key = (sector, normalize(r["title"]).lower())
        if key in seen:
            continue
        blurb = sanitize_blurb(r["rationale"], r["score"])
        # Links are checked before redaction too: "careers.Acme.io" would
        # otherwise turn into "careers.[employer].io" and look harmless.
        raw = normalize(" ".join(str(x or "") for x in (r["title"], r["location"], blurb)))
        if any(pattern.search(raw) for pattern in CONTACT_PATTERNS):
            dropped += 1
            continue
        record = {
            "sector": sector,
            "role": redact_names(normalize(r["title"]), name_re),
            # Some boards prefix the location with the employer ("Acme - Atlanta").
            "location": clean_location(r["location"], name_re),
            "score": r["score"],
            "type": derive_type(r["employment_type"], r["start_date"]),
            "start": normalize(r["start_date"]) or None,
            "posted": r["posted_at"],
            "blurb": redact_terms(redact_names(blurb, name_re), term_list),
        }
        if check_record(record, name_re, term_re, near) is not None:
            dropped += 1
            continue
        seen.add(key)
        out.append(record)
    if dropped:
        logger.info("showcase: dropped %d postings that failed the publication checks", dropped)
    return out


def leak_scan(
    postings: list[dict[str, Any]],
    employer_names: Iterable[str],
    terms: Iterable[str] = (),
) -> None:
    """Fail closed: no gap vocabulary, employer name, private term, link or
    contact detail anywhere in the payload."""
    employer_names = set(employer_names)
    name_re = name_pattern(employer_names)
    near = near_names(employer_names)
    term_re = name_pattern(terms)
    for p in postings:
        reason = check_record(p, name_re, term_re, near)
        if reason is not None:
            raise LeakError(f"{reason} leaked into payload for {p['sector']} {p['role']!r}")


def build_summary(conn: db.Connection) -> dict[str, Any]:
    runs = [dict(r) for r in conn.execute("SELECT * FROM run_logs ORDER BY id")]
    last = runs[-1] if runs else {}
    scored = conn.execute("SELECT COUNT(*) AS n FROM postings WHERE score IS NOT NULL").fetchone()
    listings = conn.execute("SELECT COUNT(*) AS n FROM postings WHERE score IS NULL").fetchone()
    n_scored = int(scored["n"]) if scored else 0
    n_listings = int(listings["n"]) if listings else 0
    parse_f = sum(r.get("parse_failures") or 0 for r in runs)
    score_f = sum(r.get("score_failures") or 0 for r in runs)
    # Failures happen per posting the model was asked to judge, so the rate is
    # over all postings judged across runs, not over the postings scored today.
    judged = sum(r.get("new_postings") or 0 for r in runs)
    return {
        "updated": last.get("run_ts"),
        "postings_on_boards": last.get("postings_seen"),
        "companies_covered": last.get("companies_covered"),
        "roles_scored": n_scored,
        "listings_tracked": n_listings,
        "schema_failure_rate": round((parse_f + score_f) / max(judged, 1), 4),
        "runs": len(runs),
        "runs_without_failures": sum(
            1 for r in runs if not (r.get("parse_failures") or r.get("score_failures"))
        ),
    }


def export_showcase(conn: db.Connection | None = None, out_path: Path | str | None = None) -> Path:
    conn = conn or db.connect()
    target = Path(out_path or OUT)
    sectors = load_sectors()
    terms = private_terms()
    names = employer_names(conn, sectors)
    postings = build_postings(
        conn.execute(SELECT_SHOWCASE).fetchall(), sectors, terms, SHOWCASE_LIMIT, names
    )
    leak_scan(postings, names, terms)
    payload = {"summary": build_summary(conn), "postings": postings}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=1))
    logger.info("wrote %s: %d postings, leak scan clean", target, len(postings))
    return target


def main(argv: list[str] | None = None) -> int:
    log.configure()
    args = sys.argv[1:] if argv is None else argv
    try:
        print(export_showcase(out_path=args[0] if args else None))
    except LeakError as exc:
        print(f"refusing to publish: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
