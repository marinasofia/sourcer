"""Alerting: surface postings worth acting on as GitHub issues.

  python -m sourcer.alerts

Selects postings not yet alerted that meet any of:
  - score >= SOURCER_ALERT_SCORE (default 85)
  - internship or new-grad timing (any score, including unscored listings)

In CI (GITHUB_TOKEN + GITHUB_REPOSITORY set) it opens one issue per run
with the batch; locally it prints. Flood control: if a bootstrap run
surfaces more than SOURCER_ALERT_CAP rows (default 60), the issue contains
counts plus the newest slice instead of everything, and the rest are
marked alerted so they never spam a later run.
"""

from __future__ import annotations

import os
import re
import shutil

# Only the fixed gh argv in create_issue is executed, without a shell.
import subprocess  # nosec B404
import sys
from typing import Any

from . import db, log
from .safetext import one_line

logger = log.get_logger(__name__)

ALERT_SCORE = int(os.environ.get("SOURCER_ALERT_SCORE", "85"))
ALERT_CAP = int(os.environ.get("SOURCER_ALERT_CAP", "60"))

SELECT_ALERTABLE = """
    SELECT id, company, title, url, location, score, employment_type,
           start_date, source, posted_at
    FROM postings
    WHERE alerted=0 AND excluded=0 AND status='new'
      AND (level_fit IS NULL OR level_fit != 'misfit')
      AND (verified IS NULL OR verified NOT IN ('dead','closed'))
      AND (
          (score IS NOT NULL AND score >= ?)
       OR employment_type='intern'
       OR (start_date IS NOT NULL AND lower(start_date) LIKE '%new grad%'))
    ORDER BY (posted_at IS NULL), posted_at DESC, score DESC
"""


def select_alertable(conn: db.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(SELECT_ALERTABLE, (ALERT_SCORE,))]


# characters that change meaning in GitHub markdown
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#+!|~>-])")


def md_text(value: Any) -> str:
    """Board text made inert in an issue: one line, markdown characters
    escaped, and @ broken up so a company name cannot mention a user."""
    text = _MARKDOWN_SPECIAL.sub(r"\\\1", one_line(value or ""))
    return text.replace("@", "@\u200b")


def md_url(value: Any) -> str | None:
    """The URL when it is plain https and safe inside a markdown link."""
    url = one_line(value or "")
    if not url.lower().startswith("https://") or any(c in url for c in " ()<>[]`\"'"):
        return None
    return url


def format_line(p: dict[str, Any]) -> str:
    score = f"[{p['score']:>3}]" if p["score"] is not None else "[ - ]"
    tag = md_text(p["employment_type"] or "?")
    posted = f" posted {md_text(p['posted_at'])}" if p["posted_at"] else ""
    title = md_text(p["title"])
    url = md_url(p["url"])
    link = f"[{title}]({url})" if url else title
    return (
        f"- {score} **{md_text(p['company'])}**: {link} "
        f"({tag}, start: {md_text(p['start_date'] or 'n/a')}{posted})"
    )


def build_body(rows: list[dict[str, Any]]) -> str:
    shown = rows[:ALERT_CAP]
    lines = [format_line(p) for p in shown]
    body = (
        f"{len(rows)} actionable posting(s): score >= {ALERT_SCORE}, "
        f"internships, or new grad roles.\n\n" + "\n".join(lines)
    )
    if len(rows) > ALERT_CAP:
        body += (
            f"\n\n...and {len(rows) - ALERT_CAP} more (bootstrap batch); "
            f"see `python -m sourcer.track list` for the full set."
        )
    return body


def create_issue(title: str, body: str) -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    gh = shutil.which("gh")
    if gh is None:
        raise RuntimeError("gh CLI not found on PATH; cannot open the alert issue")
    # Fixed argv, a resolved executable path, and no shell interpretation.
    subprocess.run(  # noqa: S603  # nosec B603
        [gh, "api", f"repos/{repo}/issues", "-f", f"title={title}", "-f", f"body={body}"],
        check=True,
        capture_output=True,
    )


def mark_alerted(conn: db.Connection, rows: list[dict[str, Any]]) -> None:
    conn.executemany("UPDATE postings SET alerted=1 WHERE id=?", [(p["id"],) for p in rows])
    conn.commit()


def main() -> int:
    log.configure()
    conn = db.connect()
    rows = select_alertable(conn)
    if not rows:
        print("no new alertable postings")
        return 0
    n_scored = sum(1 for p in rows if p["score"] is not None)
    title = (
        f"Sourcer: {len(rows)} new target posting(s) "
        f"({n_scored} scored, {len(rows) - n_scored} listings)"
    )
    body = build_body(rows)
    if os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPOSITORY"):
        create_issue(title, body)
        logger.info("opened issue: %s", title)
    else:
        print(title)
        print(body)
    mark_alerted(conn, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
