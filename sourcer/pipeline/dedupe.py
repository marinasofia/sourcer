"""Dedupe node: drop postings whose hash is already stored, and repeats within the run."""

from __future__ import annotations

import time

from .. import db, progress
from .state import Posting, State, _timed


def dedupe_node(state: State) -> State:
    t0 = time.time()
    stats = state["stats"]
    seen = db.seen_identities(state["conn"])
    fresh: list[Posting] = []
    ordered = sorted(state["postings"], key=lambda posting: posting.get("source") == "simplify")
    for p in ordered:
        p["hash"] = db.posting_hash(p["company"], p["title"], p.get("location") or "")
        prior_source = seen.get(p["hash"])
        source = p.get("source", "ats")
        if prior_source is None or (prior_source != "ats" and source != "simplify"):
            seen[p["hash"]] = source
            fresh.append(p)
    stats["dedupe_count"] = len(state["postings"]) - len(fresh)
    stats["new_postings"] = len(fresh)
    _timed(stats, "dedupe", t0, new=len(fresh), duplicates=stats["dedupe_count"])
    state["new_postings"] = fresh
    progress.stage("dedupe", done=len(fresh), total=len(state["postings"]), status="done")
    return state
