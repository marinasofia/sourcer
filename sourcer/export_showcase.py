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
3. After building the payload, every blurb is re-scanned; any hit raises,
   so CI fails instead of publishing a leak.

Never included: employer names, posting URLs, skill gap data, statuses,
excluded postings, exclusion reasons, requirements, tech stacks.
"""

from __future__ import annotations

import json
import re
import sys
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
    r"|unclear|criticality|non-negotiable)\b",
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

SELECT_SHOWCASE = (
    "SELECT company, title, location, score, rationale, employment_type, start_date, posted_at "
    "FROM postings WHERE excluded=0 AND score IS NOT NULL AND source='ats' "
    "ORDER BY score DESC, company"
)


class LeakError(RuntimeError):
    """A published string carried gap vocabulary or an employer name."""


def fallback_for(score: int | None) -> str:
    value = max(score or 0, 0)
    return next(text for floor, text in FALLBACKS if value >= floor)


def sanitize_blurb(rationale: str | None, score: int | None = None) -> str:
    text = re.sub(r"\[timing unclear[^\]]*\]", "", rationale or "").strip()
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


def redact_names(text: str, names: Iterable[str]) -> str:
    """Strip employer names out of free text.

    Employers routinely put their own name in the role title ("Year at
    <Company> - Forward Deployed Software Engineer"), and the model
    occasionally repeats it in a rationale, so dropping the company column
    alone does not anonymize the payload.
    """
    if not text:
        return text
    for n in sorted(names, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(n)}\b", "[employer]", text, flags=re.I)
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


def build_postings(rows: Iterable[Any], sectors: dict[str, str]) -> list[dict[str, Any]]:
    names = set(sectors)
    return [
        {
            "sector": sectors.get(r["company"], "Unlabelled"),
            "role": redact_names(r["title"], names),
            "location": r["location"],
            "score": r["score"],
            "type": derive_type(r["employment_type"], r["start_date"]),
            "start": r["start_date"],
            "posted": r["posted_at"],
            "blurb": redact_names(sanitize_blurb(r["rationale"], r["score"]), names),
        }
        for r in rows
    ]


def leak_scan(postings: list[dict[str, Any]], employer_names: Iterable[str]) -> None:
    """Fail closed: no gap vocabulary and no employer name anywhere in the payload."""
    names = {c.lower() for c in employer_names}
    for p in postings:
        if GAP_MARKERS.search(p["blurb"]):
            raise LeakError(f"leak scan failed for {p['sector']} {p['role']!r}: {p['blurb']!r}")
        haystack = " ".join(str(v) for v in p.values() if v).lower()
        hit = next((n for n in names if re.search(rf"\b{re.escape(n)}\b", haystack)), None)
        if hit:
            raise LeakError(f"employer name {hit!r} leaked into payload: {p!r}")


def build_summary(conn: db.Connection) -> dict[str, Any]:
    runs = [dict(r) for r in conn.execute("SELECT * FROM run_logs ORDER BY id")]
    last = runs[-1] if runs else {}
    scored = conn.execute("SELECT COUNT(*) AS n FROM postings WHERE score IS NOT NULL").fetchone()
    listings = conn.execute("SELECT COUNT(*) AS n FROM postings WHERE score IS NULL").fetchone()
    n_scored = int(scored["n"]) if scored else 0
    n_listings = int(listings["n"]) if listings else 0
    parse_f = sum(r.get("parse_failures") or 0 for r in runs)
    score_f = sum(r.get("score_failures") or 0 for r in runs)
    return {
        "updated": last.get("run_ts"),
        "postings_on_boards": last.get("postings_seen"),
        "companies_covered": last.get("companies_covered"),
        "roles_scored": n_scored,
        "listings_tracked": n_listings,
        "schema_failure_rate": round((parse_f + score_f) / max(n_scored, 1), 4),
    }


def export_showcase(conn: db.Connection | None = None, out_path: Path | str | None = None) -> Path:
    conn = conn or db.connect()
    target = Path(out_path or OUT)
    sectors = load_sectors()
    postings = build_postings(conn.execute(SELECT_SHOWCASE).fetchall(), sectors)
    leak_scan(postings, sectors)
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
