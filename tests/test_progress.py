"""Run progress: a file a live page can poll, written only when asked for."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sourcer import progress


def test_unset_path_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOURCER_PROGRESS", raising=False)
    progress.start_run()
    progress.stage("fetch", done=1, total=2)
    progress.event("hello")
    progress.finish_run()
    assert list(tmp_path.iterdir()) == []


def test_stages_and_feed_land_in_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "progress.json"
    monkeypatch.setenv("SOURCER_PROGRESS", str(out))
    progress.start_run()
    progress.stage("fetch", done=0, total=3)
    progress.stage("fetch", done=2, item="Acme")
    progress.stage("fetch", status="done")
    for n in range(progress.MAX_EVENTS + 5):
        progress.event(f"event {n}", "match")
    progress.finish_run()
    data = json.loads(out.read_text())
    fetch = data["stages"]["fetch"]
    assert (fetch["done"], fetch["total"], fetch["item"], fetch["status"]) == (2, 3, "Acme", "done")
    assert "ended" in fetch and data["finished"] is not None
    assert len(data["events"]) == progress.MAX_EVENTS
    assert data["events"][-1]["text"] == f"event {progress.MAX_EVENTS + 4}"
