"""Local onboarding files: the sources list, candidate facts, the audit log,
the preview, and the onboarding section of the evidence file.

Every path comes from ``config`` and is read at call time, so tests and
``SOURCER_*`` overrides point everything elsewhere. Files are written owner
readable only.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import config

FACT_KINDS = ("job", "project", "education", "certification", "publication", "other")
STATUSES = ("pending", "approved", "rejected")
EVIDENCE_START = (
    "# >>> sourcer onboarding: approved facts (sourcer facts review rewrites this part)"
)
EVIDENCE_END = "# <<< sourcer onboarding"


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` readable by its owner only."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    path.chmod(0o600)  # an existing file keeps its old mode through O_TRUNC


def _read_json(path: Path, empty: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    return data if isinstance(data, dict) else empty


# ---------------------------------------------------------------- sources


def load_sources() -> dict[str, Any]:
    data = _read_json(config.SOURCES_PATH, {})
    data.setdefault("version", 1)
    data.setdefault("sources", [])
    return data


def save_sources(data: dict[str, Any]) -> None:
    write_private(config.SOURCES_PATH, json.dumps(data, indent=2) + "\n")


def add_source(data: dict[str, Any], spec: dict[str, Any]) -> bool:
    """Add a source unless one with the same id is there. True when added."""
    if any(s["id"] == spec["id"] for s in data["sources"]):
        return False
    data["sources"].append({**spec, "added_at": now()})
    return True


# ---------------------------------------------------------------- facts


def load_facts() -> dict[str, Any]:
    data = _read_json(config.FACTS_PATH, {})
    data.setdefault("version", 1)
    data.setdefault("facts", [])
    data.setdefault("scanned", {})  # item id -> sha of the excerpt last read
    return data


def save_facts(data: dict[str, Any]) -> None:
    write_private(config.FACTS_PATH, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def merge_facts(data: dict[str, Any], new: Iterable[dict[str, Any]]) -> int:
    """Queue new candidate facts; a fact already known (same id) keeps its
    status and any edit. Returns how many were added."""
    known = {f["id"] for f in data["facts"]}
    added = 0
    for fact in new:
        if fact["id"] not in known:
            data["facts"].append({**fact, "status": "pending"})
            known.add(fact["id"])
            added += 1
    return added


# ---------------------------------------------------------------- audit log


def audit(destination: str, action: str, source_ids: Iterable[str], chars: int) -> None:
    """Record one outbound call: where it went and how much, never the content."""
    entry = {
        "ts": now(),
        "destination": destination,
        "action": action,
        "sources": sorted(set(source_ids)),
        "chars": int(chars),
    }
    path = config.ONBOARD_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    path.chmod(0o600)


def read_audit() -> list[dict[str, Any]]:
    try:
        lines = config.ONBOARD_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ---------------------------------------------------------------- evidence


def _fact_line(fact: dict[str, Any]) -> str:
    skills = ", ".join(fact.get("skills") or [])
    return f"- {str(fact['text']).lstrip('# ')}" + (f" Skills: {skills}." if skills else "")


def render_approved(facts: list[dict[str, Any]]) -> str:
    """Approved facts grouped by the job, project or school they belong to,
    one fact per line: the shape the evidence file expects."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for fact in facts:
        if fact.get("status") != "approved":
            continue
        key = (fact.get("group") or "Other", fact.get("kind") or "other", fact.get("dates") or "")
        groups.setdefault(key, []).append(fact)
    blocks = []
    for (group, kind, dates), members in groups.items():
        head = f"{group.lstrip('# ')} ({kind}{', ' + dates if dates else ''})"
        blocks.append("\n".join([head, *(_fact_line(f) for f in members)]))
    return "\n\n".join(blocks)


def split_evidence(text: str) -> tuple[str, str]:
    """(hand written part, onboarding part) of an evidence file."""
    start = text.find(EVIDENCE_START)
    if start < 0:
        return text.strip(), ""
    end = text.find(EVIDENCE_END, start)
    tail = "" if end < 0 else text[end + len(EVIDENCE_END) :]
    inner = text[start + len(EVIDENCE_START) : end if end >= 0 else len(text)]
    return (text[:start] + tail).strip(), inner.strip()


def write_evidence(facts: list[dict[str, Any]]) -> int:
    """Rewrite the onboarding section of the evidence file from the approved
    facts, keeping every hand written line outside it. Returns the number of
    approved facts written. The file is removed when nothing is left in it."""
    path = config.EVIDENCE_PATH
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = ""
    hand, _ = split_evidence(current)
    approved = render_approved(facts)
    parts = [hand] if hand else []
    if approved:
        parts.append(f"{EVIDENCE_START}\n{approved}\n{EVIDENCE_END}")
    if parts:
        write_private(path, "\n\n".join(parts) + "\n")
    elif path.exists():
        path.unlink()
    return sum(1 for f in facts if f.get("status") == "approved")
