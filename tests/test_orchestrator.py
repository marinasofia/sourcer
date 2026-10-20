"""Safe orchestration tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sourcer import orchestrator


def test_run_lock_blocks_overlap_and_cleans_up(tmp_path: Path) -> None:
    path = tmp_path / "run.lock"
    with orchestrator.RunLock(path):
        assert path.exists()
        with pytest.raises(RuntimeError, match="another Sourcer run"), orchestrator.RunLock(path):
            pass
    with orchestrator.RunLock(path):
        assert path.exists()


def test_run_lock_cleans_up_after_failure(tmp_path: Path) -> None:
    path = tmp_path / "run.lock"
    with pytest.raises(ValueError), orchestrator.RunLock(path):
        raise ValueError("failed")
    with orchestrator.RunLock(path):
        assert path.exists()


def test_daily_runs_pipeline_without_telegram_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(orchestrator, "_telegram_client", lambda: None)
    state: dict[str, Any] = {"stats": {"new_postings": 2}, "conn": object()}
    assert orchestrator.run_daily(lambda: state, lock_path=tmp_path / "run.lock") is state


def test_weekly_probe_limit_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        orchestrator.run_weekly_discovery(probe_limit=-1)
