"""Candidate facts from source items: batching, the cost estimate, the
preview, the model calls, and the check that every quote is in its item.

No model call happens here without the caller's confirmation; ``run``
takes a client the caller created only after the candidate said yes, and
every call passes ``Usage.can_call`` (the cost cap).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config, llm
from ..log import get_logger
from .backends import Backend
from .items import SourceItem, digest
from .privacy import contact_detail
from .store import FACT_KINDS, audit, now, write_private

log = get_logger(__name__)

MAX_TOKENS = 4000
# a reply is about this many tokens per call, for the estimate only
EST_OUT_TOKENS = 1200
MAX_FACT_CHARS = 400


def batches(items: Iterable[SourceItem], limit: int | None = None) -> list[list[SourceItem]]:
    """Items packed in order into groups of at most ``limit`` excerpt characters."""
    limit = limit or config.ONBOARD_BATCH_CHARS
    out: list[list[SourceItem]] = []
    size = 0
    for item in items:
        if out and size + len(item.text) <= limit:
            out[-1].append(item)
            size += len(item.text)
        else:
            out.append([item])
            size = len(item.text)
    return out


def _fence(text: str) -> str:
    return re.sub(r"</?\s*item\b[^>]*>", " ", text, flags=re.I)


def user_turn(batch: list[SourceItem]) -> str:
    """The exact user message for one call. Items get short ids (i1, i2) so
    nothing from a file name or URL lands inside a tag attribute."""
    parts = [llm.FACTS_NOTE, ""]
    for n, item in enumerate(batch, 1):
        parts.append(f'<item id="i{n}" source="{item.source_type}">\n{_fence(item.text)}\n</item>')
    return "\n".join(parts)


@dataclass
class Estimate:
    items: int
    chars: int
    calls: int
    usd: float | None  # None on a backend that is not billed per call


def estimate(items: list[SourceItem], backend: Backend) -> Estimate:
    groups = batches(items)
    chars = sum(len(i.text) for i in items)
    prompt_chars = sum(len(user_turn(g)) + len(llm.FACTS_SYSTEM) for g in groups)
    usd = None
    if backend.priced:
        pin, pout = llm.model_prices(config.FACTS_MODEL)
        usd = round(prompt_chars / 4 / 1e6 * pin + len(groups) * EST_OUT_TOKENS / 1e6 * pout, 2)
    return Estimate(len(items), chars, len(groups), usd)


def write_preview(items: list[SourceItem], path: Path | None = None) -> Path:
    """Write the exact text each call would send, already redacted, and make
    no call. Returns the file written."""
    path = path or config.ONBOARD_PREVIEW_PATH
    groups = batches(items)
    out = [
        "Sourcer onboarding preview. Nothing below has been sent.",
        f"Written {now()}. {len(groups)} model calls, {len(items)} excerpts.",
        "",
        "=== Instructions sent with every call ===",
        llm.FACTS_SYSTEM,
    ]
    for n, group in enumerate(groups, 1):
        where = ", ".join(sorted({f"{i.source_type} {i.location}" for i in group}))
        out += ["", f"=== Call {n} of {len(groups)}: {where} ===", user_turn(group)]
    write_private(path, "\n".join(out) + "\n")
    return path


def _named_in(skill: str, text: str) -> bool:
    s = skill.strip().lower()
    return bool(s) and re.search(r"(?<!\w)" + re.escape(s) + r"(?!\w)", text.lower()) is not None


def clean(text: Any, limit: int) -> str:
    """One line of plain text: no control characters (a terminal escape in a
    source cannot reach the review screen), single spaces, capped."""
    return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(text or "")).split())[:limit]


def _skills(named: list[Any], text: str) -> list[str]:
    """The named skills the item's text contains, once each, case ignored."""
    seen: dict[str, str] = {}
    for raw in named:
        skill = clean(raw, 60)
        if _named_in(skill, text):
            seen.setdefault(skill.lower(), skill)
    return list(seen.values())


def verify_facts(data: dict[str, Any], batch: list[SourceItem]) -> tuple[list[dict[str, Any]], int]:
    """The facts whose quote is in their own item, with only the skills that
    item names. Returns (facts, number dropped)."""
    by_id = {f"i{n}": item for n, item in enumerate(batch, 1)}
    kept: list[dict[str, Any]] = []
    dropped = 0
    for raw in data.get("facts") or []:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        item = by_id.get(str(raw.get("item")))
        text = clean(raw.get("text"), MAX_FACT_CHARS)
        quote = clean(raw.get("quote"), MAX_FACT_CHARS)
        if (
            item is None
            or not text
            or not llm.quoted_in(quote, item.text)
            or contact_detail(text)
            or contact_detail(quote)
        ):
            dropped += 1
            continue
        named = raw.get("skills")
        skills: list[Any] = named if isinstance(named, list) else []
        kind = str(raw.get("kind") or "other").lower()
        kept.append(
            {
                "id": digest(f"{item.id}|{quote.lower()}|{text.lower()}"),
                "text": text,
                "skills": _skills(skills, item.text),
                "kind": kind if kind in FACT_KINDS else "other",
                "group": clean(raw.get("group"), 120),
                "dates": clean(raw.get("dates"), 60),
                "source_type": item.source_type,
                "source_id": item.source_id,
                "source_ref": item.location,
                "item_id": item.id,
                "item_sha": item.sha,
                "quote": quote,
                "created_at": now(),
            }
        )
    return kept, dropped


@dataclass
class Result:
    facts: list[dict[str, Any]] = field(default_factory=list)
    done: list[SourceItem] = field(default_factory=list)  # items read successfully
    dropped: int = 0
    failed_calls: int = 0
    stopped: str | None = None  # why the scan stopped early


def run(client: Any, items: list[SourceItem], backend: Backend, usage: llm.Usage) -> Result:
    """Extract candidate facts, one call per batch. Stops at the cost cap."""
    result = Result()
    for batch in batches(items):
        if not usage.can_call():
            result.stopped = "the cost cap (SOURCER_COST_CAP) was reached"
            break
        user = user_turn(batch)
        audit(
            backend.destination,
            "extract facts",
            [i.source_id for i in batch],
            len(user) + len(llm.FACTS_SYSTEM),
        )
        try:
            data, err = llm.json_call(
                client, llm.FACTS_SYSTEM, user, llm.FACTS_REQUIRED, usage,
                max_tokens=MAX_TOKENS, model=config.FACTS_MODEL,
            )  # fmt: skip
        except Exception as exc:  # a CLI or API failure must not lose earlier batches
            log.warning("fact extraction call failed: %s", type(exc).__name__)
            data, err = None, type(exc).__name__
        if data is None:
            result.failed_calls += 1
            log.info("fact extraction failed for one batch: %s", err)
            continue
        facts, dropped = verify_facts(data, batch)
        result.facts += facts
        result.dropped += dropped
        result.done += batch
    return result
