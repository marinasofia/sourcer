"""Score the unscored-listing backlog through the Message Batches API at half
the per-token price.

  python -m sourcer.batch --dry-run          # build and cost the batch, send nothing
  python -m sourcer.batch --limit 200        # submit, wait, write results back

Batching trades latency for price: the same prompts cost 50% less, and results
come back within the hour rather than in seconds. The backlog scoring job has
no latency requirement, so this is the cheaper way to run it. Page fetching,
prompt construction, exclusion and timing rules are identical to the
synchronous path in score_listings; only the transport differs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import anthropic
from dotenv import load_dotenv

from . import config, db, llm, log
from .config import Profile
from .score_listings import (
    apply_parsed,
    apply_twin,
    fetch_description,
    hold_back_stale,
    select_rows,
)

logger = log.get_logger(__name__)

# Batches bill at 50% of standard rates.
BATCH_DISCOUNT = 0.50
POLL_SECONDS = 30
TYPICAL_REPLY_TOKENS = 350  # observed typical parse+score reply
CHARS_PER_TOKEN = 4

BatchRequest = dict[str, Any]


def build_requests(
    conn: db.Connection, rows: list[Any], skills: dict[str, Any], write: bool = True
) -> tuple[list[BatchRequest], list[tuple[int, str]], int]:
    """Returns (requests, skipped, reused). custom_id is the posting id, which
    is how a result is mapped back to its row: batch results come back in
    arbitrary order and must never be matched by position.

    write=False keeps this read-only so --dry-run has no side effects."""
    reqs: list[BatchRequest] = []
    skipped: list[tuple[int, str]] = []
    reused = 0
    current = llm.current_twin(skills)  # a twin from an older scorer is read again
    for row in rows:
        stale = hold_back_stale(conn, row, write)  # checked before anything is spent
        if stale:
            skipped.append((row["id"], f"stale: {stale}"))
            continue
        twin = db.find_scored_twin(conn, row["company"], row["title"], current)
        if twin is not None:
            if write:
                apply_twin(conn, row, twin)
            reused += 1
            continue
        fetched = fetch_description(row["url"])
        if fetched[0] is None:
            skipped.append((row["id"], fetched[1]))
            continue
        text = fetched[0]
        posting = {
            "company": row["company"],
            "title": row["title"],
            "location": row["location"] or "",
            "url": row["url"],
            "description": text,
        }
        system, user = llm.parse_score_prompt(posting, skills)
        reqs.append({"custom_id": str(row["id"]), "params": llm.call_params(system, user)})
    conn.commit()
    return reqs, skipped, reused


def estimate_cost(reqs: list[BatchRequest]) -> float:
    """Rough pre-flight estimate at batch rates, from prompt characters.
    Deliberately approximate: it is a guard rail, not an invoice."""
    chars = sum(
        len(r["params"]["system"][0]["text"]) + len(r["params"]["messages"][0]["content"])
        for r in reqs
    )
    in_tok = chars / CHARS_PER_TOKEN
    out_tok = TYPICAL_REPLY_TOKENS * len(reqs)
    full = in_tok / 1e6 * config.PRICE_IN_PER_MTOK + out_tok / 1e6 * config.PRICE_OUT_PER_MTOK
    return round(full * BATCH_DISCOUNT, 4)


def apply_result(
    conn: db.Connection, row_id: int, text: str, profile: Profile | None = None
) -> str:
    data, err = llm.extract_json(llm.unfence(text), llm.parse_score_required())
    if err or data is None:
        return f"parse+score: {err}"
    row = conn.execute(
        "SELECT id, title, employment_type, start_date, description FROM postings WHERE id=?",
        (row_id,),
    ).fetchone()
    if row is None:
        return "row vanished"
    data = llm.finish_parse_score(data, str(row["description"] or ""))
    return apply_parsed(conn, row, data, profile)


def _wait_for(client: anthropic.Anthropic, batch_id: str) -> Any:
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            return batch
        logger.info("batch %s: %s", batch_id, batch.processing_status)
        time.sleep(POLL_SECONDS)


def submit_and_apply(
    conn: db.Connection,
    client: anthropic.Anthropic,
    reqs: list[BatchRequest],
    profile: Profile | None,
) -> tuple[int, int]:
    """Submit the batch, wait for it, write every result back. Returns (ok, failed)."""
    batch = client.messages.batches.create(requests=reqs)  # type: ignore[arg-type]
    print(f"submitted batch {batch.id} ({len(reqs)} requests); polling")
    _wait_for(client, batch.id)
    ok = failed = 0
    for result in client.messages.batches.results(batch.id):
        rid = int(result.custom_id)  # map by custom_id, never by order
        if result.result.type != "succeeded":
            failed += 1
            print(f"  id={rid}: {result.result.type}")
            continue
        text = next((b.text for b in result.result.message.content if b.type == "text"), "")
        outcome = apply_result(conn, rid, text, profile)
        conn.commit()
        ok += 1
        print(f"  id={rid}: {outcome}")
    return ok, failed


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    log.configure()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="build the batch and print the estimate, submit nothing",
    )
    args = ap.parse_args(argv)

    conn = db.connect()
    rows = select_rows(conn, args.limit)
    if not rows:
        print("nothing to score")
        return 0
    skills = json.loads(config.SKILLS_PATH.read_text())
    reqs, skipped, reused = build_requests(conn, rows, skills, write=not args.dry_run)

    print(
        f"selected {len(rows)} | reused twin score {reused} | "
        f"unfetchable {len(skipped)} | to send {len(reqs)}"
    )
    for rid, err in skipped[:5]:
        print(f"  skip id={rid}: {err}")
    if not reqs:
        print("nothing to send")
        return 0

    est = estimate_cost(reqs)
    print(f"estimated batch cost ~${est} (~${round(est / BATCH_DISCOUNT, 4)} if run synchronously)")
    if est > config.COST_CAP_USD:
        print(
            f"estimate exceeds SOURCER_COST_CAP (${config.COST_CAP_USD}); "
            "lower --limit or raise the cap. Nothing submitted."
        )
        return 1
    if args.dry_run:
        print("dry run: nothing submitted")
        return 0

    ok, failed = submit_and_apply(conn, llm.get_client(), reqs, config.load_profile())
    print(f"done: {ok} applied, {failed} failed, {reused} reused without an API call")
    return 0


if __name__ == "__main__":
    sys.exit(main())
