"""The collector registry: one function per source type.

A new source type (step 5 adds portfolio sites, feeds, Kaggle, Hugging Face,
Devpost, Scholar and Credly links) registers a ``collect(source)`` here that
returns ``Collected`` items, logs each outbound call with ``store.audit``
and passes every excerpt through ``privacy.redact``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import folders, github, linkedin, resume
from .items import Collected


def _links(source: dict[str, Any]) -> Collected:
    """Links are recorded at init; reading them is not built yet."""
    return Collected(notes=[f"link saved, not read yet: {source['value']}"])


COLLECTORS: dict[str, Callable[[dict[str, Any]], Collected]] = {
    "resume": resume.collect,
    "github": github.collect,
    "folder": folders.collect,
    "linkedin": linkedin.collect,
    "link": _links,
}


def collect_all(sources: list[dict[str, Any]]) -> dict[str, Collected]:
    """Every source's items, by source id."""
    return {s["id"]: COLLECTORS[s["type"]](s) for s in sources if s.get("type") in COLLECTORS}
