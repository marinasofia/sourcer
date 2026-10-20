"""Re-apply today's filter rules to postings already on the board.

  python -m sourcer.rescreen            # archive new rows that fail today's rules
  python -m sourcer.rescreen --dry-run  # print what would change
  python -m sourcer.rescreen --undo     # put every rescreen archive back to new

Rules tighten over time, but rows scored under older rules stay on the board.
This pass uses only stored fields and makes no model calls. Only rows still in
status new are archived; shortlisted rows are reported, never touched, because
the operator chose them.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from . import config, db, trace
from .config import Profile
from .eligibility import eligible

NOTE_PREFIX = "auto-archived: rescreen: "
# Guardrail: a rule change that would archive more than this share of the open
# board in one pass is more likely a bad pattern than a real cleanup. The pass
# refuses and prints what it would do; --force overrides after a look.
MAX_ARCHIVE_SHARE = 0.25
GUARDRAIL_MIN_ROWS = 20  # a small board can lose most rows to one real rule

OPEN_ROWS = (
    "SELECT id, company, title, location, status, score, seniority, years_experience_min, "
    "employment_type, start_date, work_mode, description FROM postings "
    "WHERE excluded=0 AND score IS NOT NULL AND status IN ('new', 'shortlist')"
)


def reason_for(row: dict[str, Any], profile: Profile | None) -> str | None:
    """Why this stored row fails today's rules, or None when it still fits.
    Kept for callers that still import it; ``eligibility.eligible`` is the rule."""
    return eligible(row, profile).reason or None


def rescreen(
    conn: db.Connection, profile: Profile | None, *, dry_run: bool = False, force: bool = False
) -> dict[str, Any]:
    """Archive failing new rows. Returns archived and flagged (shortlist) rows,
    and ``refused`` when the guardrail held the archive back."""
    archived: list[dict[str, Any]] = []
    flagged: list[dict[str, Any]] = []
    rows = [dict(raw) for raw in conn.execute(OPEN_ROWS).fetchall()]
    for row in rows:
        reason = reason_for(row, profile)
        if not reason:
            continue
        item = {**row, "reason": reason}
        if row["status"] == "shortlist":
            flagged.append(item)
            continue
        archived.append(item)
    open_new = sum(1 for row in rows if row["status"] == "new")
    refused = (
        not force
        and open_new >= GUARDRAIL_MIN_ROWS
        and len(archived) / open_new > MAX_ARCHIVE_SHARE
    )
    if not dry_run and not refused:
        for item in archived:
            conn.execute(
                "UPDATE postings SET status='archived', status_updated=?, notes=? "
                "WHERE id=? AND status='new'",
                (db.now_iso(), NOTE_PREFIX + item["reason"], item["id"]),
            )
            trace.record(conn, item["id"], "status", "archived", why=f"rescreen: {item['reason']}")
        conn.commit()
    return {"archived": archived, "flagged": flagged, "refused": refused}


def undo(conn: db.Connection) -> int:
    rows = conn.execute(
        "SELECT id FROM postings WHERE status='archived' AND notes LIKE ?", (NOTE_PREFIX + "%",)
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE postings SET status='new', status_updated=?, notes=NULL WHERE id=?",
            (db.now_iso(), row["id"]),
        )
    conn.commit()
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--undo", action="store_true")
    ap.add_argument("--force", action="store_true", help="archive past the guardrail share")
    args = ap.parse_args(argv)
    conn = db.connect()
    if args.undo:
        print(f"restored {undo(conn)} rows to new")
        return 0
    result = rescreen(conn, config.load_profile(), dry_run=args.dry_run, force=args.force)
    verb = "would archive" if args.dry_run or result["refused"] else "archived"
    for item in result["archived"]:
        print(f"{verb}: [{item['score']}] {item['company']} | {item['title']}: {item['reason']}")
    for item in result["flagged"]:
        print(f"shortlist check: {item['company']} | {item['title']}: {item['reason']}")
    print(f"{verb} {len(result['archived'])}, shortlist flagged {len(result['flagged'])}")
    if result["refused"]:
        print(
            f"refused: more than {MAX_ARCHIVE_SHARE:.0%} of open rows; nothing archived. "
            "Check the reasons above, then rerun with --force if they are right."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
