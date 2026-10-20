"""Recheck before sending: the company's own board decides, not the list."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import patch

import pytest

from sourcer.apply import recheck

TODAY = date(2026, 10, 3)


class Resp:
    def __init__(self, status: int, data: Any = None, text: str = "") -> None:
        self.status_code, self._data, self.text = status, data, text
        self.ok = 200 <= status < 300
        self.is_redirect, self.headers = False, {}

    def json(self) -> Any:
        return self._data


def board(responses: dict[str, Resp]) -> Any:
    def fake(url: str, **_: Any) -> Resp:
        for prefix, resp in responses.items():
            if url.startswith(prefix):
                return resp
        return Resp(404)

    return patch.multiple(recheck, _get=fake)


FUNDWELL = "https://jobs.ashbyhq.com/fundwell/2d91136d-b54d-4ed0-93de-22c7839a3982/application"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/fundwell"


def test_old_posting_still_listed_is_held_back_only_when_its_program_ended() -> None:
    """Fundwell: listed on its own page, first posted in March for a summer 2026
    program. Age alone never holds a listed role back; its ended program does."""
    jobs = {
        "jobs": [
            {
                "id": "2d91136d-b54d-4ed0-93de-22c7839a3982",
                "isListed": True,
                "publishedAt": "2026-03-02T17:51:56.945+00:00",
            }
        ]
    }
    row = {
        "url": FUNDWELL,
        "posted_at": "2026-09-30",
        "description": "DURATION: JUNE - AUGUST 2026",
    }
    with board({ASHBY_API: Resp(200, jobs)}):
        reason, found = recheck.why_not_send(row, TODAY)
    assert found.posted == "2026-03-02" and found.via == "ashby"
    assert reason == "its program dates ended by 8/2026"


def test_fresh_listed_posting_passes() -> None:
    url = "https://job-boards.greenhouse.io/anthropic/jobs/5427938008"
    api = "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs/5427938008"
    data = {"first_published": "2026-09-30T16:10:59-04:00", "updated_at": "2026-10-02"}
    with board({api: Resp(200, data)}):
        reason, found = recheck.why_not_send({"url": url, "description": "Summer 2027"}, TODAY)
    assert reason is None and found.posted == "2026-09-30"


@pytest.mark.parametrize(
    ("url", "responses"),
    [
        (
            "https://boards.greenhouse.io/acme/jobs/1",
            {"https://boards-api.greenhouse.io": Resp(404)},
        ),
        (FUNDWELL, {ASHBY_API: Resp(200, {"jobs": []})}),
        ("https://jobs.lever.co/acme/abc", {"https://api.lever.co": Resp(404)}),
        (
            "https://acme.wd1.myworkdayjobs.com/Careers/job/NY/Analyst_R1",
            {
                "https://acme.wd1.myworkdayjobs.com/wday": Resp(
                    200,
                    {
                        "jobPostingInfo": {
                            "canApply": False,
                            "posted": True,
                            "startDate": "2026-10-01",
                        }
                    },
                )
            },
        ),
        (
            "https://careers.acme.com/jobs/1",
            {"https://careers.acme.com": Resp(200, text="This job has expired")},
        ),
    ],
)
def test_closed_postings_are_held_back(url: str, responses: dict[str, Resp]) -> None:
    with board(responses):
        reason, _ = recheck.why_not_send({"url": url}, TODAY)
    assert reason == "no longer listed on the company's board"


def test_ended_program_dates_hold_back_even_when_fresh() -> None:
    url = "https://jobs.lever.co/acme/abc"
    with board({"https://api.lever.co": Resp(200, {"createdAt": 1790000000000})}):  # 2026-09-21
        reason, _ = recheck.why_not_send(
            {"url": url, "description": "Summer 2026 internship"}, TODAY
        )
    assert reason == "its program dates ended by 8/2026"


def test_unknown_board_answer_does_not_block() -> None:
    url = "https://careers.acme.com/jobs/1"
    with board({"https://careers.acme.com": Resp(503)}):
        reason, found = recheck.why_not_send({"url": url, "posted_at": "2026-09-30"}, TODAY)
    assert reason is None and found.listed is None


PAYPAL = "https://paypal.eightfold.ai/careers/job/274922421821"
EIGHTFOLD_API = "https://paypal.eightfold.ai/api/pcsx/position_details"


def test_eightfold_asks_the_details_api_not_the_page() -> None:
    """Every Eightfold page carries the search filter "-position.ats_data.status:CLOSED",
    so the page text cannot tell an open role from a closed one."""
    live = Resp(200, {"status": 200, "data": {"postedTs": 1789430400}})  # 2026-09-15
    with board({EIGHTFOLD_API: live}):
        reason, found = recheck.why_not_send({"url": PAYPAL}, TODAY)
    assert reason is None and found.posted == "2026-09-15" and found.via == "eightfold"
    gone = Resp(404, {"status": 404, "error": {"message": "Position not found"}, "data": {}})
    with board({EIGHTFOLD_API: gone}):
        reason, _ = recheck.why_not_send({"url": PAYPAL}, TODAY)
    assert reason == "no longer listed on the company's board"
    assert not recheck.CLOSED_RE.search("AND -position.ats_data.status:CLOSED AND")
    assert recheck.CLOSED_RE.search("This position is now closed")


AMEX = "https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/26012982"
ORACLE_API = "https://egug.fa.us2.oraclecloud.com/hcmRestApi/"


def test_oracle_uses_its_own_posted_date() -> None:
    item = {
        "ExternalPostedStartDate": "2026-09-01T23:14:03+00:00",
        "ExternalPostedEndDate": "2099-11-01T03:59:00+00:00",
    }
    with board({ORACLE_API: Resp(200, {"items": [item]})}):
        reason, found = recheck.why_not_send({"url": AMEX, "posted_at": "2026-09-30"}, TODAY)
    # its own date is kept, and a role still listed is never held back for its age
    assert found.via == "oracle" and found.posted == "2026-09-01" and reason is None
    with board({ORACLE_API: Resp(200, {"items": []})}):
        reason, _ = recheck.why_not_send({"url": AMEX}, TODAY)
    assert reason == "no longer listed on the company's board"


def test_program_ended() -> None:
    assert recheck.program_ended("Summer 2027 cohort", TODAY) is None
    assert recheck.program_ended("Runs June 2026 through August 2026", TODAY) == "8/2026"
    assert recheck.program_ended("Fall 2026 and Spring 2027", TODAY) is None
