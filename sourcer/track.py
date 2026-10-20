"""Application tracker CLI.

  python -m sourcer.track list [status]        ranked list with ids
  python -m sourcer.track set <id> <status> [note]

Statuses: new, shortlist, applied, interview, offer, rejected, archived.
"""

from __future__ import annotations

import sys
from typing import Any

from . import db

SELECT_BOARD = (
    "SELECT id, score, status, company, title, employment_type, start_date "
    "FROM postings WHERE excluded=0"
)


def list_postings(conn: db.Connection, status: str | None = None) -> list[Any]:
    q = SELECT_BOARD
    args: tuple[Any, ...] = ()
    if status:
        q += " AND status=?"
        args = (status,)
    q += " ORDER BY score DESC"
    return list(conn.execute(q, args).fetchall())


def format_row(r: Any) -> str:
    score = f"{r['score']:>3}" if r["score"] is not None else "  -"
    return (
        f"{r['id']:>4}  [{score}]  {r['status']:<9} "
        f"{r['company']} - {r['title']}  ({r['employment_type']}, "
        f"start: {r['start_date'] or 'n/a'})"
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    conn = db.connect()
    if not args or args[0] == "list":
        status = args[1] if len(args) > 1 else None
        if status and status not in db.STATUSES:
            print(f"unknown status {status!r}; use one of {db.STATUSES}", file=sys.stderr)
            return 2
        rows = list_postings(conn, status)
        for r in rows:
            print(format_row(r))
        print(f"{len(rows)} postings")
        return 0
    if args[0] == "set" and len(args) >= 3:
        note = " ".join(args[3:]) or None
        try:
            n = db.set_status(conn, int(args[1]), args[2], note)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print("updated" if n else f"no posting with id {args[1]}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
