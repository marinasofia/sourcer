"""Pipeline state shared by every node, plus per-node bookkeeping helpers."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypedDict

import anthropic

from .. import config, db, llm
from ..config import Profile

Posting = dict[str, Any]


class State(TypedDict, total=False):
    companies: list[dict[str, Any]]
    skills: dict[str, Any]
    profile: Profile | None
    postings: list[Posting]  # in-scope raw postings
    new_postings: list[Posting]  # after dedupe
    parsed: list[Posting]  # after parse node
    scored: list[Posting]  # after score node
    stats: dict[str, Any]
    client: anthropic.Anthropic | None  # injectable for tests
    usage: llm.Usage
    conn: db.Connection
    read_posting: Callable[[str], str | None]  # url -> page text, for postings collected bare


def _timed(stats: dict[str, Any], node: str, t0: float, **extra: Any) -> None:
    stats.setdefault("node_metrics", {})[node] = {"latency_s": round(time.time() - t0, 2), **extra}


def _cost_cap_reached(usage: llm.Usage) -> bool:
    return usage.est_cost_usd >= config.COST_CAP_USD
