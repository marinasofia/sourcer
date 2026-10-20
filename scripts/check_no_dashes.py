#!/usr/bin/env python3
"""Fail if any given file contains an em dash or an en dash.

House style: commas, colons, or periods instead. Runs as a pre-commit hook and
in CI over code, docs, and configuration.
"""

from __future__ import annotations

import sys
from pathlib import Path

BANNED = {"\u2014": "em dash", "\u2013": "en dash"}


def offending_lines(path: Path) -> list[tuple[int, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for char, name in BANNED.items():
            if char in line:
                hits.append((lineno, name))
    return hits


def main(argv: list[str]) -> int:
    failed = False
    for arg in argv:
        for lineno, name in offending_lines(Path(arg)):
            print(f"{arg}:{lineno}: {name}")  # noqa: T201
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
