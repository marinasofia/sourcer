"""Render the end-of-run digest as text. Pure function of the final state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

RULE = "=" * 78
THIN = "-" * 78


def render_digest(state: Mapping[str, Any]) -> str:
    scored = state.get("scored", [])
    stats = state.get("stats", {})
    ranked = sorted(
        (p for p in scored if not p.get("excluded") and p.get("score") is not None),
        key=lambda p: p["score"],
        reverse=True,
    )
    n_listings = sum(1 for p in scored if p.get("source") == "simplify")
    lines = [
        "",
        RULE,
        "SOURCER DIGEST",
        RULE,
        f"companies covered : {stats.get('companies_covered')}",
        f"postings on boards: {stats.get('postings_seen')} "
        f"(prefiltered out of scope: {stats.get('prefiltered_out')})",
        f"new after dedupe  : {stats.get('new_postings')} "
        f"(duplicates skipped: {stats.get('dedupe_count')})",
        f"parse/score fails : {stats.get('parse_failures')}/{stats.get('score_failures')}",
        f"skipped, cost cap : {stats.get('skipped_cost_cap', 0)}",
        f"hard excluded     : {stats.get('excluded_count')}",
        f"listing-only new  : {n_listings} (SimplifyJobs, unscored)",
        f"timing unclear    : {stats.get('timing_unclear_count')}",
        f"tokens in/out     : {stats.get('prompt_tokens')}/{stats.get('completion_tokens')}"
        f"  est cost ${stats.get('est_cost_usd')}",
        THIN,
    ]
    for i, p in enumerate(ranked, 1):
        flag = "  [TIMING UNCLEAR]" if p.get("timing_unclear") else ""
        lines.append(f"{i:>2}. [{p['score']:>3}] {p['company']} - {p['title']}{flag}")
        lines.append(
            f"       {p.get('location')}  |  {p.get('employment_type')}"
            f"  |  start: {p.get('start_date') or 'n/a'}"
        )
        lines.append(f"       {p.get('rationale')}")
        lines.append(f"       {p.get('url')}")
    if stats.get("exclusions"):
        lines.append(THIN)
        lines.append("EXCLUDED (logged to run_logs):")
        lines.extend(
            f"  - {e['company']} - {e['title']}: {e['reason']}" for e in stats["exclusions"]
        )
    lines.append(RULE)
    return "\n".join(lines)
