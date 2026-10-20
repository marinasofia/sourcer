"""Scoring: the parse and score nodes, plus the service shared by HTTP and MCP.

parse: a senior title is excluded from the title alone, an already scored
twin transfers its score, and what remains goes to the model in one call
that parses and scores. score: deterministic exclusions, the timing flag,
and level fit (see ``filters``).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import anthropic

from .. import config, db, llm, progress, trace
from ..eligibility import EXCLUSION_RULES, eligible
from ..log import get_logger
from .record import classify, judge
from .state import Posting, State, _cost_cap_reached, _timed

log = get_logger(__name__)

# a scored posting at or above this shows in the live feed as a match
SHOW_IN_FEED = 75


MIN_TEXT = 400  # characters of posting text before a score means anything


def _has_text(p: Posting) -> bool:
    return len((p.get("description") or "").strip()) >= MIN_TEXT


def _read_posting(state: State, url: str) -> str | None:
    """The posting's own page as text (an HTTP GET, never a model call). The
    reader comes in the state: page fetching lives in a higher layer."""
    read = state.get("read_posting")
    return read(url) if read else None


def _listing_record(p: Posting) -> Posting:
    """A SimplifyJobs listing has no text to parse; it bypasses the model."""
    return {
        "company": p["company"],
        "title": p["title"],
        "url": p["url"],
        "location": p["location"],
        "hash": p["hash"],
        "requirements": [],
        "tech_stack": [],
        "seniority": "unspecified",
        "employment_type": p.get("employment_type"),
        "start_date": p.get("start_date"),
        "years_experience_min": None,
        "source": "simplify",
        "posted_at": p.get("posted_at"),
    }


def _senior_record(p: Posting) -> Posting:
    """A senior title is excluded from the title alone; parsing it would pay to learn nothing.

    The minimal record keeps the exclusion logged and the hash deduped.
    """
    return {
        "company": p["company"],
        "title": p["title"],
        "url": p["url"],
        "location": p["location"],
        "hash": p["hash"],
        "posted_at": p.get("posted_at"),
        "source": "ats",
        "requirements": [],
        "tech_stack": [],
        "seniority": "senior",
        "employment_type": "unspecified",
        "start_date": None,
        "years_experience_min": None,
        "work_mode": "unspecified",
        "score": 0,
        "rationale": "",
    }


def _twin_record(p: Posting, twin: Any) -> Posting:
    """One role under several urls: the already scored twin transfers exactly."""
    return {
        "company": p["company"],
        "title": p["title"],
        "url": p["url"],
        "location": p["location"],
        "hash": p["hash"],
        "posted_at": p.get("posted_at"),
        "source": "ats",
        "requirements": json.loads(twin["requirements"] or "[]"),
        "tech_stack": json.loads(twin["tech_stack"] or "[]"),
        "seniority": twin["seniority"] or "unspecified",
        "employment_type": p.get("employment_type") or "unspecified",
        "start_date": p.get("start_date"),
        "years_experience_min": twin["years_experience_min"],
        "work_mode": "unspecified",
        "score": twin["score"],
        "rationale": twin["rationale"],
        # the twin's own trace, so the copy can be traced and judged current
        "score_trace": {**json.loads(twin["score_trace"] or "{}"), "reused_from": twin["id"]},
    }


def _score_event(p: Posting, data: dict[str, Any]) -> None:
    """The score on the timeline, with the rule and arithmetic of the read kept."""
    t = data.get("score_trace") or {}
    reads = t.get("reads") or [{}]
    kept = reads[min(t.get("kept", 0), len(reads) - 1)] or {}
    trace.event(p, "score", data.get("score"), rule=t.get("rule"), formula=kept.get("formula"))
    data["_events"] = p.pop("_events", [])


def _traced_twin(p: Posting, twin: Any) -> Posting:
    """The twin's score carried over, with that on the timeline."""
    trace.event(p, "score", twin["score"], how="carried over from the same role already scored")
    record = _twin_record(p, twin)
    record["_events"] = p.pop("_events", [])
    return record


def _set_aside(state: State, p: Posting, verdict: str, **detail: Any) -> None:
    """A posting not scored this run: its timeline says why, under its address."""
    trace.event(p, "score", verdict, **detail)
    trace.flush(state["conn"], p, None)


def reading_order(postings: list[Posting]) -> list[Posting]:
    """The order the read budget is spent in, so the jobs most worth reading
    come first: posted most recently, in NYC or remote before elsewhere, one
    company's roles interleaved with the rest instead of filling the budget,
    and a title repeated across cities read once before its copies."""

    def freshness(p: Posting) -> int:
        age = config.posting_age_days(p.get("posted_at"))
        if age is None:
            return 2
        return 0 if age <= 3 else 1 if age <= 14 else 2 if age <= 45 else 3

    def stem(p: Posting) -> str:
        # "Chemistry - AI Training - Charlotte, US" and "... - Phoenix" share a stem
        return re.split(r"\s+[-|(]\s*|,", p["title"])[0].strip().lower()

    turn: dict[str, int] = {}
    copies: set[tuple[str, str]] = set()
    keyed = []
    for p in sorted(postings, key=freshness):
        company = (p.get("company") or "").lower()
        repeat = (company, stem(p)) in copies
        copies.add((company, stem(p)))
        turn[company] = turn.get(company, 0) + (0 if repeat else 1)
        metro = config.location_in_scope(p.get("location") or "", p.get("description") or "")
        keyed.append(
            (
                (
                    freshness(p),
                    repeat,
                    not metro,
                    turn[company],
                ),
                p,
            )
        )
    return [p for _, p in sorted(keyed, key=lambda kp: kp[0])]


def parse_node(state: State) -> State:
    t0 = time.time()
    stats = state["stats"]
    parsed_list: list[Posting] = []
    failures: list[dict[str, str]] = []
    skipped_cost_cap = no_text = 0
    progress.stage("read", done=0, total=len(state["new_postings"]))
    state["new_postings"] = reading_order(state["new_postings"])
    skipped_fresh = read_by_model = 0
    for n, p in enumerate(state["new_postings"], 1):
        progress.stage("read", done=n - 1, item=f"{p['company']}, {p['title']}")
        if p.get("source") == "simplify":
            parsed_list.append(_listing_record(p))
            continue
        if config.is_senior_title(p["title"]):
            parsed_list.append(_senior_record(p))
            continue
        if not _has_text(p):
            # never score a title alone: the model guesses, and a 3+ years role
            # with a clearance rule scored 92. Read the posting, or keep it unscored.
            p["description"] = _read_posting(state, p["url"]) or p.get("description")
            if not _has_text(p):
                no_text += 1
                parsed_list.append(
                    {**_listing_record(p), "source": p.get("source") or "ats", "no_text": True}
                )
                continue
        # a twin from an older scorer is read again, never copied
        twin = db.find_scored_twin(
            state["conn"], p["company"], p["title"], llm.current_twin(state["skills"])
        )
        if twin is not None and not twin["excluded"]:
            parsed_list.append(_traced_twin(p, twin))
            continue
        usage = state["usage"]
        client = state.get("client")
        if client is None or _cost_cap_reached(usage):
            skipped_cost_cap += 1
            age = config.posting_age_days(p.get("posted_at"))
            skipped_fresh += age is not None and age <= 3
            _set_aside(state, p, "skipped: spending cap reached", place=n)
            continue
        try:
            data, err = llm.parse_and_score(client, p, state["skills"], usage)
        except (anthropic.APIError, RuntimeError) as exc:
            # a timed out or failed model call costs this posting, never the run
            data, err = None, f"model call failed: {exc}"
        if err or data is None:
            log.warning("parse failed for %s / %s: %s", p["company"], p["title"], err)
            failures.append(
                {"company": p["company"], "title": p["title"], "url": p["url"], "error": err or ""}
            )
            _set_aside(state, p, "failed", error=err or "")
            continue
        # trust the board's own title/location over the model's echo
        data.update(
            company=p["company"],
            url=p["url"],
            hash=p["hash"],
            title=p["title"],
            location=p["location"],
            posted_at=p.get("posted_at"),
            source="ats",
            description=p.get("description"),  # the graduation and eligibility rules read it
        )
        _score_event(p, data)
        read_by_model += 1
        parsed_list.append(data)
    progress.stage("read", done=len(state["new_postings"]), status="done")
    stats.update(
        parse_failures=len(failures),
        parse_failure_detail=failures,
        skipped_cost_cap=skipped_cost_cap,
        # posted in the last 3 days: what the cap really costs
        skipped_cost_cap_fresh=skipped_fresh,
        skipped_no_text=no_text,
    )
    _timed(
        stats,
        "parse",
        t0,
        parsed=len(parsed_list),
        failures=len(failures),
        read_by_model=read_by_model,
        skipped_cost_cap=skipped_cost_cap,
        skipped_cost_cap_fresh=skipped_fresh,
    )
    state["parsed"] = parsed_list
    return state


def score_node(state: State) -> State:
    t0 = time.time()
    stats = state["stats"]
    scored: list[Posting] = []
    exclusions: list[dict[str, str]] = []
    profile = state.get("profile")
    for parsed in state["parsed"]:
        if parsed.pop("no_text", False) or parsed.get("source") == "simplify":
            parsed.update(classify(parsed, profile))
            trace.event(parsed, "level", parsed["level_fit"])
            # listing-only record: no text, no score; kept for alerts/tracker
            parsed.update(
                score=None,
                rationale=None,
                timing_unclear=False,
                excluded=False,
                exclusion_reason=None,
            )
            scored.append(parsed)
            continue
        # the score arrived with the combined parse call; no second model call
        judged = judge(
            parsed, profile, score=parsed.pop("score"), rationale=parsed.pop("rationale")
        )
        parsed.update(judged.fields)
        reason = judged.reason
        trace.event(parsed, "level", parsed["level_fit"])
        trace.event(parsed, "rules", f"excluded: {reason}" if reason else "passed")
        if reason:
            exclusions.append(
                {
                    "company": parsed["company"],
                    "title": parsed["title"],
                    "url": parsed["url"],
                    "reason": reason,
                }
            )
            scored.append(parsed)
            continue
        if judged.timing:
            trace.event(parsed, "timing", "unclear", note=judged.timing)
        scored.append(parsed)
        if parsed["score"] >= SHOW_IN_FEED:
            progress.event(f"{parsed['score']}% {parsed['title']} at {parsed['company']}", "match")
    stats["score_failures"] = 0  # scoring is part of the parse call; failures are counted there
    stats["score_failure_detail"] = []
    stats["excluded_count"] = len(exclusions)
    stats["exclusions"] = exclusions
    stats["timing_unclear_count"] = sum(1 for s in scored if s.get("timing_unclear"))
    _timed(stats, "score", t0, scored=len(scored), excluded=len(exclusions))
    state["scored"] = scored
    return state


# ------------------------------------------------ shared service for HTTP and MCP


def validate_input(posting_text: str, title: str, company: str) -> None:
    """Reject malformed requests before allocating a model client or reading skills."""
    if not isinstance(posting_text, str) or not posting_text.strip():
        raise ValueError("posting_text must be a nonempty string")
    if not isinstance(title, str) or not isinstance(company, str):
        raise ValueError("title and company must be strings")
    if len(posting_text) > config.MAX_DESCRIPTION_CHARS:
        raise ValueError("posting_text exceeds the configured character limit")


def score_text(
    posting_text: str, title: str = "", company: str = "", usage: llm.Usage | None = None
) -> dict[str, Any]:
    """Score once using the same input, exclusion, and budget rules on both interfaces.
    A long running service passes its one ``usage`` so the cost cap bounds its
    total spend, not each request."""
    validate_input(posting_text, title, company)
    usage = usage if usage is not None else llm.Usage()
    if not usage.can_call():
        return {"error": "model budget exhausted or invalid", "est_cost_usd": 0.0}
    skills = json.loads(config.SKILLS_PATH.read_text())
    client = llm.get_client()
    raw = {
        "company": company or "unknown",
        "title": title or "untitled",
        "location": "",
        "url": "",
        "description": posting_text,
    }
    parsed, error = llm.parse_posting(client, raw, usage)
    if error or parsed is None:
        return {"error": f"parse failed: {error}", "est_cost_usd": usage.est_cost_usd}
    # Model output is a summary, not a substitute for the original eligibility text.
    parsed["description"] = posting_text
    if title:
        parsed["title"] = title
    if company:
        parsed["company"] = company
    profile = config.load_profile()
    parsed.update(classify(parsed, profile))
    reason = eligible(parsed, profile, EXCLUSION_RULES).reason or None
    if reason:
        return {
            "score": 0,
            "excluded": True,
            "exclusion_reason": reason,
            "parsed": parsed,
            "est_cost_usd": usage.est_cost_usd,
        }
    scored, error = llm.score_posting(client, parsed, skills, usage)
    if error or scored is None:
        return {"error": f"score failed: {error}", "est_cost_usd": usage.est_cost_usd}
    return {
        "score": int(scored["score"]),
        "rationale": scored["rationale"],
        "excluded": False,
        "parsed": parsed,
        "est_cost_usd": usage.est_cost_usd,
    }
