#!/usr/bin/env python3
"""Refuse to commit operator data into the public repository.

The public repo holds code and aggregate statistics only. This check fails if
any tracked or staged file is one of the private inputs, or if a source file
contains an API key pattern or a per-posting rationale export. It runs as a
pre-commit hook and in CI.
"""

from __future__ import annotations

import re

# Only the fixed git argv in tracked_files is executed, without a shell.
import subprocess  # nosec B404
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_PATHS = {
    "skills_vector.json",
    "profile.json",
    "docs/data.json",
    "sample_output.txt",
    ".env",
}
FORBIDDEN_PREFIXES = ("resumes/", "kits/", "data/")
FORBIDDEN_SUFFIXES = (
    ".docx",
    ".odt",
    ".pdf",
    ".rtf",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pem",
    ".key",
)

SECRET_PATTERNS = {
    "anthropic api key": re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    "aws access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
}
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".yml",
    ".yaml",
    ".toml",
    ".json",
    ".txt",
    ".sh",
    ".html",
    ".js",
    ".cfg",
}


def tracked_files() -> list[str]:
    # The executable is git from the operator PATH and every argument is fixed.
    out = subprocess.run(  # nosec B603, B607
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def path_problems(rel: str) -> str | None:
    if rel in FORBIDDEN_PATHS:
        return "operator input must stay in the private data repo"
    if rel.startswith(FORBIDDEN_PREFIXES):
        return "directory is private"
    if rel.endswith(FORBIDDEN_SUFFIXES):
        return "binary or credential file type"
    return None


def content_problems(rel: str) -> list[str]:
    path = ROOT / rel
    if path.suffix not in TEXT_SUFFIXES or not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    return [name for name, pattern in SECRET_PATTERNS.items() if pattern.search(text)]


def main() -> int:
    failures: list[str] = []
    for rel in tracked_files():
        problem = path_problems(rel)
        if problem:
            failures.append(f"{rel}: {problem}")
        failures.extend(f"{rel}: contains {name}" for name in content_problems(rel))
    for line in failures:
        print(line)  # noqa: T201
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
