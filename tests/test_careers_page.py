"""Conservative parsing for the read-only browser fallback."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import patch

from sourcer import net
from sourcer.probe import linked_boards
from sourcer.sources import careers_page, greenhouse


def test_parse_rendered_page_prefers_jobposting_jsonld() -> None:
    payload = {
        "@type": "JobPosting",
        "title": "AI Intern",
        "url": "/careers/jobs/123",
        "description": "<p>Summer 2027, Python</p>",
        "datePosted": "2026-09-01T12:00:00Z",
        "employmentType": "INTERN",
        "jobLocation": "New York, NY",
    }
    html = f'<script type="application/ld+json">{json.dumps(payload)}</script>'
    rows = careers_page.parse_rendered_page(html, "https://acme.example/careers")
    assert rows == [
        {
            "title": "AI Intern",
            "location": "New York, NY",
            "url": "https://acme.example/careers/jobs/123",
            "description": "Summer 2027, Python",
            "employment_type": "intern",
            "posted_at": "2026-09-01",
        }
    ]


def test_parse_rendered_page_falls_back_to_dated_job_links() -> None:
    html = """
    <ul><li><a href="/jobs/123-ai-intern">AI Intern</a>
    New York, NY, posted 2026-09-01</li></ul>
    """
    rows = careers_page.parse_rendered_page(html, "https://acme.example/careers")
    assert rows[0]["title"] == "AI Intern"
    assert rows[0]["posted_at"] == "2026-09-01"


def test_fetch_postings_only_renders_until_first_page_with_jobs() -> None:
    with patch.object(
        careers_page,
        "_render",
        side_effect=["<h1>No jobs</h1>", '<a href="/jobs/1">AI Intern</a> 2026-09-01'],
    ) as render:
        rows = careers_page.fetch_postings({"company": "Acme", "domain": "acme.example"})
    assert len(rows) == 1
    assert render.call_count == 2


def test_jsonld_graph_formats_structured_locations() -> None:
    payload = {
        "@graph": [
            {
                "@type": "JobPosting",
                "title": "Data Intern",
                "jobLocation": {
                    "address": {
                        "addressLocality": "New York",
                        "addressRegion": "NY",
                        "addressCountry": "US",
                    }
                },
                "datePosted": "2026-09-01",
            }
        ]
    }
    html = f'<script type="application/ld+json">{json.dumps(payload)}</script>'
    row = careers_page.parse_rendered_page(html, "https://acme.example/jobs")[0]
    assert row["location"] == "New York, NY, US"


def test_invalid_jsonld_and_non_job_links_are_ignored() -> None:
    html = '<script type="application/ld+json">{bad</script><a href="/about">About</a>'
    assert careers_page.parse_rendered_page(html, "https://acme.example") == []


def test_fetch_postings_requires_url_or_domain() -> None:
    assert careers_page.fetch_postings({"company": "Acme"}) == []


class _FakeRoute:
    def __init__(self, url: str, method: str = "GET", kind: str = "document") -> None:
        self.request = type(
            "R",
            (),
            {
                "url": url,
                "method": method,
                "resource_type": kind,
                "headers": {"user-agent": "x", "cookie": "secret"},
            },
        )()
        self.result = ""
        self.fulfilled: dict[str, Any] = {}

    async def abort(self) -> None:
        self.result = "abort"

    async def fulfill(self, **kwargs: Any) -> None:
        self.result = "fulfill"
        self.fulfilled = kwargs


launch_env: dict[str, str] = {}


def _fake_playwright(monkeypatch: Any, launches: list[int], open_pages: list[int]) -> None:
    """A stand in for playwright.async_api that counts launches and open pages."""
    live = {"now": 0}
    sockets: list[str] = []
    launch_env.clear()

    class Page:
        async def goto(self, url: str, **kw: Any) -> None:
            live["now"] += 1
            open_pages.append(live["now"])
            await asyncio.sleep(0.01)

        async def wait_for_load_state(self, *a: Any, **kw: Any) -> None:
            raise TimeoutError("never quiet")

        async def content(self) -> str:
            return '<a href="/jobs/1">Data Intern</a>'

        async def close(self) -> None:
            live["now"] -= 1

    class Context:
        async def route(self, pattern: str, handler: Any) -> None:
            pass

        async def route_web_socket(self, pattern: str, handler: Any) -> None:
            sockets.append(pattern)

        async def new_page(self) -> Page:
            return Page()

        async def close(self) -> None:
            pass

    class Chromium:
        async def launch(self, **kw: Any) -> Chromium:
            launches.append(1)
            launch_env.update(kw.get("env") or {"unset": "yes"})
            await asyncio.sleep(0.2)  # slow enough that other threads ask before it is up
            return self

        async def new_context(self, **kw: Any) -> Context:
            return Context()

        async def close(self) -> None:
            pass

    class PW:
        chromium = Chromium()

        async def start(self) -> PW:
            return self

        async def stop(self) -> None:
            pass

    mod = types.ModuleType("playwright.async_api")
    mod.async_playwright = PW  # type: ignore[attr-defined]
    pkg = types.ModuleType("playwright")
    pkg.async_api = mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", mod)


def test_one_browser_serves_every_thread_with_a_few_pages_at_a_time(monkeypatch: Any) -> None:
    launches: list[int] = []
    open_pages: list[int] = []
    _fake_playwright(monkeypatch, launches, open_pages)
    monkeypatch.setenv("SOURCER_BROWSER", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "secret-token")
    monkeypatch.setattr(careers_page, "_BROWSER", None)
    monkeypatch.setattr(careers_page, "validate_public_https_url", lambda url: url)
    with ThreadPoolExecutor(max_workers=8) as pool:
        pages = list(
            pool.map(careers_page._render, [f"https://acme.example/{i}" for i in range(12)])
        )
    assert all("Data Intern" in p for p in pages)
    assert launches == [1], "one browser for the whole run"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in launch_env and "PATH" in launch_env
    assert max(open_pages) <= careers_page.PAGES_AT_ONCE
    careers_page._BROWSER.close()  # type: ignore[union-attr]


def test_the_browser_lets_through_only_public_page_gets(monkeypatch) -> None:
    sent: list[dict[str, Any]] = []

    def fake_get(url: str, **kwargs: Any) -> Any:
        sent.append(kwargs)
        if "rebinds" in url:
            raise net.Refused("resolves to a non-public address")
        headers = {"content-type": "text/html", "content-encoding": "gzip"}
        return type("Reply", (), {"status_code": 200, "headers": headers, "content": b"<p>"})()

    monkeypatch.setattr(net, "get", fake_get)
    b = careers_page._Browser()
    b._public = {"jobs.acme.example": True, "rebinds.example": True, "10.0.0.1": False}
    cases = {
        ("https://jobs.acme.example/list", "GET", "document"): "fulfill",
        ("https://jobs.acme.example/app.js", "GET", "script"): "fulfill",
        ("https://jobs.acme.example/logo.png", "GET", "image"): "abort",
        ("https://jobs.acme.example/apply", "POST", "xhr"): "abort",
        ("https://10.0.0.1/admin", "GET", "document"): "abort",
        ("http://jobs.acme.example/list", "GET", "document"): "abort",
        ("https://rebinds.example/list", "GET", "document"): "abort",
    }
    for (url, method, kind), want in cases.items():
        route = _FakeRoute(url, method, kind)
        asyncio.run(b._guard(route))
        assert route.result == want, url
        if want == "fulfill":
            assert route.fulfilled["headers"] == {"content-type": "text/html"}
    assert all(k["headers"] == {"user-agent": "x"} for k in sent), "no cookies forwarded"
    assert all(k["allow_redirects"] is False for k in sent)


def test_a_page_that_names_a_job_board_is_read_through_that_board() -> None:
    assert careers_page.board_on_page(
        '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme"></iframe>'
    ) == ("greenhouse", "acme")
    assert careers_page.board_on_page(
        '<a href="https://osv-rubicon.wd5.myworkdayjobs.com/en-US/MagniteCareers">Jobs</a>'
    ) == ("workday", "osv-rubicon/wd5/MagniteCareers")
    assert careers_page.board_on_page('<a href="https://jobs.ashbyhq.com/acme.ai">x</a>') == (
        "ashby",
        "acme.ai",
    )
    assert careers_page.board_on_page("<p>no board</p>") is None


def test_menu_links_are_not_jobs_but_a_job_card_heading_names_its_link() -> None:
    html = (
        '<a href="/careers/all">See open roles</a>'
        "<div><h3>Data Analyst</h3><p>New York</p>"
        '<a href="/careers/data-analyst">Learn more</a></div>'
        '<a href="/careers-positions-growth-lead">Growth Lead</a>'
    )
    rows = careers_page.parse_rendered_page(html, "https://acme.example/careers")
    assert [r["title"] for r in rows] == ["Data Analyst", "Growth Lead"]


def test_only_categories_on_the_page_means_one_step_to_the_real_list() -> None:
    pages = {
        "https://acme.example/careers": (
            '<a href="/jobs/warsaw">Warsaw</a><a href="/jobs/all">All open positions</a>'
        ),
        "https://acme.example/jobs/all": '<a href="/jobs/123">Senior Data Analyst</a>',
    }
    with patch.object(careers_page, "_render", side_effect=lambda url: pages[url]):
        rows = careers_page.fetch_postings({"company": "Acme", "domain": "acme.example"})
    assert [r["title"] for r in rows] == ["Senior Data Analyst"]


def test_a_hosted_board_is_followed_and_its_links_are_jobs() -> None:
    pages = {
        "https://acme.example/careers": '<a href="https://jobs.gem.com/acme">Careers</a>',
        "https://jobs.gem.com/acme": (
            '<a href="https://jobs.gem.com/acme/abc123">Founding Engineer</a>'
        ),
    }
    with patch.object(careers_page, "_render", side_effect=lambda url: pages[url]):
        rows = careers_page.fetch_postings({"company": "Acme", "domain": "acme.example"})
    assert [(r["title"], r["url"]) for r in rows] == [
        ("Founding Engineer", "https://jobs.gem.com/acme/abc123")
    ]


def test_the_path_the_probe_found_is_tried_first() -> None:
    with patch.object(careers_page, "_render", return_value="<p>none</p>") as render:
        careers_page.fetch_postings({"company": "Acme", "domain": "acme.example", "slug": "jobs"})
    assert render.call_args_list[0].args[0] == "https://acme.example/jobs"


def test_a_board_that_does_not_answer_falls_back_to_the_page() -> None:
    html = '<a href="https://jobs.lever.co/acme">Jobs</a><a href="/jobs/1">Data Engineer</a>'
    with (
        patch.object(careers_page, "_render", return_value=html),
        patch.object(careers_page, "_from_board", side_effect=RuntimeError("down")),
    ):
        rows = careers_page.fetch_postings({"company": "Acme", "domain": "acme.example"})
    assert [r["title"] for r in rows] == ["Data Engineer"]


def test_a_named_board_goes_to_that_boards_reader() -> None:
    with patch.object(greenhouse, "fetch_postings", return_value=[{"title": "Analyst"}]) as gh:
        assert careers_page._from_board(("greenhouse", "acme")) == [{"title": "Analyst"}]
    gh.assert_called_once_with({"slug": "acme"})


def test_nav_titles_are_recognised_after_folding_spaces_and_punctuation() -> None:
    for text in ("See open roles →", "View position", "Jobs & apply", "  Browse all   jobs  "):
        assert careers_page.nav_title(text), text
    for text in ("Data Analyst Intern", "Open source engineer"):
        assert not careers_page.nav_title(text), text


def test_hostile_link_text_and_long_pages_are_read_in_linear_time() -> None:
    import time  # noqa: PLC0415

    start = time.perf_counter()
    assert not careers_page.nav_title("!" + " " * 20_000 + "x")
    assert careers_page.board_on_page("a" * 300_000 + ".wd5") is None
    assert linked_boards("a" * 300_000 + ".fa") == []
    assert time.perf_counter() - start < 2
