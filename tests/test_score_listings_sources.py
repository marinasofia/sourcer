"""Community listings on Workday and Oracle sites get their text from the site API."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from sourcer import score_listings

LONG = "Analyze data with SQL and Python. " * 20


def reply(payload: Any) -> MagicMock:
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def test_workday_listing_reads_the_career_site_api() -> None:
    url = "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/New-York/Data-Intern_R123"
    with patch.object(
        score_listings.net, "get",
        return_value=reply({"jobPostingInfo": {"jobDescription": f"<p>{LONG}</p>"}}),
    ) as get:  # fmt: skip
        text, err = score_listings.fetch_description(url)
    assert err is None and text.startswith("Analyze data")
    assert get.call_args[0][0] == (
        "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/job/Data-Intern_R123"
    )


def test_oracle_listing_reads_the_requisition_details() -> None:
    url = "https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/55?x=1"
    with patch.object(
        score_listings.oracle, "_detail", return_value={"description": LONG, "location": "NY"}
    ) as detail:  # fmt: skip
        text, err = score_listings.fetch_description(url)
    assert err is None and text.startswith("Analyze data")
    detail.assert_called_once_with("egug.fa.us2.oraclecloud.com", "CX_1", "55")


def test_short_workday_text_is_reported_not_scored() -> None:
    url = "https://acme.wd5.myworkdayjobs.com/Careers/job/NY/Intern_R1"
    with patch.object(
        score_listings.net, "get", return_value=reply({"jobPostingInfo": {}})
    ):  # fmt: skip
        assert score_listings.fetch_description(url) == (None, "workday description too short")


def test_one_failed_model_call_skips_that_listing_not_the_whole_run(monkeypatch: Any) -> None:
    rows = [{"company": "A", "title": "Analyst"}, {"company": "B", "title": "Analyst"}]
    monkeypatch.setattr(score_listings, "check_titles", lambda *a, **k: 0)
    monkeypatch.setattr(score_listings, "select_rows", lambda conn, limit: rows)

    def score_one(conn: Any, client: Any, usage: Any, row: dict[str, Any], **kw: Any) -> str:
        if row["company"] == "A":
            raise RuntimeError("claude CLI error: API Error: 400")
        return "scored"

    monkeypatch.setattr(score_listings, "score_one", score_one)
    usage = MagicMock(est_cost_usd=0.0)
    done = score_listings.score_pending(
        MagicMock(), MagicMock(), usage, skills={}, profile=None, limit=5
    )
    assert done == 1
