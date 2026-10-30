"""The collector registry: one function per source type.

A new source type registers a ``collect(source)`` here that returns
``Collected`` items, logs each outbound call with ``store.audit`` (links do
it through ``web.Fetcher``) and passes every excerpt through
``privacy.redact``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import folders, github, linkedin, links, resume
from .items import Collected

COLLECTORS: dict[str, Callable[[dict[str, Any]], Collected]] = {
    "resume": resume.collect,
    "github": github.collect,
    "folder": folders.collect,
    "linkedin": linkedin.collect,
    "link": links.legacy_link,
    **links.COLLECTORS,
}


def collect_all(sources: list[dict[str, Any]]) -> dict[str, Collected]:
    """Every source's items, by source id."""
    return {s["id"]: COLLECTORS[s["type"]](s) for s in sources if s.get("type") in COLLECTORS}
