"""Source node: fetch every board, prefilter, triage regex misses, add community listings.

Pipeline order: source -> dedupe -> parse -> score -> output. Dedupe runs
BEFORE parse so already-seen postings never cost API tokens.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from .. import db, llm, progress, sources, trace
from ..log import get_logger
from .state import Posting, State, _cost_cap_reached, _timed

log = get_logger(__name__)

# careers_page.py raises this for sites that only render in a browser
BROWSER_OFF = "browser fallback is disabled"
# boards that could not be reached get one more try after this pause: a Mac
# just woken at 9:00 often has no network for the first minute
RETRY_PAUSE_S = float(os.environ.get("SOURCER_FETCH_RETRY_PAUSE", "30"))


def _why(exc: Exception) -> str:
    """A few plain words for the feed: why a board failed this time."""
    if isinstance(exc, requests.Timeout):
        return "timed out"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, requests.ConnectionError):
        return "could not connect"
    return type(exc).__name__


def source_node(state: State) -> State:
    t0 = time.time()
    stats = state.setdefault("stats", {})
    postings: list[Posting] = []
    unsure: list[Posting] = []
    failures: list[str] = []
    prefiltered = seen = 0
    reasons: Counter[str] = Counter()
    workers = max(1, int(os.environ.get("SOURCER_FETCH_WORKERS", "8")))
    companies = state["companies"]
    results = _fetch_all(companies, state.get("profile"), workers)
    needs_browser: list[str] = []
    for entry, result in zip(state["companies"], results, strict=True):
        if isinstance(result, Exception):
            # a dead board must not kill the run; it is counted and reported
            log.warning("source fetch failed for %s: %s", entry.get("company"), result)
            failures.append(f"{entry.get('company')}: {result}")
            if BROWSER_OFF in str(result):  # expected every run, not a board that failed today
                needs_browser.append(str(entry.get("company")))
            else:
                company = entry.get("company")
                progress.event(f"{company}: board did not answer ({_why(result)})", "warn")
            continue
        kept, maybe, dropped, counts, _ = result
        reasons.update(counts)
        postings.extend(kept)
        unsure.extend(maybe)
        prefiltered += dropped
        seen += len(kept) + len(maybe) + dropped
    if needs_browser:
        progress.event(
            f"Skipped {len(needs_browser)} careers sites that need a browser to read: "
            + ", ".join(sorted(needs_browser)),
            "info",
        )
    progress.stage("fetch", status="done")
    progress.stage("triage", done=0, total=len(unsure))
    # the gates' set asides from every board, then the title check's, are written together
    triaged_in = _triage(
        state, unsure, postings, [d for r in results if isinstance(r, tuple) for d in r[4]]
    )
    progress.stage("triage", done=len(unsure), status="done")
    prefiltered += len(unsure) - triaged_in
    stats["companies_covered"] = len(state["companies"]) - len(failures)
    listings = _fetch_listings(failures)
    stats["postings_seen"] = seen
    stats["prefiltered_out"] = prefiltered
    stats["listings_seen"] = len(listings)
    stats["triaged_in"] = triaged_in
    stats["source_failures"] = failures
    reasons["triage_rejected"] += len(unsure) - triaged_in
    stats["drop_reasons"] = dict(reasons)
    _timed(
        stats,
        "source",
        t0,
        fetched=seen,
        kept=len(postings),
        triaged_in=triaged_in,
        listings=len(listings),
        fetch_failures=len(failures),
        drop_reasons=dict(reasons),
    )
    state["postings"] = postings + listings
    return state


def _fetch_all(companies: list[dict[str, Any]], profile: Any, workers: int) -> list[Any]:
    """Every board's result in company order; unreachable ones get a second try."""
    results: list[Any] = [None] * len(companies)
    progress.stage("fetch", done=0, total=len(companies))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, entry, profile): i for i, entry in enumerate(companies)}
        for n, future in enumerate(as_completed(futures), 1):
            i = futures[future]
            results[i] = future.result()
            progress.stage("fetch", done=n, item=companies[i].get("company"))
    _retry_unreachable(companies, results, profile, workers)
    return results


def _retry_unreachable(
    companies: list[dict[str, Any]], results: list[Any], profile: Any, workers: int
) -> None:
    """Fetch once more every board that failed to connect or timed out; an
    HTTP error answer is the board's own reply and is not tried again."""
    again = [
        i
        for i, r in enumerate(results)
        if isinstance(r, (requests.ConnectionError, requests.Timeout))
    ]
    if not again:
        return
    progress.event(f"Trying {len(again)} boards again that did not answer", "info")
    time.sleep(RETRY_PAUSE_S)
    with ThreadPoolExecutor(max_workers=max(1, workers // 2)) as pool:
        for i, result in zip(
            again, pool.map(lambda i: _fetch_one(companies[i], profile), again), strict=True
        ):
            results[i] = result


def _write_drops(state: State, drops: list[Any]) -> None:
    """Every posting set aside before saving, with its reason; old ones pruned."""
    conn = state.get("conn")
    if conn is None:
        return
    trace.log_dropped(conn, drops)
    trace.prune(conn)
    conn.commit()


def _trace_found(
    entry: dict[str, Any], kept: list[Posting], maybe: list[Posting], gated: list[Any]
) -> list[Any]:
    """Where each posting was found and what the gates said; the dropped ones
    come back to be written under their address."""
    board = f"{entry.get('ats')}/{entry.get('slug')}"
    for p in kept + maybe:
        trace.event(p, "found", board, company=entry.get("company"))
        trace.event(p, "gate", "kept" if any(p is k for k in kept) else "title_unsure")
    return [
        (
            p,
            "gate",
            {
                "verdict": v,
                "board": board,
                "location": p.get("location"),
                **({"evidence": p.pop("_gate_evidence")} if "_gate_evidence" in p else {}),
            },
        )
        for p, v in gated
    ]


def _fetch_one(
    entry: dict[str, Any], profile: dict[str, Any] | None
) -> tuple[list[Posting], list[Posting], int, Counter[str], list[Any]] | Exception:
    """Fetch one board on a worker thread, with its own drop counter.

    Boards are independent hosts, so fetching them side by side turns a run
    bounded by the slowest boards in sequence into one bounded by the slowest
    board. Expected failures come back as values so results keep their order.
    """
    counts: Counter[str] = Counter()
    gated: list[Any] = []
    try:
        kept, maybe, dropped = sources.fetch_company(
            entry, profile=profile, reasons=counts, dropped=gated
        )
    except (requests.RequestException, ValueError, KeyError) as exc:
        return exc
    return kept, maybe, dropped, counts, _trace_found(entry, kept, maybe, gated)


def _stored(p: Posting, seen: dict[str, Any]) -> bool:
    key = db.posting_hash(p.get("company") or "", p["title"], p.get("location") or "")
    return seen.get(key) == "ats"


def _triage(
    state: State, unsure: list[Posting], postings: list[Posting], dropped: list[Any] | None = None
) -> int:
    """Send titles through one batched title check; append the matches.

    Returns how many were admitted. A posting already stored skips the check
    and counts as admitted (dedupe drops it next), so a repeat run pays nothing
    for it and the drop counts stay true. A
    missing client or a reached cost cap admits nothing new, and a check
    failure admits every title it held.
    """
    client = state.get("client")
    drops = dropped if dropped is not None else []
    seen = db.seen_identities(state["conn"]) if unsure and state.get("conn") else {}
    stored = [p for p in unsure if _stored(p, seen)]
    postings.extend(stored)
    unsure = [p for p in unsure if not _stored(p, seen)]
    if not unsure or client is None or _cost_cap_reached(state["usage"]):
        why = "no model client" if client is None else "spending cap reached"
        drops.extend((p, "title_check", {"verdict": f"skipped: {why}"}) for p in unsure)
        _write_drops(state, drops)
        return len(stored)
    matched = set(llm.triage_titles(client, [p["title"] for p in unsure], state["usage"]))
    picked = [p for i, p in enumerate(unsure) if i in matched]
    for i, p in enumerate(unsure):
        if i in matched:
            trace.event(p, "title_check", "kept: the model read the title as a target role")
        else:
            drops.append((p, "title_check", {"verdict": "dropped: not a target role by its title"}))
    _write_drops(state, drops)
    postings.extend(picked)
    return len(stored) + len(picked)


def _fetch_listings(failures: list[str]) -> list[Posting]:
    """Metadata-only listings (SimplifyJobs): discovery and alerts, no model calls."""
    if os.environ.get("SOURCER_SIMPLIFY", "1") != "1":
        return []
    try:
        return sources.fetch_simplify()
    except (requests.RequestException, ValueError) as exc:
        log.warning("simplify fetch failed: %s", exc)
        failures.append(f"simplify: {exc}")
        return []
