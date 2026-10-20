"""Source mappers and the SimplifyJobs gates. No network."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
import requests

from sourcer import config, sources
from tests.conftest import FROZEN_NOW


class FakeResp:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def json(self) -> Any:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise requests.HTTPError(f"status {self.status}")


NOW = FROZEN_NOW


def test_greenhouse_mapper_uses_first_published_then_updated_at() -> None:
    payload = {
        "jobs": [
            {
                "title": "Data Analyst",
                "location": {"name": "New York, NY"},
                "absolute_url": "https://boards.greenhouse.io/x/1",
                "content": "<p>SQL &amp; Python</p>",
                "first_published": "2026-08-01T00:00:00Z",
                "updated_at": "2026-08-15T00:00:00Z",
            },
            {"title": "No dates", "updated_at": "2026-08-15T00:00:00Z"},
            {"title": "Bare"},
        ]
    }
    with patch("sourcer.net.get", return_value=FakeResp(payload)):
        jobs = sources.fetch_greenhouse("x")
    assert jobs[0]["posted_at"] == "2026-08-01"
    assert jobs[0]["description"] == "SQL & Python"
    assert jobs[1]["posted_at"] == "2026-08-15"
    assert jobs[1]["location"] == ""
    assert jobs[2]["posted_at"] is None
    assert jobs[2]["url"] == ""


def test_lever_mapper_maps_fields_and_created_at() -> None:
    payload = [
        {
            "text": "ML Engineer",
            "categories": {"location": "Remote US"},
            "hostedUrl": "https://jobs.lever.co/x/1",
            "descriptionPlain": "Build models",
            "createdAt": 1787000000000,
        },
        {"text": "No categories", "description": "<b>html</b>"},
    ]
    with patch("sourcer.net.get", return_value=FakeResp(payload)):
        jobs = sources.fetch_lever("x")
    assert jobs[0]["location"] == "Remote US"
    assert jobs[0]["posted_at"] == "2026-08-17"
    assert jobs[1]["location"] == ""
    assert jobs[1]["description"] == "html"
    assert jobs[1]["posted_at"] is None


def test_ashby_mapper_skips_unlisted_and_handles_no_remote() -> None:
    payload = {
        "jobs": [
            {"title": "Hidden", "isListed": "false"},
            {
                "title": "Analyst",
                "location": "Austin",
                "secondaryLocations": None,
                "isRemote": False,
                "jobUrl": "https://jobs.ashbyhq.com/x/2",
                "descriptionHtml": "<p>Excel</p>",
                "publishedAt": None,
            },
        ]
    }
    with patch("sourcer.net.get", return_value=FakeResp(payload)):
        jobs = sources.fetch_ashby("x")
    assert len(jobs) == 1
    assert jobs[0]["location"] == "Austin"
    assert jobs[0]["description"] == "Excel"
    assert jobs[0]["posted_at"] is None


def test_http_error_propagates() -> None:
    with (
        patch("sourcer.net.get", return_value=FakeResp({}, status=500)),
        pytest.raises(requests.HTTPError),
    ):
        sources.fetch_greenhouse("x")


def test_iso_from_ms() -> None:
    assert sources.iso_from_ms(None) is None
    assert sources.iso_from_ms(0) is None
    assert sources.iso_from_ms(1787000000000) == "2026-08-17"


def test_clean_html_truncates_and_drops_blank_lines() -> None:
    html = "<p>a</p>\n\n<p>  </p><p>" + "x" * (config.MAX_DESCRIPTION_CHARS + 50) + "</p>"
    out = sources.clean_html(html)
    assert out.startswith("a\nx")
    assert len(out) == config.MAX_DESCRIPTION_CHARS
    assert sources.clean_html(None) == ""


def test_term_is_current() -> None:
    assert sources.term_is_current("Summer 2027", NOW)
    assert not sources.term_is_current("Summer 2026", NOW)
    assert sources.term_is_current("Fall 2026", NOW)
    assert sources.term_is_current("N/A", NOW)
    assert sources.term_is_current("new grad", NOW)


def entry(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "active": True,
        "is_visible": True,
        "category": "AI/ML/Data",
        "company_name": "Acme",
        "title": "ML Intern",
        "locations": ["New York, NY"],
        "terms": ["Summer 2027"],
        "url": "https://a.co/1",
        "date_posted": 1787000000,
    }
    base.update(over)
    return base


def test_simplify_entry_happy_path_intern() -> None:
    out = sources._simplify_entry(entry(), "intern", None, NOW)
    assert out is not None
    assert out["start_date"] == "Summer 2027"
    assert out["posted_at"] == "2026-08-17"
    assert out["source"] == "simplify"
    assert out["description"] == ""


def test_simplify_entry_new_grad_template_joins_terms() -> None:
    out = sources._simplify_entry(entry(), "full time", "new grad", NOW)
    assert out is not None
    assert out["start_date"] == "new grad Summer 2027"


def test_simplify_entry_template_without_terms() -> None:
    out = sources._simplify_entry(entry(terms=[]), "full time", "new grad", NOW)
    assert out is not None
    assert out["start_date"] == "new grad"


def test_simplify_entry_without_a_date_counts_as_fresh() -> None:
    # config.is_fresh: an undated posting is fresh; only a dated one can be too old
    out = sources._simplify_entry(entry(terms=None, date_posted=None), "intern", None, NOW)
    assert out is not None and out["posted_at"] is None


def test_simplify_entry_freshness_follows_the_one_window() -> None:
    day = 24 * 3600
    inside = entry(date_posted=int(NOW.timestamp()) - (config.FRESHNESS_DAYS - 1) * day)
    outside = entry(date_posted=int(NOW.timestamp()) - (config.FRESHNESS_DAYS + 2) * day)
    assert sources._simplify_entry(inside, "intern", None, NOW) is not None
    assert sources._simplify_entry(outside, "intern", None, NOW) is None


def test_simplify_entry_inactive_or_invisible_rejected() -> None:
    assert sources._simplify_entry(entry(active=False), "intern", None, NOW) is None
    assert sources._simplify_entry(entry(is_visible=False), "intern", None, NOW) is None


def test_simplify_entry_category_gate() -> None:
    assert sources._simplify_entry(entry(category="Hardware"), "intern", None, NOW) is None
    assert sources._simplify_entry(entry(category="Software"), "intern", None, NOW) is None
    admitted = sources._simplify_entry(
        entry(category="Software", title="Forward Deployed Engineer Intern"), "intern", None, NOW
    )
    assert admitted is not None


def test_simplify_entry_admits_product_roles_by_title() -> None:
    apm = entry(category="Product", title="Associate Product Manager New Grad")
    assert sources._simplify_entry(apm, "full time", "new grad", NOW) is not None
    trader = entry(category="Quant", title="Quantitative Trader Intern")
    assert sources._simplify_entry(trader, "intern", None, NOW) is None


def test_simplify_entry_location_gate() -> None:
    # a summer 2027 internship anywhere in the US, never abroad
    assert sources._simplify_entry(entry(locations=["Austin, TX"]), "intern", None, NOW)
    assert sources._simplify_entry(entry(locations=["London, UK"]), "intern", None, NOW) is None
    # a new grad role with no 2027 start keeps the NYC and remote rule
    no_term = entry(locations=["Austin, TX"], terms=[], title="ML Engineer New Grad")
    assert sources._simplify_entry(no_term, "full time", "new grad", NOW) is None
    assert sources._simplify_entry(entry(locations=None), "intern", None, NOW) is None


def test_simplify_entry_degree_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    assert sources._simplify_entry(entry(degrees=["PhD"]), "intern", None, NOW) is None
    assert sources._simplify_entry(entry(degrees=["Bachelor's", "PhD"]), "intern", None, NOW)
    monkeypatch.setenv("SOURCER_DEGREE", "PhD")
    assert sources._simplify_entry(entry(degrees=["PhD"]), "intern", None, NOW) is not None


def test_simplify_entry_stale_terms_gate() -> None:
    assert sources._simplify_entry(entry(terms=["Summer 2025"]), "intern", None, NOW) is None
    assert sources._simplify_entry(entry(terms=["Summer 2025", "Summer 2027"]), "intern", None, NOW)


def test_fetch_simplify_uses_thirty_second_timeout() -> None:
    with patch("sourcer.net.get", return_value=FakeResp([entry()])) as g:
        out = sources.fetch_simplify()
    assert len(out) == 2
    assert all(c.kwargs["timeout"] == sources.SIMPLIFY_TIMEOUT for c in g.call_args_list)


def test_in_scope_requires_title_and_location() -> None:
    assert sources.in_scope({"title": "Data Scientist", "location": "New York"})
    assert not sources.in_scope({"title": "Data Scientist", "location": "Paris, France"})
    assert not sources.in_scope({"title": "Accountant", "location": "New York"})


@pytest.mark.parametrize(
    ("location", "kept_count"), [("New York, NY", 1), ("Remote US", 1), ("Paris, France", 0)]
)
def test_collects_regular_full_time_roles_without_new_grad_label(
    location: str, kept_count: int
) -> None:
    posting = {
        "title": "Data Scientist",
        "location": location,
        "description": "Full-time role using Python. 0-2 years of experience. Flexible start date.",
        "url": "https://jobs.example.com/1",
        "posted_at": FROZEN_NOW.isoformat(),
    }
    with patch("sourcer.sources.fetch_postings", return_value=[posting]):
        kept, unsure, dropped = sources.fetch_company({"company": "Example", "ats": "ashby"})
    assert len(unsure) == kept_count  # on to the title check
    assert not kept
    assert dropped == 1 - kept_count


def test_fetch_company_splits_kept_unsure_and_drops_senior() -> None:
    payload = {
        "jobs": [
            {
                "title": "Data Scientist",
                "location": {"name": "New York"},
                "absolute_url": "u1",
                "content": "entry level",
                "first_published": "2026-08-20",
            },
            {
                "title": "Deployment Wizard",
                "location": {"name": "Remote US"},
                "absolute_url": "u2",
                "content": "internship",
                "first_published": "2026-08-20",
            },
            {
                "title": "Senior Wizard",
                "location": {"name": "Remote US"},
                "absolute_url": "u3",
                "content": "internship",
                "first_published": "2026-08-20",
            },
            {
                "title": "Data Scientist",
                "location": {"name": "Paris, France"},
                "absolute_url": "u4",
                "content": "internship",
                "first_published": "2026-08-20",
            },
        ]
    }
    ent = {"company": "TestCo", "ats": "greenhouse", "slug": "testco"}
    with patch("sourcer.net.get", return_value=FakeResp(payload)):
        kept, unsure, dropped = sources.fetch_company(ent)
    assert not kept
    assert [p["title"] for p in unsure] == ["Data Scientist", "Deployment Wizard"]
    assert dropped == 2
    assert unsure[0]["company"] == "TestCo"


def test_fetch_company_unknown_ats_raises() -> None:
    with pytest.raises(KeyError):
        sources.fetch_company({"company": "X", "ats": "unknown", "slug": "x"})


def test_workable_mapper_reads_table_and_target_detail() -> None:
    table = (
        "| Title | Department | Location | Type | Salary | Posted | Details |\n"
        "|---|---|---|---|---|---|---|\n"
        "| AI Intern | Engineering | New York, NY | Internship | n/a | 2026-09-01 | "
        "[View](https://apply.workable.com/acme/jobs/view/ABC123.md) |\n"
    )
    detail = """# AI Intern
Summer 2027 internship using Python.
[Apply at Acme](https://apply.workable.com/acme/j/ABC123/apply)
"""
    with patch("sourcer.sources.workable._get_text", side_effect=[table, detail]):
        jobs = sources.workable.fetch_workable("acme")
    assert len(jobs) == 1
    assert jobs[0]["employment_type"] == "internship"
    assert jobs[0]["posted_at"] == "2026-09-01"
    assert jobs[0]["url"] == "https://apply.workable.com/acme/j/ABC123/apply"
    assert "Python" in jobs[0]["description"]


def test_smartrecruiters_mapper_fetches_details_only_for_in_scope_rows() -> None:
    listing = {
        "totalFound": 2,
        "content": [
            {
                "id": "1",
                "name": "AI Intern",
                "ref": "https://api.smartrecruiters.com/v1/companies/acme/postings/1",
                "releasedDate": "2026-09-01",
                "location": {"fullLocation": "New York, NY"},
                "typeOfEmployment": {"label": "Internship"},
            },
            {
                "id": "2",
                "name": "Data Intern",
                "ref": "https://api.smartrecruiters.com/v1/companies/acme/postings/2",
                "releasedDate": "2026-09-01",
                "location": {"fullLocation": "Paris, France"},
            },
        ],
    }
    detail = {
        "postingUrl": "https://jobs.smartrecruiters.com/acme/1-ai-intern",
        "jobAd": {"sections": {"jobDescription": {"text": "<p>Build models</p>"}}},
    }
    with patch("sourcer.sources.smartrecruiters._get_json", side_effect=[listing, detail]):
        jobs = sources.smartrecruiters.fetch_smartrecruiters("acme")
    assert len(jobs) == 2
    assert jobs[0]["description"] == "Build models"
    assert jobs[0]["url"].endswith("1-ai-intern")
    assert jobs[1]["description"] == ""


def test_rippling_mapper_reads_cards() -> None:
    board = """
    <div class="card"><div><div>
      <a href="/acme/jobs/12345678-abcd">AI Intern</a>
      <div><p>Engineering</p><p>New York, NY</p></div>
    </div></div><a href="/acme/jobs/12345678-abcd">View job</a></div>
    """
    with patch(
        "sourcer.sources.rippling._get_text",
        side_effect=[board, "<main>Summer 2027 internship with Python</main>"],
    ):
        jobs = sources.rippling.fetch_rippling("acme")
    assert jobs == [
        {
            "title": "AI Intern",
            "location": "New York, NY",
            "url": "https://ats.rippling.com/acme/jobs/12345678-abcd",
            "description": "Summer 2027 internship with Python",
            "posted_at": None,
        }
    ]


def test_fetch_simplify_keeps_going_when_one_list_is_down() -> None:
    """One dead SimplifyJobs list must not hide the other one's listings."""
    good = [
        {
            "active": True,
            "is_visible": True,
            "category": "AI/ML/Data",
            "company_name": "Acme",
            "title": "ML Intern",
            "locations": ["New York, NY"],
            "terms": ["Summer 2099"],
            "url": "https://a.co/1",
            "date_posted": int(NOW.timestamp()),
        }
    ]
    calls = iter([requests.ConnectionError("down"), FakeResp(good)])

    def fake_get(url: str, timeout: int) -> FakeResp:
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return item

    with patch("sourcer.net.get", side_effect=fake_get):
        out = sources.fetch_simplify()
    assert [o["company"] for o in out] == ["Acme"]
    assert out[0]["employment_type"] == "full time"  # the second list is the new-grad one


def test_fetch_simplify_raises_only_when_every_list_is_down() -> None:
    with (
        patch("sourcer.net.get", side_effect=requests.ConnectionError("down")),
        pytest.raises(requests.RequestException, match="down"),
    ):
        sources.fetch_simplify()
