"""Aggregate-only export for the public dashboard.

  python -m sourcer.export_public [out_path]

Writes docs/data-public.json containing run-level statistics only: totals,
failure rates, cost, duration. Deliberately absent: individual postings,
scores, rationales, statuses, and any skill or gap information.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import config, db, log

logger = log.get_logger(__name__)

OUT = config.PROJECT_ROOT / "docs" / "data-public.json"


def _count(conn: db.Connection, where: str) -> int:
    # Only fixed predicates from build_public_stats reach this private helper.
    query = f"SELECT COUNT(*) AS n FROM postings WHERE {where}"  # noqa: S608  # nosec B608
    row = conn.execute(query).fetchone()
    return int(row["n"]) if row else 0


def build_public_stats(conn: db.Connection) -> dict[str, Any]:
    runs = [dict(r) for r in conn.execute("SELECT * FROM run_logs ORDER BY id")]
    last = runs[-1] if runs else {}
    n_scored = _count(conn, "score IS NOT NULL")
    n_ranked = _count(conn, "excluded=0 AND score IS NOT NULL")
    n_listings = _count(conn, "score IS NULL")
    parse_f = sum(r.get("parse_failures") or 0 for r in runs)
    score_f = sum(r.get("score_failures") or 0 for r in runs)
    return {
        "updated": last.get("run_ts"),
        "companies_covered": last.get("companies_covered"),
        "postings_on_boards": last.get("postings_seen"),
        "postings_scored_total": n_scored,
        "postings_ranked": n_ranked,
        "listings_tracked": n_listings,
        "runs": len(runs),
        "parse_failures_total": parse_f,
        "score_failures_total": score_f,
        "schema_failure_rate": round((parse_f + score_f) / max(n_scored, 1), 4),
        "last_run_cost_usd": last.get("est_cost_usd"),
        "last_run_duration_s": last.get("duration_s"),
        "last_run_new_postings": last.get("new_postings"),
        "last_run_dedupe_count": last.get("dedupe_count"),
    }


def export_public(conn: db.Connection | None = None, out_path: Path | str | None = None) -> Path:
    conn = conn or db.connect()
    target = Path(out_path or OUT)
    out = build_public_stats(conn)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, indent=1))
    logger.info("wrote %s: %s", target, json.dumps(out))
    return target


def main(argv: list[str] | None = None) -> int:
    log.configure()
    args = sys.argv[1:] if argv is None else argv
    print(export_public(out_path=args[0] if args else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
