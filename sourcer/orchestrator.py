"""Deterministic daily and weekly orchestration with overlap protection."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import Any

from . import config, db, log
from .bot.telegram import Settings, TelegramClient, send_morning_digest, send_weekly_report
from .discovery.service import DiscoveryResult, discover
from .probe import probe_pending


class RunLock(AbstractContextManager["RunLock"]):
    """A local lock that prevents overlapping scheduled runs."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config.DB_PATH.with_suffix(".lock")
        self.fd: int | None = None

    def __enter__(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.fd)
            self.fd = None
            raise RuntimeError(f"another Sourcer run holds {self.path}") from exc
        os.ftruncate(self.fd, 0)
        os.write(self.fd, f"pid={os.getpid()}\n".encode())
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        # Keep the inode so contenders cannot acquire different lock files.


def _telegram_client() -> tuple[TelegramClient, int] | None:
    settings = Settings.from_config()
    if not settings.chat_ids or not settings.user_ids:
        return None
    chat_id = next(iter(settings.chat_ids))
    return TelegramClient(settings), chat_id


def run_daily(
    pipeline: Callable[[], dict[str, Any]],
    *,
    lock_path: Path | None = None,
) -> dict[str, Any]:
    """Collect and score, then send or preview the morning digest."""
    with RunLock(lock_path):
        state = pipeline()
        configured = _telegram_client()
        if configured:
            client, chat_id = configured
            preview = send_morning_digest(state["conn"], client, chat_id)
            state["stats"]["telegram_digest_count"] = len(preview)
        return state


def run_weekly_discovery(
    *,
    probe_limit: int = 25,
    discoverer: Callable[..., DiscoveryResult] = discover,
) -> DiscoveryResult:
    """Discover companies, then probe a bounded number with one shared limiter."""
    if probe_limit < 0:
        raise ValueError("probe_limit cannot be negative")
    conn = db.connect()
    result = discoverer(conn)
    probe_pending(conn, probe_limit)
    conn.close()
    return result


def run_weekly(
    pipeline: Callable[[], dict[str, Any]],
    *,
    probe_limit: int = 25,
    lock_path: Path | None = None,
) -> tuple[DiscoveryResult, dict[str, Any]]:
    """Run weekly discovery, collection, digest, and aggregate source report."""
    with RunLock(lock_path):
        result = run_weekly_discovery(probe_limit=probe_limit)
        state = pipeline()
        configured = _telegram_client()
        if configured:
            client, chat_id = configured
            send_morning_digest(state["conn"], client, chat_id)
            send_weekly_report(state["conn"], client, chat_id)
        return result, state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sourcer orchestrate", description=__doc__)
    parser.add_argument("cadence", choices=("daily", "weekly"))
    parser.add_argument("--probe-limit", type=int, default=25)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    log.configure()
    from .main import run  # noqa: PLC0415

    def pipeline() -> Any:
        return run()

    try:
        if args.cadence == "daily":
            state = run_daily(pipeline)
            result = {"cadence": "daily", "stats": state["stats"]}
        else:
            discovery, state = run_weekly(pipeline, probe_limit=args.probe_limit)
            result = {
                "cadence": "weekly",
                "companies_discovered": len(discovery.candidates),
                "new_companies": len(discovery.new_candidates),
                "discovery_errors": list(discovery.errors),
                "stats": state["stats"],
            }
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
