"""MCP server exposing the Sourcer scoring pipeline.

  python -m sourcer.mcp_server            # stdio transport

Tools:
  score_posting(posting_text, title, company)
      Parse + score arbitrary posting text against skills_vector.json.
      Costs one parse and one score call to the Claude API.
  lookup_scored(company, title_contains)
      Search postings Sourcer has already scored (local database, no API cost).
"""

from __future__ import annotations

from typing import Any

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

from . import db, llm, log
from .pipeline import scoring

LOOKUP_LIMIT = 20
# one spending total for the life of the server: the cost cap bounds all of it
BUDGET = llm.Usage()

mcp = MCPServer("sourcer", description="Score job postings against the operator's skills vector")


@mcp.tool()
def score_posting(posting_text: str, title: str = "", company: str = "") -> dict[str, Any]:
    """Score using the same input, exclusion, and model budget policy as HTTP."""
    return scoring.score_text(posting_text, title, company, usage=BUDGET)


@mcp.tool()
def lookup_scored(company: str = "", title_contains: str = "") -> list[dict[str, Any]]:
    """Search postings Sourcer has already scraped and scored. Free, reads
    the local database. Returns up to 20 matches ranked by score."""
    conn = db.connect()
    q = (
        "SELECT company, title, url, score, rationale, employment_type, "
        "start_date, status, excluded, exclusion_reason FROM postings WHERE 1=1"
    )
    args: list[Any] = []
    if company:
        q += " AND company LIKE ?"
        args.append(f"%{company}%")
    if title_contains:
        q += " AND title LIKE ?"
        args.append(f"%{title_contains}%")
    q += " ORDER BY score DESC LIMIT ?"
    args.append(LOOKUP_LIMIT)
    return [dict(r) for r in conn.execute(q, args)]


def main() -> None:
    load_dotenv()
    log.configure()
    mcp.run()


if __name__ == "__main__":
    main()
