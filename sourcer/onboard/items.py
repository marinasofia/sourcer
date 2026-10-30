"""The shapes every collector returns: source specs and raw source items."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The source types a candidate can add. A link is saved as the kind its URL
# names (onboard.links.detect); "link" remains for links saved before that.
SOURCE_TYPES = ("resume", "github", "folder", "link", "linkedin", "page", "feed", "kaggle",
                "huggingface", "devpost", "scholar", "credly", "publications")  # fmt: skip


def digest(text: str, size: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:size]


@dataclass(frozen=True)
class SourceItem:
    """One excerpt of one source: what the model reads and what quotes are
    checked against. ``id`` is stable for the same source and location, and
    ``sha`` changes when the excerpt changes, so a later scan reads only what
    is new or changed."""

    source_id: str
    source_type: str
    location: str  # file path relative to the source folder, or a URL
    text: str
    part: int = 0

    @property
    def id(self) -> str:
        return f"{self.source_id}/{self.location}#{self.part}"

    @property
    def sha(self) -> str:
        return digest(self.text)


@dataclass
class Collected:
    """What a collector found: items to read, files or repos it read (for the
    preview and the consent line), and short notes for the candidate.

    ``stamps`` say what was seen, so the next scan can skip what did not
    change: ``FILE`` maps to the hash of a whole file (resume, export zip),
    any other key is an item location (a repo URL) mapped to its last change.
    A stamp is saved into the source entry only once every item it covers
    has been read, so a declined or failed scan reads it again next time."""

    items: list[SourceItem] = field(default_factory=list)
    read: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    stamps: dict[str, str] = field(default_factory=dict)


FILE = "file"
UNCHANGED = "unchanged since the last scan"


def file_hash(path: Path) -> str:
    """The sha256 of a file, read in chunks."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def seen(source: dict[str, Any], key: str) -> str | None:
    """The stamp saved for ``key`` the last time this source was read."""
    stamps = source.get("seen")
    return stamps.get(key) if isinstance(stamps, dict) else None


def source_spec(kind: str, value: str, **extra: Any) -> dict[str, Any]:
    """One entry of sources.json. The id is derived from the type and value so
    adding the same source twice is a no op."""
    if kind not in SOURCE_TYPES:
        raise ValueError(f"unknown source type: {kind}")
    return {"id": f"{kind}:{digest(kind + '|' + value, 8)}", "type": kind, "value": value, **extra}
