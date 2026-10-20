"""The life of every posting, as a timeline of events.

Each stage that decides something about a posting writes one event: where it
was found, every gate it passed or failed, the title check, the score (its full
trace is kept on the posting), the exclusion rules, the level fit, board checks
that keep or archive it, status changes, and what the side panel did. Any
number, and any drop, can then be explained from stored facts instead of a rerun.

Events travel on the posting as ``p["_events"]`` while a run works on it and
are written once the posting has an id. Postings the search drops before saving
are written straight away, keyed by their address, and pruned after
``KEEP_DROPPED_DAYS``; a saved posting's timeline is kept for good.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404  fixed argv, no shell
import uuid
from datetime import timedelta
from functools import cache
from pathlib import Path
from typing import Any

from . import config

KEEP_DROPPED_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS posting_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    run_id TEXT,
    code TEXT,
    posting_id INTEGER,          -- NULL for a posting the search dropped before saving
    url TEXT,
    company TEXT,
    title TEXT,
    stage TEXT NOT NULL,         -- found, gate, title_check, score, rules, level, saved,
                                 -- board, status, panel
    verdict TEXT,
    detail TEXT                  -- JSON
);
CREATE INDEX IF NOT EXISTS posting_events_posting ON posting_events(posting_id);
CREATE INDEX IF NOT EXISTS posting_events_url ON posting_events(url);
"""

RUN_ID = uuid.uuid4().hex[:12]  # one search run, one process


@cache
def code_version() -> str:
    """The commit the code ran at, so a change in behavior can be dated."""
    try:
        out = subprocess.run(  # nosec B603 B607
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).resolve().parent,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def ensure(conn: Any) -> Any:
    conn.executescript(SCHEMA)
    return conn


def event(p: dict[str, Any], stage: str, verdict: Any = None, **detail: Any) -> None:
    """Note an event on a posting still in flight; written with :func:`flush`."""
    p.setdefault("_events", []).append(
        {
            "at": config.utc_now().isoformat(timespec="seconds"),
            "stage": stage,
            "verdict": None if verdict is None else str(verdict),
            "detail": detail,
        }
    )


def _row(p: dict[str, Any], e: dict[str, Any], posting_id: int | None) -> tuple[Any, ...]:
    return (
        e["at"],
        RUN_ID,
        code_version(),
        posting_id,
        p.get("url"),
        p.get("company"),
        p.get("title"),
        e["stage"],
        e["verdict"],
        json.dumps(e["detail"], default=str) if e["detail"] else None,
    )


INSERT = (
    "INSERT INTO posting_events (at, run_id, code, posting_id, url, company, title, stage, "
    "verdict, detail) VALUES (?,?,?,?,?,?,?,?,?,?)"
)


def flush(conn: Any, p: dict[str, Any], posting_id: int | None) -> None:
    """Write a posting's events in flight, then forget them."""
    events = p.pop("_events", None)
    if not events:
        return
    ensure(conn)
    conn.executemany(INSERT, [_row(p, e, posting_id) for e in events])


def record(
    conn: Any, posting_id: int | None, stage: str, verdict: Any = None, **detail: Any
) -> None:
    """One event for a posting already saved (a board check, a status change)."""
    ensure(conn)
    p: dict[str, Any] = {}
    if posting_id is not None:
        row = conn.execute(
            "SELECT url, company, title FROM postings WHERE id=?", (posting_id,)
        ).fetchone()
        if row is not None:
            p = {"url": row[0], "company": row[1], "title": row[2]}
    event(p, stage, verdict, **detail)
    flush(conn, p, posting_id)


def log_dropped(conn: Any, dropped: list[tuple[dict[str, Any], str, dict[str, Any]]]) -> None:
    """Postings the search set aside before saving, each with the stage that did it."""
    if not dropped:
        return
    ensure(conn)
    rows = []
    for p, stage, detail in dropped:
        event(p, stage, detail.pop("verdict", None), **detail)
        rows += [_row(p, e, None) for e in p.pop("_events", [])]
    conn.executemany(INSERT, rows)


def prune(conn: Any, days: int = KEEP_DROPPED_DAYS) -> int:
    """Forget dropped postings' events after ``days``; saved ones stay."""
    ensure(conn)
    cur = conn.execute(
        "DELETE FROM posting_events WHERE posting_id IS NULL AND at < ?",
        ((config.utc_now() - timedelta(days=days)).isoformat(timespec="seconds"),),
    )
    return int(cur.rowcount or 0)


def timeline(
    conn: Any, posting_id: int | None = None, url: str | None = None
) -> list[dict[str, Any]]:
    """Every event for a posting, oldest first: by id (with any events from
    before it was saved, under its address), else by address."""
    ensure(conn)
    if posting_id is not None:
        found = conn.execute("SELECT url FROM postings WHERE id=?", (posting_id,)).fetchone()
        url = found[0] if found else None
    rows = conn.execute(
        "SELECT at, run_id, code, stage, verdict, detail FROM posting_events "
        "WHERE (posting_id = ?) OR (posting_id IS NULL AND url = ?) ORDER BY at, id",
        (posting_id, url),
    ).fetchall()
    return [
        {
            "at": r[0],
            "run": r[1],
            "code": r[2],
            "stage": r[3],
            "verdict": r[4],
            "detail": json.loads(r[5]) if r[5] else {},
        }
        for r in rows
    ]
