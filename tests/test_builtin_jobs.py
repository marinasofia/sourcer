"""Built In NYC job leads: reading listing cards, and confirming each role on
the employer's own board. Synthetic pages; no network."""

from __future__ import annotations

import json
from typing import Any

import requests

from sourcer.discovery import builtin_jobs as bj


def card(job_id: str, company: str, slug: str, title: str) -> str:
    return (
        f'<div id="job-card-{job_id}" data-id="job-card"><a href="/company/{slug}" target="_blank">'
        f'<img alt="{company} Logo"></a><a href="/company/{slug}" target="_blank" '
        f'data-id="company-title" class="x"><span>{company}</span></a>'
        f'<h2><a href="/job/{slug}-role/{job_id}" target="_blank" data-id="job-card-title" '
        f'class="y">{title}</a></h2></div>'
    )


LISTING = (
    "<html>"
    + card("111", "Anthropic", "anthropic", "Product Engineer, Computer Use")
    + card("222", "AT&amp;T", "att", "Data Scientist")
    + "</html>"
)


def test_listing_urls_stay_inside_robots_rules() -> None:
    urls = bj.listing_urls()
    assert all(u.startswith("https://www.builtinnyc.com/jobs/") for u in urls)
    pages = [u.split("?page=")[1] for u in urls if "?page=" in u]
    assert set(pages) == {"2", "3"}  # page 1 is the bare URL; never past 3
    assert not any("search" in u or "?f" in u for u in urls)


def test_cards_give_role_employer_and_employer_page() -> None:
    leads = bj.parse_cards(LISTING)
    assert [(x.job_id, x.company, x.title) for x in leads] == [
        ("111", "Anthropic", "Product Engineer, Computer Use"),
        ("222", "AT&T", "Data Scientist"),
    ]
    assert leads[0].builtin_url == "https://www.builtinnyc.com/job/anthropic-role/111"
    assert leads[0].company_page == "https://www.builtinnyc.com/company/anthropic"
    assert bj.parse_cards("<html>nothing</html>") == []


def test_company_domain_comes_from_the_employer_page() -> None:
    page = (
        '<script type="application/ld&#x2B;json">'
        + json.dumps(
            {"@type": "Organization", "name": "monday.com", "url": "https://www.monday.com/"}
        )
        + "</script>"
    )
    assert bj.parse_company_domain(page) == "monday.com"
    assert bj.parse_company_domain("<html></html>") is None


def test_same_role_and_best_match() -> None:
    assert bj.same_role("Product Engineer, Computer Use", "Product Engineer - Computer Use")
    assert bj.same_role("Data Scientist (Remote)", "Data Scientist")
    assert not bj.same_role("Data Scientist", "Senior Data Scientist, Payments Risk")
    board = [
        {"title": "Software Engineer, Backend (Product)"},
        {"title": "Software Engineer, Backend (Platform)"},
    ]
    assert bj.best_match("Software Engineer, Backend (Platform)", board) == board[1]
    # two near titles and no exact one: too close to call, so no match
    assert bj.best_match("Software Engineer, Backend", board) is None


class Client:
    def __init__(self, pages: dict[str, str], limited: set[str] | None = None) -> None:
        self.pages, self.limited, self.asked = pages, limited or set(), []

    def get(self, url: str) -> Any:
        self.asked.append(url)
        if url in self.limited:
            resp = requests.Response()
            resp.status_code = 429
            raise requests.HTTPError("429", response=resp)
        return type("R", (), {"text": self.pages.get(url, "<html></html>")})()


def test_collect_reads_cards_and_skips_known_jobs() -> None:
    client = Client({bj.listing_urls()[0]: LISTING})
    leads = bj.collect(client, known={"222"})
    assert [lead.job_id for lead in leads] == ["111"]
    assert not any("/job/" in url for url in client.asked)  # job pages are never opened


def test_a_rate_limit_ends_todays_reading() -> None:
    urls = bj.listing_urls()
    client = Client({urls[0]: LISTING}, limited={urls[1]})
    leads = bj.collect(client, known=set())
    assert len(leads) == 2 and client.asked == urls[:2]


def lead(title: str, company: str, job_id: str = "1") -> bj.JobLead:
    return bj.JobLead(
        job_id,
        title,
        company,
        f"https://www.builtinnyc.com/job/x/{job_id}",
        company_page=f"https://www.builtinnyc.com/company/{company.lower()}",
    )


def test_verify_confirms_on_the_watched_board_and_probes_new_employers() -> None:
    leads = [
        lead("Product Engineer, Computer Use", "Anthropic", "1"),
        lead("Founding Engineer", "Anthropic", "2"),
        lead("ML Engineer", "Newco", "3"),
        lead("Analyst", "Bigbank", "4"),
        lead("Engineer", "Ghost Inc", "5"),
    ]
    companies = [
        {"name": "Anthropic", "ats_type": "greenhouse", "ats_slug": "anthropic"},
        {"name": "Bigbank", "ats_type": "workday", "ats_slug": "big/wd5/Careers"},
    ]
    boards = {
        ("greenhouse", "anthropic"): [
            {"title": "Product Engineer, Computer Use", "url": "https://gh/1"}
        ],
        ("ashby", "newco"): [{"title": "ML Engineer", "url": "https://ashby/3"}],
    }
    fetched: list[tuple[str, str]] = []

    def fetch_board(c: dict[str, Any]) -> list[dict[str, Any]]:
        fetched.append((c["ats"], c["slug"]))
        return boards[(c["ats"], c["slug"])]

    def find_board(name: str, domain: str | None) -> tuple[str | None, str | None]:
        return ("ashby", "newco") if name == "Newco" else (None, None)

    bj.verify(
        leads,
        companies=companies,
        fetch_board=fetch_board,
        find_board=find_board,
        company_domain=lambda url: "newco.com",
    )
    assert [x.status for x in leads] == [
        bj.VERIFIED,
        bj.NOT_LISTED,
        bj.VERIFIED,
        bj.BOARD_ONLY,
        bj.NO_BOARD,
    ]
    assert leads[0].official_url == "https://gh/1" and leads[2].slug == "newco"
    assert fetched.count(("greenhouse", "anthropic")) == 1  # one board read per run


def test_probe_budget_leaves_the_rest_for_tomorrow() -> None:
    leads = [lead("Engineer", f"Co{i}", str(i)) for i in range(3)]
    bj.verify(
        leads,
        companies=[],
        fetch_board=lambda c: [],
        find_board=lambda n, d: (None, None),
        company_domain=lambda u: None,
        max_probes=1,
    )
    assert [x.status for x in leads] == [bj.NO_BOARD, bj.UNCHECKED, bj.UNCHECKED]


def test_a_fast_client_is_refused() -> None:
    fast = Client({})
    fast.interval = 1.0  # type: ignore[attr-defined]
    try:
        bj.collect(fast, known=set())
    except ValueError as exc:
        assert "3.0 seconds" in str(exc)
    else:
        raise AssertionError("a one second client must be refused")
