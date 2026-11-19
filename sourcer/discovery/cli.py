"""CLI for weekly company discovery and ATS probing."""

from __future__ import annotations

import argparse
import sys

from .. import db, log
from ..probe import probe_pending
from .service import FETCHERS, discover
from .websites import backfill_domains


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sourcer discover", description=__doc__)
    parser.add_argument(
        "--source",
        action="append",
        choices=sorted(FETCHERS),
        help="source to run, repeatable; defaults to all configured sources",
    )
    parser.add_argument(
        "--no-probe",
        action="store_true",
        help="save companies without probing newly discovered ATS boards",
    )
    parser.add_argument(
        "--probe-limit",
        type=int,
        default=25,
        help="maximum companies to probe this run; default 25",
    )
    parser.add_argument(
        "--probe-minutes",
        type=float,
        default=None,
        help="time budget for probing; no new company starts after it",
    )
    parser.add_argument(
        "--backfill-domains",
        type=int,
        default=0,
        metavar="N",
        help="look up websites for up to N companies stored without one, before probing",
    )
    parser.add_argument(
        "--backfill-minutes",
        type=float,
        default=None,
        help="time budget for the website lookup; no new company starts after it",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.probe_limit < 0:
        parser.error("--probe-limit cannot be negative")
    if args.probe_minutes is not None and args.probe_minutes <= 0:
        parser.error("--probe-minutes must be positive")
    if args.backfill_domains < 0:
        parser.error("--backfill-domains cannot be negative")
    if args.backfill_minutes is not None and args.backfill_minutes <= 0:
        parser.error("--backfill-minutes must be positive")
    log.configure()
    conn = db.connect()
    result = discover(conn, args.source)
    print(
        f"discovered {len(result.candidates)} unique companies, "
        f"{len(result.new_candidates)} new, {len(result.errors)} source errors"
    )
    for source, count in result.source_counts.items():
        print(f"{source}: {count}")
    if args.backfill_domains:
        found = backfill_domains(conn, args.backfill_domains, minutes=args.backfill_minutes)
        named = sum(item.domain is not None for item in found)
        print(f"website backfill: {named}/{len(found)} found")
    if not args.no_probe:
        results = probe_pending(conn, args.probe_limit, minutes=args.probe_minutes)
        resolved = sum(result.ats_type is not None for result in results)
        print(f"ATS probe: {resolved}/{len(results)} resolved")
    companies = db.list_companies(conn)
    boards = sum(bool(row.get("ats_type")) for row in companies)
    share = f" ({boards / len(companies):.0%})" if companies else ""
    print(f"coverage: {boards} of {len(companies)} companies have a board{share}")
    for error in result.errors:
        print(error, file=sys.stderr)
    return 1 if result.errors and not result.candidates else 0


if __name__ == "__main__":
    raise SystemExit(main())
