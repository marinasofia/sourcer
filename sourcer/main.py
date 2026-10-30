"""Entry point: ``python -m sourcer.main`` or the ``sourcer`` console script."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from dotenv import load_dotenv

from . import __version__, config, db, llm, log, progress, rescreen, score_listings
from .digest import render_digest
from .pipeline.graph import build_graph
from .pipeline.state import State


def build_state(client: Any | None = None) -> State:
    """Initial pipeline state from the configured inputs.

    ``client`` is injectable for tests and for cost-free runs; the default
    creates a real Anthropic client only when the cost cap allows spend.
    """
    if client is None and config.COST_CAP_USD > 0:
        client = llm.get_client()
    configured_companies = json.loads(config.COMPANIES_PATH.read_text())
    conn = db.connect()
    db.upsert_companies(conn, configured_companies)
    companies = [
        {
            "company": row["name"],
            "domain": row.get("domain"),
            "ats": row["ats_type"],
            "slug": row["ats_slug"],
            "quality_signals": row.get("quality_signals"),
        }
        for row in db.list_companies(conn)
        if row.get("quality_status") == "passed" and row.get("ats_type") and row.get("ats_slug")
    ]
    return {
        "companies": companies,
        "skills": json.loads(config.SKILLS_PATH.read_text()),
        "profile": config.load_profile(),
        "client": client,
        "usage": llm.Usage(),
        "conn": conn,
        "stats": {},
        # a posting collected without its text is read from its page before scoring
        "read_posting": lambda url: score_listings.fetch_description(url)[0],
    }


def _after_collect(final: State) -> None:
    """Score a few community listings with what is left of the budget, then
    re-apply today's rules to rows already on the board. No calls when the
    budget is spent or no client exists."""
    stats = final["stats"]
    client = final.get("client")
    if client is not None and config.LISTING_SCORE_LIMIT > 0:
        stats["listings_scored"] = score_listings.score_pending(
            final["conn"],
            client,
            final["usage"],
            skills=final["skills"],
            profile=final.get("profile"),
            limit=config.LISTING_SCORE_LIMIT,
        )
        stats["est_cost_usd"] = final["usage"].est_cost_usd
    screened = rescreen.rescreen(final["conn"], final.get("profile"))
    stats["rescreen_archived"] = len(screened["archived"])


def run(state: State | None = None) -> State:
    """Run the pipeline once and persist the run log. Returns the final state."""
    t0 = time.time()
    progress.stage("prepare")
    state = state or build_state()
    progress.stage("prepare", status="done")
    final: State = build_graph().invoke(state)
    progress.stage("save", status="running")
    _after_collect(final)
    final["stats"]["duration_s"] = round(time.time() - t0, 1)
    db.insert_run_log(final["conn"], final["stats"])
    progress.stage("save", done=final["stats"].get("new_postings"), status="done")
    return final


def main(argv: list[str] | None = None) -> int:
    args_in = sys.argv[1:] if argv is None else argv
    if args_in[:1] == ["probe"]:
        from .probe import main as probe_main  # noqa: PLC0415

        return probe_main(args_in[1:])
    if args_in[:1] == ["discover"]:
        from .discovery.cli import main as discovery_main  # noqa: PLC0415

        return discovery_main(args_in[1:])
    if args_in[:1] == ["orchestrate"]:
        from .orchestrator import main as orchestrator_main  # noqa: PLC0415

        return orchestrator_main(args_in[1:])
    if args_in[:1] == ["telegram"]:
        from .bot.telegram import main as telegram_main  # noqa: PLC0415

        return telegram_main(args_in[1:])
    if args_in[:1] in (["init"], ["facts"], ["forget"]):
        from .onboard.cli import main as onboard_main  # noqa: PLC0415

        return onboard_main(args_in)
    if args_in[:1] == ["collect"]:
        args_in = args_in[1:]
    ap = argparse.ArgumentParser(prog="sourcer", description=__doc__)
    ap.add_argument("--version", action="version", version=f"sourcer {__version__}")
    ap.add_argument(
        "--cost-cap",
        type=float,
        default=None,
        metavar="USD",
        help="hard ceiling for model spend this run (default: SOURCER_COST_CAP)",
    )
    args = ap.parse_args(args_in)
    load_dotenv()  # picks up ANTHROPIC_API_KEY from .env if present
    log.configure()
    if args.cost_cap is not None:
        config.COST_CAP_USD = args.cost_cap
    final = run()
    print(render_digest(final))
    failures = final["stats"].get("source_failures")
    if failures:
        print("source fetch failures:", failures, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
