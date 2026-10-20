"""Score unscored listings by reading their own page (Ashby, Workday and
Oracle through their APIs, any other host as plain HTML).

  python -m sourcer.score_listings [--limit N] [--workers N]

Fetches each posting's own page (Greenhouse and Lever render server-side,
Ashby is resolved through its posting API), parses and scores it through the
normal pipeline path, and updates the row. Newest first, internships
prioritised. Respects SOURCER_COST_CAP.

Before any fetch or model call, the company's own board is asked whether the
role is still listed and when it first went up. A listing can be new to the
community list yet months old on the board; those are excluded unscored.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import anthropic
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from . import config, db, llm, log, net, trace
from .apply import recheck
from .apply.verify import WORKDAY_PULLED, WORKDAY_RE, workday_board_answers
from .config import Profile
from .pipeline.record import judge, read_columns, save_score
from .sources import oracle

logger = log.get_logger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (compatible; sourcer)"}
PAGE_TIMEOUT = 15

# Ashby renders its posting pages client-side: fetching the HTML yields ~80
# characters of shell and never the description, so every Ashby row failed the
# readability check and was retried on the next invocation forever. Its public
# posting API returns the same descriptions sources.py already ingests, so
# Ashby urls are resolved through that instead of scraped.
ASHBY_URL_RE = re.compile(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{8,})", re.I)
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{org}"
MIN_DESCRIPTION_CHARS = 400
_ashby_boards: dict[str, list[dict[str, Any]]] = {}
# (reason to hold back, what the board said); a seam so tests stay offline
RECHECK = recheck.why_not_send

SELECT_UNSCORED = (
    "SELECT id, company, title, url, location, employment_type, start_date, posted_at, source "
    # a company board row normally arrives with its text and is scored on the
    # way in; one whose board gave no text (Goldman's Oracle site) waits here
    "FROM postings WHERE source IN ('simplify', 'ats') AND status='new' AND score IS NULL "
    # any host: Ashby, Workday and Oracle have readers, every other page is
    # read as plain HTML, and a page that will not read is marked and skipped
    "AND (notes IS NULL OR notes NOT LIKE 'listing unreadable:%') "
    "ORDER BY (employment_type='intern') DESC, posted_at DESC LIMIT ?"
)

Fetched = tuple[str, None] | tuple[None, str]
ORACLE_URL_RE = re.compile(
    r"https://([a-z0-9-]+(?:\.[a-z0-9-]+)*\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z-]+/sites/([\w-]+)/"
    r"(?:job|requisitions/preview)/(\d+)"
)
"""Either (description, None) or (None, error)."""


def _ashby_board(org: str) -> list[dict[str, Any]]:
    """One board fetch per org per process; a backlog run hits the same org
    repeatedly and there is no reason to re-download the board each time."""
    if org not in _ashby_boards:
        try:
            resp = net.get(ASHBY_API.format(org=org), timeout=config.REQUEST_TIMEOUT, headers=UA)
            resp.raise_for_status()
            _ashby_boards[org] = resp.json().get("jobs", []) or []
        except (requests.RequestException, ValueError) as exc:
            logger.warning("ashby board %s unavailable: %s", org, exc)
            _ashby_boards[org] = []
    return _ashby_boards[org]


def _plain_text(html: str) -> str:
    text = BeautifulSoup(html, "html.parser").get_text("\n")
    return "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())


def _fetch_ashby(org: str, job_id: str) -> Fetched:
    for j in _ashby_board(org):
        if job_id.lower() in (str(j.get("jobUrl", "")) + str(j.get("id", ""))).lower():
            text = _plain_text(j.get("descriptionPlain") or j.get("descriptionHtml") or "")
            if len(text) < MIN_DESCRIPTION_CHARS:
                return None, "ashby description too short"
            return text[: config.MAX_DESCRIPTION_CHARS], None
    return None, "not on the ashby board (likely closed)"


def _fetch_workday(m: re.Match[str]) -> Fetched:
    """Workday renders in the browser; its career site API returns the text."""
    tenant, wd, site, slug = m.groups()
    try:
        resp = net.get(
            f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{slug}",
            timeout=PAGE_TIMEOUT,
            headers={**UA, "Accept": "application/json"},
        )
        if resp.status_code == WORKDAY_PULLED and workday_board_answers(tenant, wd, site):
            return None, "taken down from the workday board (closed)"
        resp.raise_for_status()
        info = resp.json().get("jobPostingInfo") or {}
    except (requests.RequestException, ValueError) as e:
        return None, f"workday fetch failed: {e}"
    return _long_enough(_plain_text(info.get("jobDescription") or ""), "workday")


def _fetch_oracle(m: re.Match[str]) -> Fetched:
    try:
        detail = oracle._detail(m.group(1), m.group(2), m.group(3))
    except (requests.RequestException, ValueError, KeyError) as e:
        return None, f"oracle fetch failed: {e}"
    return _long_enough(detail["description"], "oracle")


def _long_enough(text: str, where: str) -> Fetched:
    if len(text) < MIN_DESCRIPTION_CHARS:
        return None, f"{where} description too short"
    return text[: config.MAX_DESCRIPTION_CHARS], None


def fetch_description(url: str) -> Fetched:
    """Returns (description, error). Costs an HTTP GET, never an API call."""
    m = ASHBY_URL_RE.search(url or "")
    if m:
        return _fetch_ashby(m.group(1), m.group(2))
    m = WORKDAY_RE.match(url or "")
    if m:
        return _fetch_workday(m)
    m = ORACLE_URL_RE.search(url or "")
    if m:
        return _fetch_oracle(m)
    try:
        resp = net.get(url, timeout=PAGE_TIMEOUT, headers=UA)
        resp.raise_for_status()
    except requests.RequestException as e:
        return None, f"fetch failed: {e}"
    text = _plain_text(resp.text)
    if len(text) < MIN_DESCRIPTION_CHARS:
        return None, "page not readable"
    return text[: config.MAX_DESCRIPTION_CHARS], None


TWIN_COLUMNS = (
    "score",
    "rationale",
    "requirements",
    "tech_stack",
    "seniority",
    "years_experience_min",
    "timing_unclear",
    "excluded",
    "exclusion_reason",
    "score_trace",
)


def apply_twin(conn: db.Connection, row: Any, twin: Any) -> str:
    """Copy an identical role's score across instead of paying for it again."""
    fields = {k: twin[k] for k in TWIN_COLUMNS}
    save_score(conn, row["id"], fields, how="carried over from the same role already scored")
    return f"reused twin score {twin['score']} (no API call)"


def write_excluded(conn: db.Connection, row_id: int, reason: str) -> None:
    save_score(
        conn,
        row_id,
        {
            "score": 0,
            "excluded": True,
            "exclusion_reason": reason,
            "rationale": f"excluded: {reason}",
        },
        how="excluded",
    )


def apply_parsed(
    conn: db.Connection,
    row: Any,
    parsed: dict[str, Any],
    profile: Profile | None,
    description: str | None = None,
) -> str:
    """Apply a parse+score reply to a row: the board's own fields win, then the
    same rules as the pipeline, read with the posting's text (``description``,
    else the row's own)."""
    parsed["title"] = row["title"]
    if row["employment_type"]:
        parsed["employment_type"] = row["employment_type"]
    if row["start_date"]:
        parsed["start_date"] = row["start_date"]
    text = description if description is not None else dict(row).get("description")
    judged = judge(
        parsed,
        profile,
        score=parsed.pop("score"),
        rationale=parsed.pop("rationale"),
        description=text,
    )
    if judged.reason:
        write_excluded(conn, row["id"], judged.reason)
        return f"excluded ({judged.reason})"
    fields = {k: v for k, v in judged.fields.items() if k not in ("level_fit", "employment_type")}
    save_score(
        conn, row["id"], {**fields, **read_columns(parsed)}, how="listing scored from its page"
    )
    return f"scored {judged.fields['score']}"


def hold_back_stale(conn: db.Connection, row: Any, write: bool = True) -> str | None:
    """Ask the company's board before anything is spent on a listing. A role
    the board no longer lists, or whose program dates have passed, is excluded
    unscored; returns why. None means score it. A role still listed is never
    held back for its age (config.is_fresh)."""
    reason, board = RECHECK(dict(row))
    return record_board(conn, row, reason, board) if write else reason


def record_board(conn: db.Connection, row: Any, reason: str | None, board: Any) -> str | None:
    trace.record(
        conn,
        row["id"],
        "board",
        f"closed or stale: {reason}" if reason else "open",
        posted=board.posted,
    )
    if reason:
        write_excluded(conn, row["id"], f"stale: {reason}")
    elif board.posted:  # the board's own date, not the day the list picked it up
        conn.execute("UPDATE postings SET posted_at=? WHERE id=?", (board.posted, row["id"]))
    return reason


def score_one(
    conn: db.Connection,
    client: anthropic.Anthropic,
    usage: llm.Usage,
    row: Any,
    *,
    skills: dict[str, Any],
    profile: Profile | None,
) -> str:
    twin = db.find_scored_twin(conn, row["company"], row["title"], llm.current_twin(skills))
    looked = look(client, usage, row, skills, twin is not None)
    return finish(conn, row, looked, twin, profile)


def look(
    client: anthropic.Anthropic,
    usage: llm.Usage,
    row: Any,
    skills: dict[str, Any],
    has_twin: bool,
) -> dict[str, Any]:
    """The network half of scoring a listing: the company's board, the page, the
    model. Touches no database, so it is safe on a worker thread."""
    reason, board = RECHECK(dict(row))
    out: dict[str, Any] = {"stale": reason, "board": board}
    if reason or has_twin:
        return out
    text, why = fetch_description(row["url"])
    out.update(text=text, why=why)
    if text is None:
        return out
    raw = {
        "company": row["company"],
        "title": row["title"],
        "location": row["location"] or "",
        "url": row["url"],
        "description": text,
    }
    out["parsed"], out["err"] = llm.parse_and_score(client, raw, skills, usage)
    return out


def finish(
    conn: db.Connection, row: Any, looked: dict[str, Any], twin: Any, profile: Profile | None
) -> str:
    """The storage half, on the thread that owns the connection."""
    stale = record_board(conn, row, looked["stale"], looked["board"])
    if stale:
        return f"held back: {stale}"
    if twin is not None:
        return apply_twin(conn, row, twin)
    if looked["text"] is None:
        # remembered, so an unreadable page is not fetched again every run
        conn.execute(
            "UPDATE postings SET notes=? WHERE id=?",
            (f"listing unreadable: {looked['why']}", row["id"]),
        )
        return str(looked["why"])
    conn.execute(
        "UPDATE postings SET description=? WHERE id=? AND (description IS NULL OR description='')",
        (looked["text"], row["id"]),
    )
    parsed, err = looked["parsed"], looked["err"]
    if err or parsed is None:
        return f"parse+score: {err}"
    return apply_parsed(conn, row, parsed, profile, looked["text"])


# Listings the title check has not judged yet. A title she does not want is
# archived under the rescreen note, so ``rescreen --undo`` brings it back.
TITLE_NOTE = "auto-archived: rescreen: title check: not the work she wants"
UNCHECKED = (
    "SELECT id, title FROM postings p WHERE source='simplify' AND status='new' "
    "AND excluded=0 AND score IS NULL AND NOT EXISTS (SELECT 1 FROM posting_events e "
    "WHERE e.posting_id=p.id AND e.stage='title_check')"
)


def check_titles(conn: db.Connection, client: anthropic.Anthropic, usage: llm.Usage) -> int:
    """Run the profile's title check over listings not yet judged, in her
    words rather than a list of role names. Returns how many were archived."""
    trace.ensure(conn)
    rows = conn.execute(UNCHECKED).fetchall()
    if not rows:
        return 0
    kept = set(llm.triage_titles(client, [r["title"] for r in rows], usage))
    for i, r in enumerate(rows):
        if i not in kept:
            conn.execute(
                "UPDATE postings SET status='archived', status_updated=?, notes=? WHERE id=?",
                (db.now_iso(), TITLE_NOTE, r["id"]),
            )
        trace.record(conn, r["id"], "title_check", "kept" if i in kept else "dropped")
    conn.commit()
    return len(rows) - len(kept)


def _wide(row: Any) -> bool:
    return config.starts_in_wide_window({"title": row["title"], "start_date": row["start_date"]})


def select_rows(conn: db.Connection, limit: int) -> list[Any]:
    rows = conn.execute(SELECT_UNSCORED, (limit * 4,)).fetchall()
    # never spend on rows that fail the location, level, or freshness policy;
    # which titles fit is the title check's call, not a list of role names
    keep = [
        r
        for r in rows
        if config.location_in_scope(r["location"] or "", wide=_wide(r))
        and not config.title_location_conflict(r["title"], _wide(r))
        and not config.is_senior_title(r["title"])
        and config.is_fresh(r["posted_at"], listed_on_board=r["source"] == "ats")
    ]
    return keep[:limit]


def score_pending(
    conn: db.Connection,
    client: anthropic.Anthropic,
    usage: llm.Usage,
    *,
    skills: dict[str, Any],
    profile: Profile | None,
    limit: int,
) -> int:
    """Score up to ``limit`` listings while the shared budget allows. Returns the count."""
    check_titles(conn, client, usage)
    done = 0
    for row in select_rows(conn, limit):
        if usage.est_cost_usd >= config.COST_CAP_USD:
            break
        try:
            result = score_one(conn, client, usage, row, skills=skills, profile=profile)
        except RuntimeError as exc:  # one failed model call skips that listing, never the run
            logger.warning("listing %s / %s not scored: %s", row["company"], row["title"][:50], exc)
            continue
        conn.commit()
        done += 1
        logger.info("listing %s / %s: %s", row["company"], row["title"][:50], result)
    return done


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    log.configure()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--workers", type=int, default=1, help="listings read and scored at a time")
    args = ap.parse_args(argv)
    conn = db.connect()
    client = llm.get_client()
    usage = llm.Usage()
    archived = check_titles(conn, client, usage)
    if archived:
        print(f"title check archived {archived} listings (rescreen --undo restores them)")
    rows = select_rows(conn, args.limit)
    if not rows:
        print("nothing to score")
        return 0
    skills = json.loads(config.SKILLS_PATH.read_text())
    profile = config.load_profile()
    current = llm.current_twin(skills)
    twins = {r["id"]: db.find_scored_twin(conn, r["company"], r["title"], current) for r in rows}

    def one(r: Any) -> dict[str, Any] | None:
        if usage.est_cost_usd >= config.COST_CAP_USD:
            return None
        try:
            return look(client, usage, r, skills, twins[r["id"]] is not None)
        except (anthropic.APIError, RuntimeError, OSError) as exc:
            # a failed call costs this listing, never the run
            return {"error": str(exc)[:200]}

    done = 0
    with ThreadPoolExecutor(max(1, args.workers)) as pool:
        for row, looked in zip(rows, pool.map(one, rows), strict=True):
            if looked is None:
                print(f"cost cap reached after {done}")
                break
            done += 1
            if "error" in looked:
                result = f"failed: {looked['error']}"
            else:
                result = finish(conn, row, looked, twins[row["id"]], profile)
                conn.commit()
            print(f"[{done}/{len(rows)}] {row['company']} - {row['title'][:50]}: {result}")
    print(f"done: {done} listings, cost ${usage.est_cost_usd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
