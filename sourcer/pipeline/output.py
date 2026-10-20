"""Output node: persist scored postings and the run log, archive level misfits."""

from __future__ import annotations

import time

from .. import db, trace
from ..log import get_logger
from .state import State, _timed

log = get_logger(__name__)


def output_node(state: State) -> State:
    t0 = time.time()
    conn = state["conn"]
    stats = state["stats"]
    usage = state["usage"]
    text_by_url = {p["url"]: p.get("description") for p in state.get("new_postings", [])}
    for p in state["scored"]:
        if not p.get("description") and text_by_url.get(p.get("url")):
            p["description"] = text_by_url[p["url"]]
        db.insert_posting(conn, p)
        row = conn.execute("SELECT id FROM postings WHERE hash=?", (p["hash"],)).fetchone()
        trace.event(p, "saved", "new posting" if row else "not saved")
        trace.flush(conn, p, row[0] if row else None)
    misfits = [
        r[0]
        for r in conn.execute("SELECT id FROM postings WHERE status='new' AND level_fit='misfit'")
    ]
    conn.execute(
        "UPDATE postings SET status='archived', "
        "notes='auto-archived: level misfit for profile' "
        "WHERE status='new' AND level_fit='misfit'"
    )
    for pid in misfits:
        trace.record(conn, pid, "status", "archived", why="level misfit for profile")
    conn.commit()
    stats["prompt_tokens"] = usage.prompt_tokens
    stats["completion_tokens"] = usage.completion_tokens
    stats["est_cost_usd"] = usage.est_cost_usd
    _timed(stats, "output", t0, written=len(state["scored"]))
    log.info(
        "run complete: %s new, %s excluded, cost $%s",
        stats.get("new_postings"),
        stats.get("excluded_count"),
        usage.est_cost_usd,
    )
    return state
