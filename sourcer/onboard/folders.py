"""Local folder collector: cheap structural signals from project folders.

Per project it reads only the README and package manifests at the project
root, counts files by extension (names only, never contents), and asks git
for the first and last commit dates. Everything ``privacy`` rules out is
skipped before any file is opened: secrets and keys, gitignored files,
dependencies and build output, hidden files, links, binaries and large files.
A folder that is not itself a project is treated as a folder of projects.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404  fixed argv, no shell
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config
from .items import Collected, SourceItem
from .privacy import IgnoreRules, content_reason, dir_skip_reason, name_reason, redact

MANIFESTS = (
    "pyproject.toml", "setup.cfg", "requirements.txt", "environment.yml", "package.json",
    "Cargo.toml", "go.mod", "Gemfile", "pom.xml", "build.gradle", "build.gradle.kts",
    "composer.json", "DESCRIPTION", "Project.toml", "pubspec.yaml", "mix.exs",
)  # fmt: skip
README_NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md", "Readme.md")
README_CHARS = 2000
MANIFEST_CHARS = 1200
MAX_WALK_FILES = 20000
MAX_PROJECTS = 50


@dataclass
class Project:
    root: Path
    location: str  # relative to the source folder
    to_read: list[Path] = field(default_factory=list)
    extensions: Counter[str] = field(default_factory=Counter)


@dataclass
class Plan:
    """What a scan of one folder would read, before anything is read."""

    folder: Path
    projects: list[Project] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)

    @property
    def files(self) -> list[str]:
        return [p.relative_to(self.folder).as_posix() for pr in self.projects for p in pr.to_read]


def _is_project(path: Path) -> bool:
    return (path / ".git").exists() or any(
        (path / n).is_file() for n in (*MANIFESTS, *README_NAMES)
    )


def _walk(project: Project, rules: IgnoreRules, skipped: Counter[str]) -> None:
    """Count extensions under the project and pick the files to read, applying
    every privacy rule. Never follows links."""
    seen = 0
    for current, dirs, files in os.walk(project.root, followlinks=False):
        here = Path(current)
        rules.load(here)
        keep = []
        for d in sorted(dirs):
            reason = dir_skip_reason(d) or ("ignored" if rules.ignored(here / d) else None)
            if reason or (here / d).is_symlink():
                skipped[reason or "links"] += 1
            else:
                keep.append(d)
        dirs[:] = keep
        for name in sorted(files):
            seen += 1
            if seen > MAX_WALK_FILES:
                skipped["over the file limit"] += 1
                continue
            path = here / name
            # names first, so a secret or ignored file is never opened
            reason = name_reason(path, config.ONBOARD_MAX_FILE_BYTES) or (
                "ignored" if rules.ignored(path) else None
            )
            wanted = here == project.root and (name in MANIFESTS or name in README_NAMES)
            if not reason and wanted:
                reason = content_reason(path)
            if reason:
                skipped[reason] += 1
                continue
            project.extensions[path.suffix.lower() or "(none)"] += 1
            if wanted:
                project.to_read.append(path)


def plan(folder: Path, excludes: list[str] | None = None) -> Plan:
    folder = folder.expanduser().resolve()
    out = Plan(folder)
    if not folder.is_dir():
        return out
    rules = IgnoreRules(folder, excludes)
    rules.load(folder)
    if _is_project(folder):
        candidates = [folder]
    else:
        candidates = []
        for child in sorted(folder.iterdir()):
            if not child.is_dir() or child.is_symlink():
                continue
            reason = dir_skip_reason(child.name) or ("ignored" if rules.ignored(child) else None)
            if reason:
                out.skipped[reason] += 1
            elif _is_project(child):
                candidates.append(child)
    for root in candidates[:MAX_PROJECTS]:
        location = "." if root == folder else root.relative_to(folder).as_posix()
        project = Project(root, location)
        _walk(project, rules, out.skipped)
        out.projects.append(project)
    return out


def git_dates(root: Path) -> tuple[str, str, int] | None:
    """(first commit date, last commit date, commit count), or None."""
    git = shutil.which("git")
    if not git or not (root / ".git").exists():
        return None
    try:
        proc = subprocess.run(  # noqa: S603  # nosec B603
            [git, "--no-pager", "-c", "core.fsmonitor=false", "-C", str(root), "log",
             "--format=%cs", "--no-merges"],
            capture_output=True, text=True, timeout=20, check=False,
            env={"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1",
                 "HOME": "/nonexistent"},
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    dates = proc.stdout.split()
    if proc.returncode != 0 or not dates:
        return None
    return dates[-1], dates[0], len(dates)


def _read(path: Path, limit: int) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def project_excerpt(project: Project) -> str:
    name = project.root.name
    top = ", ".join(f"{ext} {n}" for ext, n in project.extensions.most_common(8))
    lines = [f"Project: {name}", f"Files by type: {top}"]
    dates = git_dates(project.root)
    if dates:
        lines += [f"First commit: {dates[0]}", f"Last commit: {dates[1]}", f"Commits: {dates[2]}"]
    for path in project.to_read:
        limit = README_CHARS if path.name in README_NAMES else MANIFEST_CHARS
        body = _read(path, limit).strip()
        if body:
            lines += ["", f"{path.name}:", body]
    return redact("\n".join(lines))


def collect(source: dict[str, Any]) -> Collected:
    folder = Path(source["value"]).expanduser()
    out = Collected()
    if not folder.is_dir():
        out.notes.append(f"folder not found: {source.get('label') or folder.name}")
        return out
    found = plan(folder, source.get("excludes") or [])
    out.skipped = dict(found.skipped)
    out.read = found.files
    for project in found.projects:
        out.items.append(
            SourceItem(source["id"], "folder", project.location, project_excerpt(project))
        )
    if not found.projects:
        out.notes.append(f"no projects found in {folder.name}")
    return out
