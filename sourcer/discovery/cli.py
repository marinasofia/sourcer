"""CLI for weekly company discovery and ATS probing."""

from __future__ import annotations

import argparse
import sys

from .. import db, log
from ..probe import probe_pending
from .service import FETCHERS, discover


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
        help="maximum new companies to probe this run; default 25",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.probe_limit < 0:
        parser.error("--probe-limit cannot be negative")
    log.configure()
    conn = db.connect()
    result = discover(conn, args.source)
    print(
        f"discovered {len(result.candidates)} unique companies, "
        f"{len(result.new_candidates)} new, {len(result.errors)} source errors"
    )
    for source, count in result.source_counts.items():
        print(f"{source}: {count}")
    if not args.no_probe:
        results = probe_pending(conn, args.probe_limit)
        resolved = sum(result.ats_type is not None for result in results)
        print(f"ATS probe: {resolved}/{len(results)} resolved")
    for error in result.errors:
        print(error, file=sys.stderr)
    return 1 if result.errors and not result.candidates else 0


if __name__ == "__main__":
    raise SystemExit(main())
