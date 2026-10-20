"""Bounded, explainable ranking adjustments learned from operator feedback."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

MAX_ADJUSTMENT = 5
MIN_FEEDBACK = 3
TOKEN_RE = re.compile(r"[a-z0-9+#.]{2,}")
STOPWORDS = frozenset({"and", "the", "for", "with", "engineer", "intern", "new", "grad"})


def _tokens(title: str) -> set[str]:
    return {token for token in TOKEN_RE.findall(title.casefold()) if token not in STOPWORDS}


def title_adjustment(title: str, feedback: Iterable[Mapping[str, Any]]) -> int:
    """Return a capped title-token preference adjustment after enough feedback."""
    observations = list(feedback)
    if len(observations) < MIN_FEEDBACK:
        return 0
    weights: dict[str, int] = defaultdict(int)
    for item in observations:
        value = 1 if int(item["value"]) > 0 else -1
        for token in _tokens(str(item.get("title") or "")):
            weights[token] += value
    raw = sum(weights[token] for token in _tokens(title))
    return max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, raw))


def adjusted_score(posting: Mapping[str, Any], feedback: Iterable[Mapping[str, Any]]) -> int:
    """Adjust ranking only. Hard filters and stored model scores remain unchanged."""
    base = int(posting.get("score") or 0)
    return max(0, min(100, base + title_adjustment(str(posting.get("title") or ""), feedback)))
