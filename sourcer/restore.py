"""Seed the database from docs/data.json before a CI run.

CI machines start with an empty database; without this, dedupe would
re-score every posting on every scheduled run. Restoring the committed
export makes scheduled runs pay only for postings not seen before. Run
history is restored too so aggregate exports count every run, not just the
current one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import config, db, log
from .export import DOCS_DATA

logger = log.get_logger(__name__)


def restore(conn: db.Connection | None = None, data_path: Path | None = None) -> tuple[int, int]:
    """Insert postings and run logs from the export. Returns (postings, runs) restored."""
    source = data_path or DOCS_DATA
    if not source.exists():
        logger.info("no %s to restore from; starting fresh", source)
        return 0, 0
    data: dict[str, Any] = json.loads(source.read_text())
    conn = conn or db.connect()
    postings = data.get("postings", [])
    # postings dedupe on their hash; run logs have no key, so dedupe on run_ts
    have = {r["run_ts"] for r in conn.execute("SELECT run_ts FROM run_logs")}
    runs = [r for r in data.get("runs", []) if r.get("run_ts") not in have]
    for p in postings:
        db.insert_posting(conn, p)
    for r in runs:
        db.insert_run_log(conn, r, run_ts=r.get("run_ts"))
    _restore_company_state(conn, data)
    _restore_preferences(conn, data)
    conn.commit()
    logger.info(
        "restored %d postings and %d runs into %s", len(postings), len(runs), config.DB_PATH
    )
    return len(postings), len(runs)


def _restore_company_state(conn: db.Connection, data: dict[str, Any]) -> None:
    for row in data.get("companies", []):
        record = {
            "name": row["name"],
            "domain": row.get("domain"),
            "source": row.get("source"),
            "quality_status": row.get("quality_status", "pending"),
            "quality_signals": json.loads(row.get("quality_signals") or "{}"),
        }
        db.save_discovered_companies(conn, [record])
        if row.get("probed_at"):
            db.save_company_probe(
                conn,
                {
                    "name": row["name"],
                    "domain": row.get("domain"),
                    "ats_type": row.get("ats_type"),
                    "ats_slug": row.get("ats_slug"),
                    "urls_tried": json.loads(row.get("probe_urls") or "[]"),
                },
            )
        conn.execute(
            "UPDATE companies SET added_at=?, last_seen_at=?, probed_at=?, source=? WHERE name=?",
            (
                row["added_at"],
                row.get("last_seen_at"),
                row.get("probed_at"),
                row.get("source"),
                row["name"],
            ),
        )


def _restore_preferences(conn: db.Connection, data: dict[str, Any]) -> None:
    by_identity = {
        db.posting_hash(row["company"], row["title"], row["location"] or ""): row["id"]
        for row in conn.execute("SELECT id, company, title, location FROM postings")
    }
    for row in data.get("feedback", []):
        posting_id = by_identity.get(row["identity"])
        if posting_id is not None:
            db.record_feedback(conn, posting_id, int(row["value"]))
    notified = [
        by_identity[identity]
        for row in data.get("postings", [])
        if row.get("telegram_notified")
        and (identity := db.posting_hash(row["company"], row["title"], row.get("location") or ""))
        in by_identity
    ]
    db.mark_telegram_notified(conn, notified)


def main() -> int:
    log.configure()
    n_postings, n_runs = restore()
    print(f"restored {n_postings} postings, {n_runs} runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
