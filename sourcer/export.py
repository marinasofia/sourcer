"""Export the full database state to docs/data.json.

The output contains per-posting scores and rationales, so in the split-repo
setup it is generated and committed only in the PRIVATE data repo; the
public repo gitignores it. The public dashboard consumes the aggregate-only
export from export_public.py instead.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import config, db, log

logger = log.get_logger(__name__)

DOCS_DATA = config.PROJECT_ROOT / "docs" / "data.json"
EM_DASH = "\u2014"
EN_DASH = "\u2013"


def clean_text(text: str) -> str:
    """House style: no em or en dashes in published text."""
    return text.replace(EM_DASH, ", ").replace(EN_DASH, "-")


def load_runs(conn: db.Connection) -> list[dict[str, Any]]:
    runs = [dict(r) for r in conn.execute("SELECT * FROM run_logs ORDER BY id")]
    for r in runs:
        r["node_metrics"] = json.loads(r["node_metrics"] or "{}")
        r["exclusions"] = json.loads(r["exclusions"] or "[]")
    return runs


def export(conn: db.Connection | None = None, out_path: Path | str | None = None) -> Path:
    conn = conn or db.connect()
    target = Path(out_path or DOCS_DATA)
    postings = [
        dict(r) for r in conn.execute("SELECT * FROM postings ORDER BY score DESC, company, title")
    ]
    for p in postings:
        p["requirements"] = json.loads(p["requirements"] or "[]")
        p["tech_stack"] = json.loads(p["tech_stack"] or "[]")
        if p.get("rationale"):
            p["rationale"] = clean_text(p["rationale"])
    runs = load_runs(conn)
    target.parent.mkdir(parents=True, exist_ok=True)
    companies = db.list_companies(conn)
    for company in companies:
        company.pop("submit_enabled", None)
    identities = {
        row["id"]: db.posting_hash(row["company"], row["title"], row.get("location") or "")
        for row in postings
    }
    feedback = [
        {"identity": identities[row["posting_id"]], "value": row["value"]}
        for row in db.list_feedback(conn)
        if row["posting_id"] in identities
    ]
    snapshot = {"postings": postings, "runs": runs, "companies": companies, "feedback": feedback}
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot, indent=1))
    temporary.chmod(0o600)
    temporary.replace(target)
    logger.info("exported %d postings, %d runs -> %s", len(postings), len(runs), target)
    return target


def main() -> int:
    log.configure()
    print(export())
    return 0


if __name__ == "__main__":
    sys.exit(main())
