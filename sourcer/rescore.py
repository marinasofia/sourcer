"""Re-score stored postings against the candidate's current evidence (or the
skills vector when there is no evidence file).

  python -m sourcer.rescore          # every scored posting
  python -m sourcer.rescore --open   # only postings still new or shortlisted
  python -m sourcer.rescore --open --workers 6   # six model calls at a time
  python -m sourcer.rescore --open --stale       # only scores an older scorer made

Use after editing skills_vector.json. Reads the already-parsed fields from
the database, so nothing is re-scraped or re-parsed; only the score call
runs. Hard-excluded postings stay excluded (the exclusion rules do not
depend on the vector). Respects SOURCER_COST_CAP.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import anthropic
from dotenv import load_dotenv

from . import config, db, llm, log
from .pipeline.record import judge, save_score

logger = log.get_logger(__name__)

SELECT_SCORED = (
    "SELECT id, title, requirements, tech_stack, seniority, employment_type, start_date, "
    "description, score, score_trace FROM postings WHERE excluded=0 AND score IS NOT NULL"
)
OPEN_ONLY = " AND status IN ('new', 'shortlist')"


def rescore_row(
    conn: db.Connection,
    client: anthropic.Anthropic,
    usage: llm.Usage,
    row: Any,
    skills: dict[str, Any],
) -> bool:
    """Score one stored row again. Returns False when the model reply failed validation."""
    return write_row(conn, row, *score_row(client, usage, row, skills))


def score_row(
    client: anthropic.Anthropic, usage: llm.Usage, row: Any, skills: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The model half of a rescore; safe on a worker thread."""
    parsed = {
        "title": row["title"],
        "requirements": json.loads(row["requirements"] or "[]"),
        "tech_stack": json.loads(row["tech_stack"] or "[]"),
        "seniority": row["seniority"],
        "employment_type": row["employment_type"],
        "start_date": row["start_date"],
        "description": dict(row).get("description"),
    }
    data, err = llm.score_posting(client, parsed, skills, usage)
    if err or data is None:
        logger.warning("rescore failed for id=%s: %s", row["id"], err)
        return parsed, None
    return parsed, data


def write_row(
    conn: db.Connection, row: Any, parsed: dict[str, Any], data: dict[str, Any] | None
) -> bool:
    """The storage half of a rescore, on the thread that owns the connection.
    Only the score, its timing note and its trace change: the exclusion rules
    do not depend on the evidence, so a rescore never excludes."""
    if data is None:
        return False
    judged = judge(parsed, None, score=data["score"], rationale=data["rationale"], rules=())
    fields = {k: judged.fields[k] for k in ("score", "rationale", "timing_unclear")}
    fields["score_trace"] = json.dumps(data.get("score_trace"))
    save_score(conn, row["id"], fields, how="scored again", was=dict(row).get("score"))
    return True


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    log.configure()
    args = sys.argv[1:] if argv is None else argv
    only_open = "--open" in args
    # model calls side by side (the subscription CLI is slow per call); the
    # database is written on this thread only
    workers = int(args[args.index("--workers") + 1]) if "--workers" in args else 1
    t0 = time.time()
    conn = db.connect()
    skills = json.loads(config.SKILLS_PATH.read_text())
    client = llm.get_client()
    usage = llm.Usage()
    rows = conn.execute(SELECT_SCORED + (OPEN_ONLY if only_open else "")).fetchall()
    if "--stale" in args:
        # a score whose trace shows another prompt, evidence, model or arithmetic
        basis = llm.score_basis(skills)
        why = {r["id"]: llm.stale_reason(r["score_trace"], basis) for r in rows}
        print("stale scores:", dict(Counter(w for w in why.values() if w)) or "none")
        rows = [r for r in rows if why[r["id"]]]

    def one(r: Any) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
        if usage.est_cost_usd >= config.COST_CAP_USD:
            return None
        try:
            return score_row(client, usage, r, skills)
        except (anthropic.APIError, RuntimeError) as exc:
            # a failed model call costs this posting, never the run
            logger.warning("rescore failed for posting %s: %s", r["id"], exc)
            return {}, None

    updated = failures = skipped_cap = 0
    with ThreadPoolExecutor(max(1, workers)) as pool:
        for r, res in zip(rows, pool.map(one, rows), strict=True):
            if res is None:
                skipped_cap += 1
            elif write_row(conn, r, *res):
                updated += 1
                conn.commit()  # a crash midway keeps every score already made
            else:
                failures += 1
    conn.commit()
    print(
        f"rescored {updated}/{len(rows)} postings "
        f"(failures={failures}, skipped_by_cost_cap={skipped_cap}) "
        f"in {round(time.time() - t0, 1)}s, "
        f"tokens {usage.prompt_tokens}/{usage.completion_tokens}, "
        f"cost ${usage.est_cost_usd}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
