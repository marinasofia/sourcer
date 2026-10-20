"""Snowflake storage backend, same interface as ``sourcer.sqlitedb``.

Selected with ``SOURCER_BACKEND=snowflake`` (default is sqlite). Connection
comes from environment variables:

  SNOWFLAKE_ACCOUNT     e.g. abc12345.us-east-1
  SNOWFLAKE_USER
  SNOWFLAKE_PASSWORD
  SNOWFLAKE_WAREHOUSE   default COMPUTE_WH
  SNOWFLAKE_DATABASE    default SOURCER
  SNOWFLAKE_SCHEMA      default PUBLIC

``python -m sourcer.snowdb check`` verifies the connection and prints row
counts. Snowflake does not enforce UNIQUE constraints, so posting uniqueness
is guaranteed by MERGE on the hash instead of INSERT OR IGNORE.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATUSES = ("new", "shortlist", "applied", "interview", "offer", "rejected", "archived")
APPLICATION_STATUSES = (
    "applied",
    "viewed",
    "interview",
    "offer",
    "rejected",
    "ghosted",
    "withdrawn",
    "closed",
)
APPLICATION_EVENTS = ("captured", "posting_closed", "followup_due", "email_reply", "status_set")
APPLICATION_FIELDS = (
    "posting_id",
    "company",
    "title",
    "location",
    "url",
    "ats",
    "applied_at",
    "posted_at",
    "closes_at",
    "posting_age_days",
    "resume_path",
    "resume_sha256",
    "jd_snapshot_path",
    "keyword_hits",
    "keyword_misses",
    "next_action",
    "next_action_at",
    "referral",
    "notes",
)

DDL = [
    """CREATE TABLE IF NOT EXISTS postings (
        id INTEGER AUTOINCREMENT,
        hash STRING, company STRING, title STRING, url STRING,
        location STRING, employment_type STRING, start_date STRING,
        requirements STRING, seniority STRING, years_experience_min INTEGER,
        tech_stack STRING, score INTEGER, rationale STRING,
        timing_unclear BOOLEAN DEFAULT FALSE, excluded BOOLEAN DEFAULT FALSE,
        exclusion_reason STRING, date_scraped STRING,
        status STRING DEFAULT 'new', status_updated STRING, notes STRING,
        source STRING DEFAULT 'ats', posted_at STRING,
        alerted BOOLEAN DEFAULT FALSE, verified STRING, verified_at STRING,
        level_fit STRING, work_mode STRING, telegram_notified BOOLEAN DEFAULT FALSE
    )""",
    """CREATE TABLE IF NOT EXISTS feedback (
        posting_id INTEGER, value INTEGER, created_at STRING
    )""",
    """CREATE TABLE IF NOT EXISTS run_logs (
        id INTEGER AUTOINCREMENT,
        run_ts STRING, companies_covered INTEGER, postings_seen INTEGER,
        prefiltered_out INTEGER, new_postings INTEGER, dedupe_count INTEGER,
        parse_failures INTEGER, score_failures INTEGER, excluded_count INTEGER,
        timing_unclear_count INTEGER, prompt_tokens INTEGER,
        completion_tokens INTEGER, est_cost_usd FLOAT, duration_s FLOAT,
        node_metrics STRING, exclusions STRING
    )""",
    """CREATE TABLE IF NOT EXISTS companies (
        id INTEGER AUTOINCREMENT,
        name STRING, domain STRING, ats_type STRING, ats_slug STRING,
        source STRING, added_at STRING, quality_signals STRING,
        quality_status STRING DEFAULT 'pending', last_seen_at STRING,
        submit_enabled BOOLEAN DEFAULT FALSE, probe_status STRING,
        probe_urls STRING, probed_at STRING
    )""",
    """CREATE TABLE IF NOT EXISTS applications (
        id INTEGER AUTOINCREMENT,
        posting_id INTEGER, company STRING, title STRING, location STRING,
        url STRING, ats STRING, applied_at STRING, posted_at STRING,
        closes_at STRING, posting_age_days INTEGER, resume_path STRING,
        resume_sha256 STRING, jd_snapshot_path STRING, keyword_hits STRING,
        keyword_misses STRING, status STRING DEFAULT 'applied',
        status_changed_at STRING, next_action STRING, next_action_at STRING,
        referral STRING, notes STRING
    )""",
    """CREATE TABLE IF NOT EXISTS application_events (
        id INTEGER AUTOINCREMENT,
        application_id INTEGER, at STRING, kind STRING, detail STRING
    )""",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS probe_status STRING",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS probe_urls STRING",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS probed_at STRING",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS quality_status STRING DEFAULT 'pending'",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS last_seen_at STRING",
    "ALTER TABLE postings ADD COLUMN IF NOT EXISTS telegram_notified BOOLEAN DEFAULT FALSE",
    "ALTER TABLE postings ADD COLUMN IF NOT EXISTS description STRING",
]


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Rows(list[dict[str, Any]]):
    """List of lowercase-keyed dict rows with sqlite-like accessors."""

    def fetchone(self) -> dict[str, Any] | None:
        return self[0] if self else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self)


class Conn:
    """The ``sqlite3.Connection`` surface the rest of the code relies on.

    ``execute`` with qmark params returning name-addressable rows,
    ``executemany``, ``commit``, ``close``.
    """

    def __init__(self, raw: Any) -> None:
        self.raw = raw

    def execute(self, sql: str, params: Sequence[Any] = (), /) -> Rows:
        cur = self.raw.cursor()
        cur.execute(sql, list(params))
        if cur.description is None:
            return Rows()
        cols = [c[0].lower() for c in cur.description]
        return Rows(dict(zip(cols, row, strict=True)) for row in cur.fetchall())

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]], /) -> Rows:
        for params in seq_of_params:
            self.execute(sql, params)
        return Rows()

    def commit(self) -> None:
        self.raw.commit()

    def close(self) -> None:
        self.raw.close()


def connect(path: Path | None = None) -> Conn:
    """Open a connection from the SNOWFLAKE_* environment. ``path`` is ignored."""
    del path  # interface parity with the sqlite backend
    import snowflake.connector  # noqa: PLC0415  optional dependency, imported on use

    snowflake.connector.paramstyle = "qmark"
    raw = snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        database=os.environ.get("SNOWFLAKE_DATABASE", "SOURCER"),
        schema=os.environ.get("SNOWFLAKE_SCHEMA", "PUBLIC"),
    )
    conn = Conn(raw)
    for ddl in DDL:
        conn.execute(ddl)
    return conn


def _identity_part(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold()
    text = re.sub(r"\bnyc\b|new york city", "new york", text)
    text = re.sub(r"\bu\.?s\.?(?:a\.?)?\b|united states of america", "united states", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def posting_hash(company: str, title: str, location: str) -> str:
    identity = "|".join(_identity_part(value) for value in (company, title, location))
    return hashlib.sha256(identity.encode()).hexdigest()


def seen_hashes(conn: Conn) -> set[str]:
    return {r["hash"] for r in conn.execute("SELECT hash FROM postings")}


def seen_identities(conn: Conn) -> dict[str, str]:
    return {
        posting_hash(row["company"], row["title"], row.get("location") or ""): row.get("source")
        or "ats"
        for row in conn.execute("SELECT company, title, location, source FROM postings")
    }


def find_scored_twin(
    conn: Conn, company: str, title: str, current: Any = None
) -> dict[str, Any] | None:
    # no score_trace column here, so a check for today's scorer never passes
    rows = conn.execute(
        "SELECT id, score, rationale, requirements, tech_stack, seniority, "
        "       years_experience_min, timing_unclear, excluded, exclusion_reason, "
        "       NULL AS score_trace "
        "FROM postings "
        "WHERE lower(trim(company)) = lower(trim(?)) "
        "  AND lower(trim(title)) = lower(trim(?)) "
        "  AND score IS NOT NULL "
        "ORDER BY id",
        (company, title),
    ).fetchall()
    return next((r for r in rows if current is None or current(r)), None)


def insert_posting(conn: Conn, p: dict[str, Any]) -> None:
    _insert_posting(conn, p)
    if p.get("description"):
        save_description(conn, p["hash"], p["description"])


def save_description(conn: Conn, identity: str, text: str) -> None:
    conn.execute(
        "UPDATE postings SET description=? "
        "WHERE hash=? AND (description IS NULL OR description='')",
        (text, identity),
    )


def _insert_posting(conn: Conn, p: dict[str, Any]) -> None:
    p["hash"] = posting_hash(p["company"], p["title"], p.get("location") or "")
    if p.get("source", "ats") == "ats":
        conn.execute(
            """UPDATE postings SET company=?, title=?, url=?, location=?,
               employment_type=?, start_date=?, requirements=?, seniority=?,
               years_experience_min=?, tech_stack=?, score=?, rationale=?,
               timing_unclear=?, excluded=?, exclusion_reason=?, date_scraped=?,
               source='ats', posted_at=?, level_fit=?, work_mode=?
               WHERE hash=? AND COALESCE(source, 'ats') != 'ats'""",
            (
                p["company"],
                p["title"],
                p["url"],
                p.get("location"),
                p.get("employment_type"),
                p.get("start_date"),
                json.dumps(p.get("requirements", [])),
                p.get("seniority"),
                p.get("years_experience_min"),
                json.dumps(p.get("tech_stack", [])),
                p.get("score"),
                p.get("rationale"),
                bool(p.get("timing_unclear")),
                bool(p.get("excluded")),
                p.get("exclusion_reason"),
                p.get("date_scraped") or now_iso(),
                p.get("posted_at"),
                p.get("level_fit"),
                p.get("work_mode"),
                p["hash"],
            ),
        )
    conn.execute(
        """MERGE INTO postings t USING (SELECT ? AS hash) s ON t.hash = s.hash
           WHEN NOT MATCHED THEN INSERT
           (hash, company, title, url, location, employment_type, start_date,
            requirements, seniority, years_experience_min, tech_stack, score,
            rationale, timing_unclear, excluded, exclusion_reason, date_scraped,
            source, posted_at, alerted, level_fit, work_mode,
            status, status_updated, notes, verified, verified_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            p["hash"],
            p["hash"],
            p["company"],
            p["title"],
            p["url"],
            p.get("location"),
            p.get("employment_type"),
            p.get("start_date"),
            json.dumps(p.get("requirements", [])),
            p.get("seniority"),
            p.get("years_experience_min"),
            json.dumps(p.get("tech_stack", [])),
            p.get("score"),
            p.get("rationale"),
            bool(p.get("timing_unclear")),
            bool(p.get("excluded")),
            p.get("exclusion_reason"),
            p.get("date_scraped") or now_iso(),
            p.get("source", "ats"),
            p.get("posted_at"),
            bool(p.get("alerted")),
            p.get("level_fit"),
            p.get("work_mode"),
            p.get("status", "new"),
            p.get("status_updated"),
            p.get("notes"),
            p.get("verified"),
            p.get("verified_at"),
        ),
    )


def insert_run_log(conn: Conn, stats: dict[str, Any], run_ts: str | None = None) -> None:
    conn.execute(
        """INSERT INTO run_logs
           (run_ts, companies_covered, postings_seen, prefiltered_out, new_postings,
            dedupe_count, parse_failures, score_failures, excluded_count,
            timing_unclear_count, prompt_tokens, completion_tokens, est_cost_usd,
            duration_s, node_metrics, exclusions)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run_ts or now_iso(),
            stats.get("companies_covered"),
            stats.get("postings_seen"),
            stats.get("prefiltered_out"),
            stats.get("new_postings"),
            stats.get("dedupe_count"),
            stats.get("parse_failures"),
            stats.get("score_failures"),
            stats.get("excluded_count"),
            stats.get("timing_unclear_count"),
            stats.get("prompt_tokens"),
            stats.get("completion_tokens"),
            stats.get("est_cost_usd"),
            stats.get("duration_s"),
            _as_json(stats.get("node_metrics", {})),
            _as_json(stats.get("exclusions", [])),
        ),
    )
    conn.commit()


def _as_json(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def upsert_companies(conn: Conn, companies: Sequence[dict[str, Any]]) -> int:
    """Seed the companies table by MERGE on name. Returns the number inserted."""
    entries = list(companies)
    if any("submit_enabled" in company for company in entries):
        raise ValueError(
            "submit_enabled is private operator state and is not accepted from company input"
        )
    before = _count_companies(conn)
    now = now_iso()
    for c in entries:
        conn.execute(
            """MERGE INTO companies t USING (SELECT ? AS name) s ON t.name = s.name
               WHEN MATCHED THEN UPDATE SET
                 domain = COALESCE(?, t.domain), ats_type = ?, ats_slug = ?,
                 quality_status = 'passed', last_seen_at = ?
               WHEN NOT MATCHED THEN INSERT
               (name, domain, ats_type, ats_slug, source, added_at, quality_signals,
                quality_status, last_seen_at, submit_enabled)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                c["company"],
                c.get("domain"),
                c.get("ats"),
                c.get("slug"),
                now,
                c["company"],
                c.get("domain"),
                c.get("ats"),
                c.get("slug"),
                c.get("source", "companies.json"),
                now,
                json.dumps(c.get("quality_signals", {})),
                "passed",
                now,
                False,
            ),
        )
    names = [c["company"] for c in entries]
    if names:
        placeholders = ",".join("?" for _ in names)
        # Only placeholder tokens are interpolated; company names remain bound values.
        conn.execute(
            f"UPDATE companies SET submit_enabled=FALSE WHERE name NOT IN ({placeholders})",  # noqa: S608  # nosec B608
            names,
        )
    else:
        conn.execute("UPDATE companies SET submit_enabled=FALSE")
    conn.commit()
    return int(_count_companies(conn) - before)


def _count_companies(conn: Conn) -> int:
    row = conn.execute("SELECT count(*) AS n FROM companies").fetchone()
    return int(next(iter(row.values()))) if row else 0


def list_companies(conn: Conn) -> list[dict[str, Any]]:
    return conn.execute("SELECT * FROM companies ORDER BY id").fetchall()


def save_discovered_companies(conn: Conn, candidates: Sequence[dict[str, Any]]) -> int:
    """Upsert discovery records by case-insensitive company name."""
    before = _count_companies(conn)
    now = now_iso()
    for candidate in candidates:
        if "submit_enabled" in candidate:
            raise ValueError("discovery input cannot set submit_enabled")
        status = str(candidate.get("quality_status", "pending"))
        if status not in {"passed", "pending", "rejected"}:
            raise ValueError(f"invalid quality_status: {status}")
        name = str(candidate["name"]).strip()
        domain = candidate.get("domain")
        source = candidate.get("source")
        signals = json.dumps(candidate.get("quality_signals", {}))
        conn.execute(
            """MERGE INTO companies t
               USING (SELECT ? AS name) s ON lower(trim(t.name))=lower(trim(s.name))
               WHEN MATCHED THEN UPDATE SET
                 domain=COALESCE(?, t.domain), source=?, quality_signals=?,
                 quality_status=CASE WHEN t.quality_status='passed' THEN 'passed' ELSE ? END,
                 last_seen_at=?
               WHEN NOT MATCHED THEN INSERT
                 (name, domain, source, added_at, quality_signals, quality_status,
                  last_seen_at, submit_enabled)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                name,
                domain,
                source,
                signals,
                status,
                now,
                name,
                domain,
                source,
                now,
                signals,
                status,
                now,
                False,
            ),
        )
    conn.commit()
    return int(_count_companies(conn) - before)


def save_company_probe(conn: Conn, result: dict[str, Any]) -> None:
    """Store one probe result by MERGE on company name."""
    name = str(result["name"])
    domain = result.get("domain")
    ats_type = result.get("ats_type")
    ats_slug = result.get("ats_slug")
    urls_tried = result.get("urls_tried", [])
    status = "resolved" if ats_type and ats_slug else "unresolved"
    now = now_iso()
    conn.execute(
        """MERGE INTO companies t USING (SELECT ? AS name) s ON t.name = s.name
           WHEN MATCHED THEN UPDATE SET
             domain=COALESCE(?, t.domain), ats_type=?, ats_slug=?,
             probe_status=?, probe_urls=?, probed_at=?
           WHEN NOT MATCHED THEN INSERT
             (name, domain, ats_type, ats_slug, source, added_at, quality_signals,
              submit_enabled, probe_status, probe_urls, probed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            name,
            domain,
            ats_type,
            ats_slug,
            status,
            json.dumps(list(urls_tried)),
            now,
            name,
            domain,
            ats_type,
            ats_slug,
            "probe",
            now,
            "{}",
            False,
            status,
            json.dumps(list(urls_tried)),
            now,
        ),
    )
    conn.commit()


def set_status(conn: Conn, posting_id: int, status: str, note: str | None = None) -> int:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    conn.execute(
        "UPDATE postings SET status=?, status_updated=?, notes=COALESCE(?, notes) WHERE id=?",
        (status, now_iso(), note, posting_id),
    )
    conn.commit()
    return 1


def record_feedback(conn: Conn, posting_id: int, value: int) -> int:
    if value not in {-1, 1}:
        raise ValueError("feedback value must be -1 or 1")
    if conn.execute("SELECT 1 AS found FROM postings WHERE id=?", (posting_id,)).fetchone() is None:
        return 0
    conn.execute(
        """MERGE INTO feedback t USING (SELECT ? AS posting_id) s
           ON t.posting_id=s.posting_id
           WHEN MATCHED THEN UPDATE SET value=?, created_at=?
           WHEN NOT MATCHED THEN INSERT (posting_id, value, created_at) VALUES (?,?,?)""",
        (posting_id, value, now_iso(), posting_id, value, now_iso()),
    )
    conn.commit()
    return 1


def list_feedback(conn: Conn) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT f.posting_id, f.value, p.company, p.title
           FROM feedback f JOIN postings p ON p.id=f.posting_id ORDER BY f.created_at"""
    ).fetchall()


def list_digest_postings(conn: Conn, threshold: int) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT id, score, company, title, url, location, rationale, source, posted_at
           FROM postings WHERE excluded=FALSE AND status='new'
             AND telegram_notified=FALSE AND score>=? ORDER BY company, score DESC""",
        (threshold,),
    ).fetchall()


def mark_telegram_notified(conn: Conn, posting_ids: Sequence[int]) -> None:
    for posting_id in posting_ids:
        conn.execute("UPDATE postings SET telegram_notified=TRUE WHERE id=?", (posting_id,))
    conn.commit()


def weekly_metrics(conn: Conn) -> dict[str, Any]:
    cutoff = (
        datetime.fromordinal(datetime.now(UTC).date().toordinal() - 7)
        .replace(tzinfo=UTC)
        .isoformat(timespec="seconds")
    )
    company_row = conn.execute(
        "SELECT count(*) AS n FROM companies WHERE added_at>=?", (cutoff,)
    ).fetchone()
    sources = conn.execute(
        """SELECT source, count(*) AS seen,
           sum(CASE WHEN status IN ('applied','interview','offer') THEN 1 ELSE 0 END) AS applied,
           sum(CASE WHEN status IN ('interview','offer') THEN 1 ELSE 0 END) AS responses
           FROM postings WHERE date_scraped>=? GROUP BY source ORDER BY source""",
        (cutoff,),
    ).fetchall()
    return {"companies_added": company_row["n"] if company_row else 0, "sources": sources}


def check() -> str:
    """Connect and describe the account. Raises SystemExit with guidance when unconfigured."""
    missing = [
        v
        for v in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD")
        if not os.environ.get(v)
    ]
    if missing:
        raise SystemExit(
            f"not configured: set {', '.join(missing)} "
            "(free trial: https://signup.snowflake.com, then create "
            "database SOURCER in the trial account)"
        )
    conn = connect()
    ver = conn.execute("SELECT CURRENT_VERSION() AS v").fetchone()
    n_post = conn.execute("SELECT COUNT(*) AS n FROM postings").fetchone()
    n_runs = conn.execute("SELECT COUNT(*) AS n FROM run_logs").fetchone()
    version = ver["v"] if ver else "unknown"
    postings = n_post["n"] if n_post else 0
    runs = n_runs["n"] if n_runs else 0
    return f"connected: Snowflake {version}; postings={postings} run_logs={runs}"


def main(argv: list[str]) -> int:
    if argv[:1] == ["check"]:
        print(check())
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))


def _application_values(app: dict[str, Any]) -> list[Any]:
    missing = [
        k
        for k in (
            "company",
            "title",
            "url",
            "applied_at",
            "resume_path",
            "resume_sha256",
            "jd_snapshot_path",
        )
        if not app.get(k)
    ]
    if missing:
        raise ValueError(f"application is missing {missing}")
    out = []
    for k in APPLICATION_FIELDS:
        v = app.get(k)
        out.append(
            json.dumps(v) if k in ("keyword_hits", "keyword_misses") and v is not None else v
        )
    return out


def insert_application(conn: Conn, app: dict[str, Any]) -> int:
    """Snowflake has no lastrowid, so the new id is read back by url and time."""
    values = _application_values(app)
    cols = ", ".join(APPLICATION_FIELDS)
    marks = ",".join("?" * len(APPLICATION_FIELDS))
    conn.execute(
        f"INSERT INTO applications ({cols}, status_changed_at) VALUES ({marks},?)",  # noqa: S608  # nosec B608
        (*values, app["applied_at"]),
    )
    row = conn.execute(
        "SELECT MAX(id) AS id FROM applications WHERE url=? AND applied_at=?",
        (app["url"], app["applied_at"]),
    ).fetchone()
    new_id = next(iter(row.values()), None) if row else None
    app_id = int(new_id) if new_id is not None else 0
    conn.execute(
        "INSERT INTO application_events (application_id, at, kind, detail) VALUES (?,?,?,?)",
        (app_id, app["applied_at"], "captured", None),
    )
    conn.commit()
    return app_id


def add_application_event(
    conn: Conn, application_id: int, kind: str, detail: str | None = None
) -> None:
    if kind not in APPLICATION_EVENTS:
        raise ValueError(f"event kind must be one of {APPLICATION_EVENTS}")
    conn.execute(
        "INSERT INTO application_events (application_id, at, kind, detail) VALUES (?,?,?,?)",
        (application_id, now_iso(), kind, detail),
    )
    conn.commit()


def set_application_status(
    conn: Conn, application_id: int, status: str, detail: str | None = None
) -> int:
    if status not in APPLICATION_STATUSES:
        raise ValueError(f"status must be one of {APPLICATION_STATUSES}")
    if (
        conn.execute("SELECT 1 AS found FROM applications WHERE id=?", (application_id,)).fetchone()
        is None
    ):
        return 0
    at = now_iso()
    conn.execute(
        "UPDATE applications SET status=?, status_changed_at=? WHERE id=?",
        (status, at, application_id),
    )
    conn.execute(
        "INSERT INTO application_events (application_id, at, kind, detail) VALUES (?,?,?,?)",
        (application_id, at, "status_set", status if detail is None else f"{status}: {detail}"),
    )
    conn.commit()
    return 1


def list_applications(conn: Conn) -> list[dict[str, Any]]:
    return conn.execute("SELECT * FROM applications ORDER BY applied_at DESC").fetchall()


def list_application_events(conn: Conn, application_id: int) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM application_events WHERE application_id=? ORDER BY at, id",
        (application_id,),
    ).fetchall()
