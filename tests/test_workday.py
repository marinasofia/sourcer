"""Workday adapter: paging, freshness stop, selective detail fetches."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from sourcer import sources
from sourcer.discovery import builtin
from sourcer.sources import workday


class Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self.payload


def job(title: str, where: str, posted: str, path: str) -> dict[str, str]:
    return {"title": title, "locationsText": where, "postedOn": posted, "externalPath": path}


PAGE_1 = [
    job("Data Analyst Intern", "New York, NY", "Posted Today", "/job/NY/Data-Analyst-Intern_R1"),
    job("Associate Product Manager", "3 Locations", "Posted 2 Days Ago", "/job/x/APM_R2"),
    job("Senior Data Scientist", "New York, NY", "Posted Today", "/job/NY/Senior_R3"),
    job("Branch Teller", "New York, NY", "Posted Yesterday", "/job/NY/Teller_R4"),
    job("Data Analyst", "Charlotte, NC", "Posted Today", "/job/NC/Data_R5"),
] + [job(f"Old role {i}", "New York, NY", "Posted 30+ Days Ago", f"/job/o{i}") for i in range(15)]
PAGE_2 = [job("Too old to matter", "New York, NY", "Posted 30+ Days Ago", "/job/old")] * 20

DETAILS = {
    "/job/NY/Data-Analyst-Intern_R1": {
        "location": "New York, NY",
        "startDate": "2026-08-25",
        "jobDescription": "<p>SQL and Python</p>",
        "externalUrl": "https://bank.example/R1",
    },
    "/job/x/APM_R2": {
        "location": "New York, NY",
        "additionalLocations": ["Remote, US"],
        "startDate": "2026-08-23",
        "jobDescription": "<p>Roadmaps</p>",
    },
}


@pytest.mark.parametrize(
    ("text", "days"),
    [
        ("Posted Today", 0),
        ("Posted Yesterday", 1),
        ("Posted 3 Days Ago", 3),
        ("Posted 30+ Days Ago", 31),
        ("", None),
    ],
)
def test_days_ago(text: str, days: int | None) -> None:
    assert workday.days_ago(text) == days


def test_opens_only_promising_postings_and_stops_at_the_page_limit() -> None:
    posts: list[int] = []
    gets: list[str] = []

    def fake_post(url: str, json: dict[str, Any], **kw: Any) -> Resp:
        posts.append(json["offset"])
        return Resp({"jobPostings": PAGE_1 if json["offset"] == 0 else PAGE_2})

    def fake_get(url: str, **kw: Any) -> Resp:
        path = url.split("/Careers", 1)[1]
        gets.append(path)
        return Resp({"jobPostingInfo": DETAILS[path]})

    with (
        patch("sourcer.net.post", side_effect=fake_post),
        patch("sourcer.net.get", side_effect=fake_get),
        patch("sourcer.sources.workday.time.sleep"),
    ):
        out = sources.fetch_postings(
            {"company": "Bank", "ats": "workday", "slug": "bank/wd5/Careers"}
        )
    # "30+ days ago" roles are still listed, so paging runs to its cap
    assert posts == list(range(0, workday.PAGE * workday.MAX_PAGES, workday.PAGE))
    assert gets == ["/job/NY/Data-Analyst-Intern_R1", "/job/x/APM_R2"]  # senior, teller, NC skipped
    by_title = {p["title"]: p for p in out}
    assert set(by_title) == {
        "Data Analyst Intern",
        "Associate Product Manager",
        "Senior Data Scientist",
        "Branch Teller",
        "Data Analyst",
        "Too old to matter",
        *(f"Old role {i}" for i in range(15)),
    }
    intern = by_title["Data Analyst Intern"]
    assert intern["description"] == "SQL and Python" and intern["posted_at"] == "2026-08-25"
    assert intern["url"] == "https://bank.example/R1"
    assert by_title["Associate Product Manager"]["location"] == "New York, NY; Remote, US"
    assert by_title["Branch Teller"]["posted_at"] == "2026-08-24"  # dated from "Posted Yesterday"


def test_undated_postings_stay_undated_and_short_pages_end_paging() -> None:
    assert workday._date_of(None) is None
    calls: list[int] = []

    def fake_post(url: str, json: dict[str, Any], **kw: Any) -> Resp:
        calls.append(json["offset"])
        return Resp({"jobPostings": [job("Barista", "Paris", "", "/job/p")]})

    with patch("sourcer.net.post", side_effect=fake_post):
        out = workday.fetch_workday("bank/wd1/Site")
    assert calls == [0]  # one short page means there is no next page
    assert out[0]["posted_at"] is None and out[0]["description"] == ""


def test_builtin_fetch_reads_every_category_once() -> None:
    class Client:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def get(self, url: str) -> Any:
            self.urls.append(url)
            return type("R", (), {"text": ""})()

    c = Client()
    assert builtin.fetch(c) == []  # type: ignore[arg-type]
    assert c.urls == list(builtin.BUILTIN_URLS) and len(set(c.urls)) == len(builtin.BUILTIN_TYPES)
    assert any(u.endswith("/music") for u in c.urls)
