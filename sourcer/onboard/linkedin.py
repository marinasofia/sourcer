"""LinkedIn collector: the official data export zip the candidate downloads
(Settings, Data privacy, Get a copy of your data). Never the website.

Reads only these members when present: Positions.csv, Projects.csv,
Skills.csv, Certifications.csv and Education.csv. Each row becomes one item
whose excerpt is the row written as "Column: value" lines. Members are read
with a size cap, and nothing in the zip is extracted to disk.
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path
from typing import Any

from .items import Collected, SourceItem
from .privacy import redact

MEMBERS = {
    "positions.csv": ("Title", "Company Name", "Location", "Started On", "Finished On",
                      "Description"),
    "projects.csv": ("Title", "Url", "Started On", "Finished On", "Description"),
    "certifications.csv": ("Name", "Authority", "Url", "Started On", "Finished On"),
    "education.csv": ("School Name", "Degree Name", "Start Date", "End Date", "Notes",
                      "Activities"),
    "skills.csv": ("Name",),
}  # fmt: skip
MAX_MEMBER_BYTES = 2_000_000
MAX_ROWS = 200


def _rows(raw: str) -> list[dict[str, str]]:
    """CSV rows; LinkedIn sometimes puts a notes paragraph above the header."""
    lines = raw.lstrip("﻿").splitlines()
    heads = [n for n, line in enumerate(lines[:10]) if line.strip()]
    heads = [n for n in heads if not lines[n].lower().startswith("notes")]
    with_comma = [n for n in heads if "," in lines[n]]
    if not heads:
        return []
    start = with_comma[0] if with_comma else heads[0]
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    return [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in reader][
        :MAX_ROWS
    ]


def _excerpt(row: dict[str, str], columns: tuple[str, ...]) -> str:
    return "\n".join(f"{c}: {row[c]}" for c in columns if row.get(c))


def collect(source: dict[str, Any]) -> Collected:
    path = Path(source["value"]).expanduser()
    out = Collected()
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        out.notes.append(f"LinkedIn export not read: {exc}")
        return out
    with archive:
        for info in archive.infolist():
            member = Path(info.filename).name.lower()
            if member not in MEMBERS or info.is_dir():
                continue
            if info.file_size > MAX_MEMBER_BYTES:
                out.skipped["large files"] = out.skipped.get("large files", 0) + 1
                continue
            with archive.open(info) as fh:
                raw = fh.read(MAX_MEMBER_BYTES + 1)[:MAX_MEMBER_BYTES].decode("utf-8", "replace")
            rows = _rows(raw)
            out.read.append(Path(info.filename).name)
            columns = MEMBERS[member]
            if member == "skills.csv":
                names = [r.get("Name", "") for r in rows if r.get("Name")]
                if names:
                    text = redact("Skills listed on LinkedIn: " + ", ".join(names))
                    out.items.append(SourceItem(source["id"], "linkedin", info.filename, text))
                continue
            for n, row in enumerate(rows):
                text = redact(_excerpt(row, columns))
                if text:
                    out.items.append(SourceItem(source["id"], "linkedin", info.filename, text, n))
    if not out.read:
        out.notes.append("no Positions, Projects, Skills, Certifications or Education files found")
    return out
