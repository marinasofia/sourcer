"""A collection run in two processes, so the one that browses never holds a model login.

  python -m sourcer.main fetch --out fetched.json   boards and careers pages, the rules, no model
  python -m sourcer.main read --in fetched.json     title check, read, score and save, no browser

``fetch`` runs the source stage with no model client and keeps the titles the
rules could not settle (they need the model's title check) instead of dropping
them. It writes the postings as plain JSON. ``read`` checks that file field by
field, since it came from the process that opened outside pages, then runs the
title check and the rest of the pipeline (dedupe, parse, score, output) exactly
as one ``collect`` run would. The cloud runs them as separate jobs (Nov 22 2026):
a page that broke out of the browser finds no Claude token to take.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import config, db, progress, trace
from .pipeline.dedupe import dedupe_node
from .pipeline.output import output_node
from .pipeline.scoring import parse_node, score_node
from .pipeline.source import _triage, source_node
from .pipeline.state import Posting, State

FORMAT = 1
MAX_POSTINGS = 200_000
MAX_TEXT = 200_000  # characters in one field
_SCALARS = (str, int, float, bool, type(None))


def fetch(out: Path, state: State | None = None) -> dict[str, Any]:
    """Run the source stage with no model and write what it kept to ``out``."""
    from .main import build_state  # noqa: PLC0415

    state = state or build_state(client=False)  # False: never a client, whatever the cap
    state["client"] = None
    state["defer_triage"] = True
    t0 = time.time()
    state = source_node(state)
    stats = {**state["stats"], "fetch_s": round(time.time() - t0, 1)}
    body = {
        "format": FORMAT,
        "postings": state.get("postings", []),
        "unsure": state.get("unsure", []),
        "stats": stats,
    }
    out.write_text(json.dumps(body, default=str))
    return body


def _field_ok(value: Any) -> bool:
    if isinstance(value, str):
        return len(value) <= MAX_TEXT
    return isinstance(value, _SCALARS)


def _events_ok(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(e, dict) and all(isinstance(k, str) for k in e) for e in value
    )


def _value_ok(key: Any, value: Any) -> bool:
    if not isinstance(key, str):
        return False
    if key == "_events":
        return _events_ok(value)
    return _field_ok(value) or (isinstance(value, list) and all(isinstance(v, str) for v in value))


def clean_posting(p: Any) -> Posting | None:
    """A fetched posting as the read stage takes it: a dict of plain values with
    a name, a title and a public https link; None for anything else."""
    if not isinstance(p, dict) or not all(
        isinstance(p.get(k), str) and p[k].strip() for k in ("company", "title", "url")
    ):
        return None
    link = urlparse(p["url"])
    if link.scheme != "https" or not link.hostname or link.username or link.password:
        return None
    if not all(_value_ok(k, v) for k, v in p.items()):
        return None
    return dict(p)


def load(path: Path) -> tuple[list[Posting], list[Posting], dict[str, Any]]:
    """The fetch stage's file, checked. Raises ValueError on a wrong format."""
    body = json.loads(path.read_text())
    if not isinstance(body, dict) or body.get("format") != FORMAT:
        raise ValueError("not a fetch file of this format")
    postings, unsure = body.get("postings"), body.get("unsure")
    if not isinstance(postings, list) or not isinstance(unsure, list):
        raise ValueError("fetch file has no posting lists")
    if len(postings) + len(unsure) > MAX_POSTINGS:
        raise ValueError("fetch file has too many postings")
    stats = body.get("stats") if isinstance(body.get("stats"), dict) else {}
    keep = [c for c in (clean_posting(p) for p in postings) if c is not None]
    maybe = [c for c in (clean_posting(p) for p in unsure) if c is not None]
    stats = {k: v for k, v in stats.items() if isinstance(k, str)}
    stats["fetch_rejected"] = len(postings) + len(unsure) - len(keep) - len(maybe)
    return keep, maybe, stats


def read(path: Path, state: State | None = None) -> State:
    """Title check, read, score and save the fetch stage's postings."""
    from .main import _after_collect, build_state  # noqa: PLC0415

    t0 = time.time()
    postings, unsure, stats = load(path)
    state = state or build_state()
    state["stats"] = stats
    progress.stage("triage", done=0, total=len(unsure))
    triaged_in = _triage(state, unsure, postings)
    progress.stage("triage", done=len(unsure), status="done")
    stats["triaged_in"] = triaged_in
    stats["prefiltered_out"] = int(stats.get("prefiltered_out") or 0) + len(unsure) - triaged_in
    reasons = stats.get("drop_reasons") if isinstance(stats.get("drop_reasons"), dict) else {}
    reasons["triage_rejected"] = len(unsure) - triaged_in
    stats["drop_reasons"] = reasons
    state["postings"] = postings
    for node in (dedupe_node, parse_node, score_node, output_node):
        state = node(state)
    _after_collect(state)
    stats["duration_s"] = round(float(stats.get("fetch_s") or 0) + time.time() - t0, 1)
    stats["run_id"] = trace.RUN_ID
    db.insert_run_log(state["conn"], stats)
    return state


__all__ = ["clean_posting", "config", "fetch", "load", "read"]
