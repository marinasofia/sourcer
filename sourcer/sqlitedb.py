"""SQLite storage backend: postings, run_logs, companies, and applications.

The default backend. ``sourcer.db`` exposes this module's functions through the
storage facade; callers never import it directly.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hash TEXT UNIQUE NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    location TEXT,
    employment_type TEXT,
    start_date TEXT,
    requirements TEXT,
    seniority TEXT,
    years_experience_min INTEGER,
    tech_stack TEXT,
    score INTEGER,
    rationale TEXT,
    timing_unclear INTEGER DEFAULT 0,
    excluded INTEGER DEFAULT 0,
    exclusion_reason TEXT,
    date_scraped TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_ts TEXT NOT NULL,
    companies_covered INTEGER,
    postings_seen INTEGER,
    prefiltered_out INTEGER,
    new_postings INTEGER,
    dedupe_count INTEGER,
    parse_failures INTEGER,
    score_failures INTEGER,
    excluded_count INTEGER,
    timing_unclear_count INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    est_cost_usd REAL,
    duration_s REAL,
    node_metrics TEXT,
    exclusions TEXT
);
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    domain TEXT,
    ats_type TEXT,
    ats_slug TEXT,
    source TEXT,
    added_at TEXT NOT NULL,
    quality_signals TEXT,
    quality_status TEXT NOT NULL DEFAULT 'pending',
    last_seen_at TEXT,
    submit_enabled INTEGER NOT NULL DEFAULT 0,
    probe_status TEXT,
    probe_urls TEXT,
    probed_at TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    posting_id INTEGER PRIMARY KEY,
    value INTEGER NOT NULL CHECK(value IN (-1, 1)),
    created_at TEXT NOT NULL,
    FOREIGN KEY(posting_id) REFERENCES postings(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    posting_id INTEGER REFERENCES postings(id),
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    url TEXT NOT NULL,
    ats TEXT,
    applied_at TEXT NOT NULL,
    posted_at TEXT,
    closes_at TEXT,
    posting_age_days INTEGER,
    resume_path TEXT NOT NULL,
    resume_sha256 TEXT NOT NULL,
    jd_snapshot_path TEXT NOT NULL,
    keyword_hits TEXT,
    keyword_misses TEXT,
    status TEXT NOT NULL DEFAULT 'applied',
    status_changed_at TEXT,
    next_action TEXT,
    next_action_at TEXT,
    referral TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS application_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS application_events_by_app ON application_events(application_id, at);
"""

# Additive migrations for databases created by older versions. Order matters
# only for readability; every entry is idempotent.
MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("status", "TEXT DEFAULT 'new'"),
    ("status_updated", "TEXT"),
    ("notes", "TEXT"),
    ("source", "TEXT DEFAULT 'ats'"),
    ("posted_at", "TEXT"),
    ("alerted", "INTEGER DEFAULT 0"),
    ("verified", "TEXT"),
    ("verified_at", "TEXT"),
    ("level_fit", "TEXT"),
    ("work_mode", "TEXT"),
    ("telegram_notified", "INTEGER DEFAULT 0"),
    ("description", "TEXT"),
    ("score_trace", "TEXT"),  # how the score was made: input, every read, the rule, the formula
)
COMPANY_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("quality_status", "TEXT NOT NULL DEFAULT 'pending'"),
    ("last_seen_at", "TEXT"),
    ("probe_status", "TEXT"),
    ("probe_urls", "TEXT"),
    ("probed_at", "TEXT"),
)

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


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# A writer waits this long for another to finish instead of failing at once
# with "database is locked": the command server, the morning run and the
# extension all write to one file.
BUSY_SECONDS = 30


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open (and create or migrate) the database at ``path`` or the configured default.

    Write ahead logging lets pages read while a run writes."""
    target = path or config.DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=BUSY_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(postings)")}
    for col, decl in MIGRATIONS:
        if col not in have:
            conn.execute(f"ALTER TABLE postings ADD COLUMN {col} {decl}")
    company_columns = {r["name"] for r in conn.execute("PRAGMA table_info(companies)")}
    for col, decl in COMPANY_MIGRATIONS:
        if col not in company_columns:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {decl}")
    conn.commit()


def _identity_part(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold()
    text = re.sub(r"\bnyc\b|new york city", "new york", text)
    text = re.sub(r"\bu\.?s\.?(?:a\.?)?\b|united states of america", "united states", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def posting_hash(company: str, title: str, location: str) -> str:
    """Stable dedupe key: normalized company, title, and location."""
    identity = "|".join(_identity_part(value) for value in (company, title, location))
    return hashlib.sha256(identity.encode()).hexdigest()


def seen_hashes(conn: sqlite3.Connection) -> set[str]:
    return {r["hash"] for r in conn.execute("SELECT hash FROM postings")}


def seen_identities(conn: sqlite3.Connection) -> dict[str, str]:
    """Current normalized identities and their winning source."""
    return {
        posting_hash(row["company"], row["title"], row["location"] or ""): row["source"] or "ats"
        for row in conn.execute("SELECT company, title, location, source FROM postings")
    }


def set_status(
    conn: sqlite3.Connection, posting_id: int, status: str, note: str | None = None
) -> int:
    """Update a posting's tracker status. Returns the number of rows changed."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    cur = conn.execute(
        "UPDATE postings SET status=?, status_updated=?, notes=COALESCE(?, notes) WHERE id=?",
        (status, now_iso(), note, posting_id),
    )
    if cur.rowcount:
        from . import trace  # noqa: PLC0415  # trace imports config only; no cycle at load

        trace.record(conn, posting_id, "status", status, note=note)
    conn.commit()
    return cur.rowcount


def record_feedback(conn: sqlite3.Connection, posting_id: int, value: int) -> int:
    """Record one bounded preference signal for an existing posting."""
    if value not in {-1, 1}:
        raise ValueError("feedback value must be -1 or 1")
    if conn.execute("SELECT 1 FROM postings WHERE id=?", (posting_id,)).fetchone() is None:
        return 0
    conn.execute(
        """INSERT INTO feedback (posting_id, value, created_at) VALUES (?,?,?)
           ON CONFLICT(posting_id) DO UPDATE SET value=excluded.value,
           created_at=excluded.created_at""",
        (posting_id, value, now_iso()),
    )
    conn.commit()
    return 1


def list_feedback(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT f.posting_id, f.value, p.company, p.title
           FROM feedback f JOIN postings p ON p.id=f.posting_id
           ORDER BY f.created_at"""
    )
    return [dict(row) for row in rows]


def list_digest_postings(conn: sqlite3.Connection, threshold: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT id, score, company, title, url, location, rationale, source, posted_at
           FROM postings WHERE excluded=0 AND status='new'
             AND telegram_notified=0 AND score>=? ORDER BY company, score DESC""",
        (threshold,),
    )
    return [dict(row) for row in rows]


def mark_telegram_notified(conn: sqlite3.Connection, posting_ids: Sequence[int]) -> None:
    if posting_ids:
        conn.executemany(
            "UPDATE postings SET telegram_notified=1 WHERE id=?",
            [(posting_id,) for posting_id in posting_ids],
        )
        conn.commit()


def weekly_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return aggregate seven-day funnel and source counts without personal fields."""
    cutoff = datetime.now(UTC).date().toordinal() - 7
    cutoff_iso = datetime.fromordinal(cutoff).replace(tzinfo=UTC).isoformat(timespec="seconds")
    companies = conn.execute(
        "SELECT count(*) AS n FROM companies WHERE added_at>=?", (cutoff_iso,)
    ).fetchone()["n"]
    rows = conn.execute(
        """SELECT source, count(*) AS seen,
           sum(CASE WHEN status IN ('applied','interview','offer') THEN 1 ELSE 0 END) AS applied,
           sum(CASE WHEN status IN ('interview','offer') THEN 1 ELSE 0 END) AS responses
           FROM postings WHERE date_scraped>=? GROUP BY source ORDER BY source""",
        (cutoff_iso,),
    )
    return {"companies_added": companies, "sources": [dict(row) for row in rows]}


def find_scored_twin(
    conn: sqlite3.Connection, company: str, title: str, current: Any = None
) -> Any | None:
    """An already scored posting with the same company and title.

    The dedupe hash includes the url, so one role posted to several board urls
    (per location, or re-listed) is several rows and would otherwise cost one
    model call each. Scoring is a function of company, title and requirements
    only, so the score of a twin transfers exactly. Excluded rows are eligible
    so the exclusion transfers too.

    With ``current`` (llm.current_twin), only a twin whose stored trace shows
    today's prompt, evidence and arithmetic counts: an older score is read
    again, never copied.
    """
    rows = conn.execute(
        "SELECT id, score, rationale, requirements, tech_stack, seniority, "
        "       years_experience_min, timing_unclear, excluded, exclusion_reason, score_trace "
        "FROM postings "
        "WHERE lower(trim(company)) = lower(trim(?)) "
        "  AND lower(trim(title)) = lower(trim(?)) "
        "  AND score IS NOT NULL "
        "ORDER BY id",
        (company, title),
    ).fetchall()
    return next((r for r in rows if current is None or current(r)), None)


def insert_posting(conn: sqlite3.Connection, p: dict[str, Any]) -> None:
    """Insert one posting; silently skipped when the hash already exists.

    The cleaned description is kept (matching and training need the text),
    filling it in on an existing row that has none.
    """
    _insert_posting(conn, p)
    if p.get("description"):
        save_description(conn, p["hash"], p["description"])


def save_description(conn: sqlite3.Connection, identity: str, text: str) -> None:
    conn.execute(
        "UPDATE postings SET description=? "
        "WHERE hash=? AND (description IS NULL OR description='')",
        (text, identity),
    )


def _insert_posting(conn: sqlite3.Connection, p: dict[str, Any]) -> None:
    identity = posting_hash(p["company"], p["title"], p.get("location") or "")
    p["hash"] = identity
    existing = next(
        (
            row
            for row in conn.execute("SELECT id, company, title, location, source FROM postings")
            if posting_hash(row["company"], row["title"], row["location"] or "") == identity
        ),
        None,
    )
    if existing is not None:
        if (existing["source"] or "ats") != "ats" and p.get("source", "ats") == "ats":
            conn.execute(
                """UPDATE postings SET hash=?, company=?, title=?, url=?, location=?,
                   employment_type=?, start_date=?, requirements=?, seniority=?,
                   years_experience_min=?, tech_stack=?, score=?, rationale=?,
                   timing_unclear=?, excluded=?, exclusion_reason=?, date_scraped=?,
                   source=?, posted_at=?, level_fit=?, work_mode=?, score_trace=? WHERE id=?""",
                (
                    identity,
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
                    int(bool(p.get("timing_unclear"))),
                    int(bool(p.get("excluded"))),
                    p.get("exclusion_reason"),
                    p.get("date_scraped") or now_iso(),
                    "ats",
                    p.get("posted_at"),
                    p.get("level_fit"),
                    p.get("work_mode"),
                    _trace(p),
                    existing["id"],
                ),
            )
        return
    conn.execute(
        """INSERT OR IGNORE INTO postings
           (hash, company, title, url, location, employment_type, start_date,
            requirements, seniority, years_experience_min, tech_stack, score,
            rationale, timing_unclear, excluded, exclusion_reason, date_scraped,
            source, posted_at, alerted, level_fit, work_mode,
            status, status_updated, notes, verified, verified_at, score_trace)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
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
            int(bool(p.get("timing_unclear"))),
            int(bool(p.get("excluded"))),
            p.get("exclusion_reason"),
            p.get("date_scraped") or now_iso(),
            p.get("source", "ats"),
            p.get("posted_at"),
            int(bool(p.get("alerted"))),
            p.get("level_fit"),
            p.get("work_mode"),
            p.get("status", "new"),
            p.get("status_updated"),
            p.get("notes"),
            p.get("verified"),
            p.get("verified_at"),
            _trace(p),
        ),
    )


def _trace(p: dict[str, Any]) -> str | None:
    """The score's trace as stored: JSON, or None when the posting carries none."""
    trace = p.get("score_trace")
    # a restored row carries the stored text; older exports encoded it again on
    # every cycle, so unwrap until it is the trace itself
    while isinstance(trace, str):
        try:
            trace = json.loads(trace)
        except ValueError:
            break
    return json.dumps(trace) if trace else None


def insert_run_log(
    conn: sqlite3.Connection, stats: dict[str, Any], run_ts: str | None = None
) -> None:
    """Append one run_logs row. ``run_ts`` defaults to now; pass it to restore history."""
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
    """Accept either an already serialised column or a live structure."""
    return value if isinstance(value, str) else json.dumps(value)


def upsert_companies(conn: sqlite3.Connection, companies: Sequence[dict[str, Any]]) -> int:
    """Seed the companies table from the company list. Returns the number inserted.

    A company is keyed by name. New names are inserted with ``added_at`` set
    to now; existing rows get their ATS type and slug refreshed and keep the
    operator-managed columns (``quality_signals``, ``submit_enabled``).
    """
    entries = list(companies)
    if any("submit_enabled" in company for company in entries):
        raise ValueError(
            "submit_enabled is private operator state and is not accepted from company input"
        )
    before = conn.execute("SELECT count(*) FROM companies").fetchone()[0]
    now = now_iso()
    for c in entries:
        conn.execute(
            """INSERT INTO companies
               (name, domain, ats_type, ats_slug, source, added_at, quality_signals,
                quality_status, last_seen_at, submit_enabled)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 domain=COALESCE(excluded.domain, domain),
                 ats_type=excluded.ats_type,
                 ats_slug=excluded.ats_slug,
                 quality_status='passed',
                 last_seen_at=excluded.last_seen_at""",
            (
                c["company"],
                c.get("domain"),
                c.get("ats"),
                c.get("slug"),
                c.get("source", "companies.json"),
                now,
                json.dumps(c.get("quality_signals", {})),
                "passed",
                now,
                0,
            ),
        )
    names = [c["company"] for c in entries]
    if names:
        placeholders = ",".join("?" for _ in names)
        # Only placeholder tokens are interpolated; company names remain bound values.
        conn.execute(
            f"UPDATE companies SET submit_enabled=0 WHERE name NOT IN ({placeholders})",  # noqa: S608  # nosec B608
            names,
        )
    else:
        conn.execute("UPDATE companies SET submit_enabled=0")
    conn.commit()
    after = conn.execute("SELECT count(*) FROM companies").fetchone()[0]
    return int(after - before)


def list_companies(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every company row in insertion order."""
    return [dict(r) for r in conn.execute("SELECT * FROM companies ORDER BY id")]


def save_discovered_companies(
    conn: sqlite3.Connection, candidates: Sequence[dict[str, Any]]
) -> int:
    """Upsert normalized discovery records and return the number inserted."""
    before = conn.execute("SELECT count(*) FROM companies").fetchone()[0]
    now = now_iso()
    for candidate in candidates:
        if "submit_enabled" in candidate:
            raise ValueError("discovery input cannot set submit_enabled")
        name = str(candidate["name"]).strip()
        domain = candidate.get("domain")
        existing = conn.execute(
            "SELECT id, quality_signals, quality_status FROM companies "
            "WHERE lower(trim(name))=lower(trim(?)) "
            "OR (? IS NOT NULL AND lower(domain)=lower(?)) ORDER BY id LIMIT 1",
            (name, domain, domain),
        ).fetchone()
        signals = candidate.get("quality_signals", {})
        status = str(candidate.get("quality_status", "pending"))
        if status not in {"passed", "pending", "rejected"}:
            raise ValueError(f"invalid quality_status: {status}")
        if existing is None:
            conn.execute(
                """INSERT INTO companies
                   (name, domain, source, added_at, quality_signals, quality_status,
                    last_seen_at, submit_enabled)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (name, domain, candidate.get("source"), now, json.dumps(signals), status, now, 0),
            )
            continue
        previous = json.loads(existing["quality_signals"] or "{}")
        merged = _merge_signals(previous, signals)
        if existing["quality_status"] == "passed":
            status = "passed"
        conn.execute(
            """UPDATE companies SET
               domain=COALESCE(?, domain), source=?, quality_signals=?,
               quality_status=?, last_seen_at=? WHERE id=?""",
            (
                domain,
                candidate.get("source"),
                json.dumps(merged),
                status,
                now,
                existing["id"],
            ),
        )
    conn.commit()
    after = conn.execute("SELECT count(*) FROM companies").fetchone()[0]
    return int(after - before)


def _merge_signals(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    merged = {**previous, **{key: value for key, value in current.items() if value is not None}}
    for key in ("sources", "source_urls", "tags"):
        merged[key] = list(dict.fromkeys([*previous.get(key, []), *current.get(key, [])]))
    counts = [
        value
        for value in (previous.get("employee_count"), current.get("employee_count"))
        if isinstance(value, int)
    ]
    if counts:
        merged["employee_count"] = max(counts)
    return merged


def save_company_probe(conn: sqlite3.Connection, result: dict[str, Any]) -> None:
    """Store one probe result, creating the company when it is not yet known."""
    name = str(result["name"])
    domain = result.get("domain")
    ats_type = result.get("ats_type")
    ats_slug = result.get("ats_slug")
    status = "resolved" if ats_type and ats_slug else "unresolved"
    now = now_iso()
    conn.execute(
        """INSERT INTO companies
           (name, domain, ats_type, ats_slug, source, added_at, quality_signals,
            submit_enabled, probe_status, probe_urls, probed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
             domain=COALESCE(excluded.domain, companies.domain),
             ats_type=excluded.ats_type,
             ats_slug=excluded.ats_slug,
             probe_status=excluded.probe_status,
             probe_urls=excluded.probe_urls,
             probed_at=excluded.probed_at""",
        (
            name,
            domain,
            ats_type,
            ats_slug,
            "probe",
            now,
            "{}",
            0,
            status,
            json.dumps(result.get("urls_tried", [])),
            now,
        ),
    )
    conn.commit()


def _application_values(app: dict[str, Any]) -> list[Any]:
    """Validate required fields and JSON encode the keyword lists."""
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
        out.append(_as_json(v) if k in ("keyword_hits", "keyword_misses") and v is not None else v)
    return out


def insert_application(conn: sqlite3.Connection, app: dict[str, Any]) -> int:
    """Store one submitted application plus its ``captured`` event. Returns its id."""
    values = _application_values(app)
    cols = ", ".join(APPLICATION_FIELDS)
    marks = ",".join("?" * len(APPLICATION_FIELDS))
    cur = conn.execute(
        f"INSERT INTO applications ({cols}, status_changed_at) VALUES ({marks},?)",  # noqa: S608  # nosec B608
        (*values, app["applied_at"]),
    )
    app_id = int(cur.lastrowid or 0)
    conn.execute(
        "INSERT INTO application_events (application_id, at, kind, detail) VALUES (?,?,?,?)",
        (app_id, app["applied_at"], "captured", None),
    )
    conn.commit()
    return app_id


def add_application_event(
    conn: sqlite3.Connection, application_id: int, kind: str, detail: str | None = None
) -> None:
    if kind not in APPLICATION_EVENTS:
        raise ValueError(f"event kind must be one of {APPLICATION_EVENTS}")
    conn.execute(
        "INSERT INTO application_events (application_id, at, kind, detail) VALUES (?,?,?,?)",
        (application_id, now_iso(), kind, detail),
    )
    conn.commit()


def set_application_status(
    conn: sqlite3.Connection, application_id: int, status: str, detail: str | None = None
) -> int:
    """Move an application to ``status`` and log a ``status_set`` event."""
    if status not in APPLICATION_STATUSES:
        raise ValueError(f"status must be one of {APPLICATION_STATUSES}")
    at = now_iso()
    cur = conn.execute(
        "UPDATE applications SET status=?, status_changed_at=? WHERE id=?",
        (status, at, application_id),
    )
    if cur.rowcount:
        conn.execute(
            "INSERT INTO application_events (application_id, at, kind, detail) VALUES (?,?,?,?)",
            (application_id, at, "status_set", status if detail is None else f"{status}: {detail}"),
        )
    conn.commit()
    return cur.rowcount


def list_applications(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM applications ORDER BY applied_at DESC")]


def list_application_events(conn: sqlite3.Connection, application_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM application_events WHERE application_id=? ORDER BY at, id",
        (application_id,),
    )
    return [dict(r) for r in rows]
