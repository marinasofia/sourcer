"""Workday answers 403 for a job that was taken down (404 only for one that
never existed). A 403 counts as closed when the board's own search still
answers, so a real block is never read as a closed role."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sourcer import score_listings
from sourcer.apply import recheck, verify

URL = (
    "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/AI-Engineer_JR2013803"
)


def status(code: int) -> MagicMock:
    r = MagicMock()
    r.status_code, r.ok, r.text = code, 200 <= code < 300, ""
    return r


@pytest.mark.parametrize(("board_answers", "verdict"), [(True, "closed"), (False, None)])
def test_verify(board_answers: bool, verdict: str | None) -> None:
    with (
        patch.object(verify.net, "get", return_value=status(403)),
        patch.object(verify.net, "post", return_value=status(200 if board_answers else 403)),
    ):
        assert verify.check_workday(URL) == verdict


@pytest.mark.parametrize(("board_answers", "listed"), [(True, False), (False, None)])
def test_recheck(board_answers: bool, listed: bool | None) -> None:
    with (
        patch.object(recheck, "_get", return_value=status(403)),
        patch.object(recheck, "workday_board_answers", return_value=board_answers),
    ):
        reason, found = recheck.why_not_send({"url": URL}, date(2026, 10, 5))
    assert found.listed is listed and found.via == "workday"
    assert (reason == "no longer listed on the company's board") is (listed is False)


def test_listing_scoring_names_a_taken_down_role() -> None:
    def post(*_: Any, **__: Any) -> MagicMock:
        return status(200)

    with (
        patch.object(score_listings.net, "get", return_value=status(403)),
        patch.object(verify.net, "post", side_effect=post),
    ):
        assert score_listings.fetch_description(URL) == (
            None,
            "taken down from the workday board (closed)",
        )
