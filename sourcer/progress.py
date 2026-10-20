"""Live progress of a run, for a page that shows it as it happens.

Set SOURCER_PROGRESS to a file path and each step writes what it is doing:
which stage, how far along, the item in hand, and a short feed of events.
Unset, every call is a no-op. Writes are atomic (temp file, then rename), so
a reader never sees half a file, and safe from the fetch worker threads.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

MAX_EVENTS = 60
_lock = threading.Lock()
_state: dict[str, Any] = {}


def _path() -> Path | None:
    raw = os.environ.get("SOURCER_PROGRESS", "").strip()
    return Path(raw) if raw else None


def _write() -> None:
    path = _path()
    if path is None:
        return
    _state["updated"] = time.time()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_state, default=str))
    tmp.replace(path)


def start_run() -> None:
    """A fresh run: stages and feed start empty."""
    with _lock:
        _state.clear()
        _state.update(started=time.time(), finished=None, stages={}, events=[])
        _write()


def _ensure() -> None:
    if not _state:
        _state.update(started=time.time(), finished=None, stages={}, events=[])


def stage(
    name: str,
    *,
    done: int | None = None,
    total: int | None = None,
    item: str | None = None,
    status: str = "running",
) -> None:
    """Report where a stage is. Fields left as None keep their last value."""
    if _path() is None:
        return
    with _lock:
        _ensure()
        row = _state["stages"].setdefault(name, {"started": time.time()})
        row["status"] = status
        for key, value in (("done", done), ("total", total), ("item", item)):
            if value is not None:
                row[key] = value
        if status == "done":
            row["ended"] = time.time()
        _write()


def event(text: str, kind: str = "info") -> None:
    """One line for the feed: a match found, a board that failed, a text sent."""
    if _path() is None:
        return
    with _lock:
        _ensure()
        _state["events"] = [*_state["events"], {"at": time.time(), "kind": kind, "text": text}][
            -MAX_EVENTS:
        ]
        _write()


def finish_run() -> None:
    if _path() is None:
        return
    with _lock:
        _ensure()
        _state["finished"] = time.time()
        _write()
