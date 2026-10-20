"""One scored posting, the same way everywhere.

Every path that scores a posting (the daily pipeline, the listing scorer,
the batch scorer, a rescore, and the private side panel) turns a model read
into stored fields with :func:`judge`, and every path that scores a row
already saved writes them with :func:`save_score`. So the board's intern
title, the level grade, the candidate's rules read with the posting's own
text, and the timing flag apply in one order, and every save leaves the same
trace event.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .. import config, trace
from ..config import Profile
from ..eligibility import EXCLUSION_RULES, Rule, eligible, level_fit
from .filters import timing_flag

# a rationale carried over from a twin keeps the twin's timing note; it is
# stripped and computed again for this posting
TIMING_SUFFIX_RE = re.compile(r"\s*\[timing unclear:[^\]]*\]")

# the columns a score may set on a stored posting, and nothing else
SCORE_COLUMNS = frozenset(
    {
        "score",
        "rationale",
        "excluded",
        "exclusion_reason",
        "timing_unclear",
        "level_fit",
        "employment_type",
        "requirements",
        "tech_stack",
        "seniority",
        "years_experience_min",
        "work_mode",
        "score_trace",
    }
)


@dataclass(frozen=True)
class Judged:
    """The stored fields for one read of a posting, and why: ``reason`` when a
    rule excluded it, ``timing`` when its start or type is unstated."""

    fields: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    timing: str = ""


def classify(parsed: dict[str, Any], profile: Profile | None) -> dict[str, Any]:
    """The fields every posting gets, scored or not: the board's own intern
    title over the model's reading, and the level grade."""
    out: dict[str, Any] = {}
    if config.INTERN_TITLE_RE.search(parsed.get("title") or ""):
        # a paid or full time hours internship read as "full time" must not
        # hit the start date rule
        out["employment_type"] = "intern"
    out["level_fit"] = level_fit({**parsed, **out}, profile)
    return out


def judge(
    parsed: Mapping[str, Any],
    profile: Profile | None,
    *,
    score: Any,
    rationale: Any,
    description: str | None = None,
    rules: tuple[Rule, ...] = EXCLUSION_RULES,
) -> Judged:
    """The stored fields for one model read: classify, the candidate's rules
    read with the posting's own text, then the score with its timing note.
    ``description`` is the posting's text when ``parsed`` does not carry it
    (a parse reply never does). An excluded posting scores 0."""
    posting = dict(parsed)
    if description is not None:
        posting["description"] = description
    fields = classify(posting, profile)
    posting.update(fields)
    reason = eligible(posting, profile, rules).reason or None
    if reason:
        fields.update(
            score=0,
            excluded=True,
            exclusion_reason=reason,
            rationale=f"excluded: {reason}",
            timing_unclear=False,
        )
        return Judged(fields, reason=reason)
    text = TIMING_SUFFIX_RE.sub("", str(rationale or "")).rstrip()
    unclear, note = timing_flag(posting)
    if unclear:
        text += f" [timing unclear: {note}]"
    fields.update(
        score=int(score),
        rationale=text,
        timing_unclear=unclear,
        excluded=False,
        exclusion_reason=None,
    )
    return Judged(fields, timing=note if unclear else "")


def read_columns(parsed: Mapping[str, Any]) -> dict[str, Any]:
    """The model's reading of a posting as stored columns, for the keys it has."""
    out: dict[str, Any] = {}
    for key in ("requirements", "tech_stack"):
        if key in parsed:
            out[key] = json.dumps(parsed.get(key) or [])
    for key in ("seniority", "years_experience_min", "work_mode"):
        if key in parsed:
            out[key] = parsed.get(key)
    if parsed.get("score_trace"):
        out["score_trace"] = json.dumps(parsed["score_trace"])
    return out


def save_score(
    conn: Any, posting_id: int, fields: Mapping[str, Any], *, how: str, was: Any = None
) -> None:
    """Write a stored posting's score and leave its trace event: "rules" for
    an exclusion, "score" with ``how`` (and the score it replaced) otherwise.
    Only the given columns change; a column outside ``SCORE_COLUMNS`` is refused."""
    unknown = set(fields) - SCORE_COLUMNS
    if unknown:
        raise ValueError(f"not a score column: {sorted(unknown)}")
    if fields.get("excluded"):
        trace.record(conn, posting_id, "rules", f"excluded: {fields.get('exclusion_reason')}")
    else:
        detail = {"how": how} if was is None else {"how": how, "was": was}
        trace.record(conn, posting_id, "score", fields.get("score"), **detail)
    values = {k: (int(v) if isinstance(v, bool) else v) for k, v in fields.items()}
    sets = ", ".join(f"{k}=?" for k in values)
    # the column names come only from SCORE_COLUMNS, checked above; values are bound
    conn.execute(f"UPDATE postings SET {sets} WHERE id=?", (*values.values(), posting_id))  # noqa: S608  # nosec B608
