"""Evaluation harness for the scoring policy.

  python -m sourcer.evaluate                      # deterministic rules only, free
  python -m sourcer.evaluate --model              # also score through the model
  python -m sourcer.evaluate --labelled path.json # a different labelled set

The labelled set (``evals/labelled_postings.json``) holds parsed postings
with the expected exclusion outcome, the expected level fit, and optionally
a score band. The deterministic part runs in CI on every change and exits
non-zero on any disagreement, so a rule change that flips a labelled case
is caught before merge. The model part costs one call per posting with a
description and reports mean absolute error against the band midpoint plus
band agreement.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

from . import config, llm, log
from .config import Profile
from .eligibility import EXCLUSION_RULES, eligible, level_fit

logger = log.get_logger(__name__)

DEFAULT_LABELLED = config.PROJECT_ROOT / "evals" / "labelled_postings.json"

Record = dict[str, Any]


def load_evaluation(path: Path | None = None) -> tuple[list[Record], Profile | None]:
    """Load labelled cases and their pinned policy as one evaluation fixture."""
    data = json.loads((path or DEFAULT_LABELLED).read_text())
    records = data["records"] if isinstance(data, dict) else data
    for r in records:
        for key in ("id", "posting", "expected"):
            if key not in r:
                raise ValueError(f"labelled record missing {key!r}: {r}")
    profile = data.get("profile") if isinstance(data, dict) else None
    return list(records), profile


def load_labelled(path: Path | None = None) -> list[Record]:
    return load_evaluation(path)[0]


def evaluate_rules(records: list[Record], profile: Profile | None) -> dict[str, Any]:
    """Exclusion precision and recall plus level-fit agreement, no model calls."""
    tp = fp = fn = tn = 0
    fit_agree = 0
    mismatches: list[dict[str, Any]] = []
    for r in records:
        expected = r["expected"]
        reason = eligible(r["posting"], profile, EXCLUSION_RULES).reason or None
        got_excluded = reason is not None
        want_excluded = bool(expected.get("excluded"))
        fragment = expected.get("reason_contains")
        reason_ok = not (want_excluded and fragment) or (reason is not None and fragment in reason)
        if got_excluded and want_excluded:
            tp += 1
        elif got_excluded:
            fp += 1
        elif want_excluded:
            fn += 1
        else:
            tn += 1
        fit = level_fit(r["posting"], profile)
        fit_ok = expected.get("level_fit") in (None, fit)
        if fit_ok:
            fit_agree += 1
        if got_excluded != want_excluded or not reason_ok or not fit_ok:
            mismatches.append(
                {
                    "id": r["id"],
                    "expected": expected,
                    "got": {"excluded": got_excluded, "reason": reason, "level_fit": fit},
                }
            )
    return {
        "records": len(records),
        "exclusion": {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
            "precision": round(tp / (tp + fp), 4) if tp + fp else 1.0,
            "recall": round(tp / (tp + fn), 4) if tp + fn else 1.0,
        },
        "level_fit_agreement": round(fit_agree / len(records), 4) if records else 1.0,
        "mismatches": mismatches,
    }


def evaluate_model(
    records: list[Record], client: anthropic.Anthropic, skills: dict[str, Any], usage: llm.Usage
) -> dict[str, Any]:
    """Score every record that carries a description; compare with its band."""
    errors: list[float] = []
    in_band = scored = failed = skipped_cap = 0
    details: list[dict[str, Any]] = []
    for r in records:
        band = r["expected"].get("score_band")
        posting = r.get("scoring_input")
        if not band or not posting:
            continue
        if usage.est_cost_usd >= config.COST_CAP_USD:
            skipped_cap += 1
            continue
        data, err = llm.parse_and_score(client, posting, skills, usage)
        if err or data is None:
            failed += 1
            details.append({"id": r["id"], "error": err})
            continue
        score = int(data["score"])
        lo, hi = band
        errors.append(abs(score - (lo + hi) / 2))
        in_band += int(lo <= score <= hi)
        scored += 1
        details.append({"id": r["id"], "score": score, "band": band})
    return {
        "scored": scored,
        "failed": failed,
        "skipped_cost_cap": skipped_cap,
        "mean_abs_error": round(sum(errors) / len(errors), 2) if errors else None,
        "band_agreement": round(in_band / scored, 4) if scored else None,
        "est_cost_usd": usage.est_cost_usd,
        "details": details,
    }


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    log.configure()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--labelled", type=Path, default=None, help="labelled set path")
    ap.add_argument(
        "--profile",
        type=Path,
        default=None,
        help="profile path (default: fixture profile, then configured)",
    )
    ap.add_argument("--model", action="store_true", help="also score through the model")
    args = ap.parse_args(argv)

    records, fixture_profile = load_evaluation(args.labelled)
    profile = (
        config.load_profile(args.profile)
        if args.profile is not None or fixture_profile is None
        else fixture_profile
    )
    report: dict[str, Any] = {"rules": evaluate_rules(records, profile)}
    if args.model:
        skills = json.loads(config.SKILLS_PATH.read_text())
        report["model"] = evaluate_model(records, llm.get_client(), skills, llm.Usage())
    print(json.dumps(report, indent=1))
    return 1 if report["rules"]["mismatches"] else 0


if __name__ == "__main__":
    sys.exit(main())
