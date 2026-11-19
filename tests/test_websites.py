"""Company website lookup: extraction, blocklist, storage rules, retries, budget."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
import requests

from sourcer import db, snowdb, sqlitedb
from sourcer.discovery import cli, simplify, websites
from sourcer.discovery.websites import (
    apply_url_domain,
    backfill_domains,
    builtin_company_link,
    builtin_website,
    find_website,
    simplify_website,
    website_domain,
)
from sourcer.probe import RateLimiter
from tests.test_snowdb import make_conn

NOW = datetime(2026, 11, 18, tzinfo=UTC)

BUILTIN_PAGE = """
<html><body>
<a href="https://www.builtinnyc.com/company/acme/jobs">Jobs</a>
<a href="https://www.linkedin.com/company/acme">LinkedIn</a>
<div><i class="fa-arrow-up-right-from-square"></i>
<a href="https://www.acme.example?utm_source=BuiltIn&amp;utm_medium=BuiltIn" rel="nofollow">
View Website</a></div>
</body></html>
"""
SIMPLIFY_PAGE = """
<html><head><script type="application/ld+json">{"@context":"https://schema.org",
"@type":"Organization","name":"Acme Rockets","url":"https://simplify.jobs/c/Acme-Rockets",
"sameAs":["https://www.linkedin.com/company/acme","https://acmerockets.com.au/"]}</script>
</head><body><a href="https://acmerockets.com.au/?utm_source=Simplify">Website</a></body></html>
"""
JOB_PAGE = """
<a href="/company/other-fund">Other Fund</a>
<a href="/company/acme">Acme</a>
"""


@dataclass
class FakeResponse:
    status_code: int
    text: str
    url: str = ""


class FakeRequester:
    def __init__(self, pages: dict[str, str], fail: set[str] | None = None) -> None:
        self.pages = pages
        self.fail = fail or set()
        self.seen: list[str] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.seen.append(url)
        if url in self.fail:
            raise requests.ConnectionError("reset")
        if url in self.pages:
            return FakeResponse(200, self.pages[url], url)
        return FakeResponse(404, "", url)


def no_wait() -> RateLimiter:
    return RateLimiter(interval=0, sleep=lambda s: None)


def row(name: str, source: str, urls: list[str]) -> dict[str, Any]:
    return {"name": name, "source": source, "quality_signals": json.dumps({"source_urls": urls})}


def store(conn: Any, name: str, source: str, urls: list[str], domain: str | None = None) -> None:
    sqlitedb.save_discovered_companies(
        conn,
        [
            {
                "name": name,
                "domain": domain,
                "source": source,
                "quality_signals": {"source_urls": urls},
            }
        ],
    )


def test_website_domain_rejects_boards_directories_and_social_hosts() -> None:
    assert website_domain("https://www.Acme.example/about") == "acme.example"
    for url in (
        "https://job-boards.greenhouse.io/acme/jobs/1",
        "https://jobs.lever.co/acme/1",
        "https://jobs.ashbyhq.com/acme",
        "https://acme.wd5.myworkdayjobs.com/en-US/careers",
        "https://careers-acme.icims.com/jobs",
        "https://www.builtinnyc.com/company/acme",
        "https://builtin.com/company/acme",
        "https://simplify.jobs/c/Acme",
        "https://www.linkedin.com/company/acme",
        "https://x.com/acme",
        "https://10.0.0.1/",
        "https://localhost/",
        "",
        None,
    ):
        assert website_domain(url) is None, url


def test_apply_url_domain_takes_own_careers_hosts_with_the_name() -> None:
    assert apply_url_domain("https://jobs.l3harris.example/job/1", "L3Harris Technologies") == (
        "l3harris.example"
    )
    assert apply_url_domain("https://lifeattiktok.com/search/1", "TikTok") == "lifeattiktok.com"
    assert apply_url_domain("https://careers.acme.com.au/1", "Acme") == "acme.com.au"
    # a host that does not carry the name could be anyone's job site
    assert apply_url_domain("https://careers.example.com/1", "Acme") is None
    assert apply_url_domain("https://job-boards.greenhouse.io/acme/jobs/1", "Acme") is None
    assert apply_url_domain("not a url", "Acme") is None


def test_builtin_page_gives_the_view_website_link() -> None:
    assert builtin_website(BUILTIN_PAGE) == "acme.example"
    assert builtin_website('<a href="https://www.linkedin.com/x">View Website</a>') is None


def test_simplify_page_gives_its_structured_website_for_the_named_company() -> None:
    assert simplify_website(SIMPLIFY_PAGE) == "acmerockets.com.au"
    assert simplify_website(SIMPLIFY_PAGE, "Acme Rockets") == "acmerockets.com.au"
    assert simplify_website(SIMPLIFY_PAGE, "Other Co") is None  # a namesake's page
    link_only = '<a href="https://acme.example/?utm_source=Simplify">site</a>'
    assert simplify_website(link_only) == "acme.example"
    broken = '<script type="application/ld+json">{not json</script>' + link_only
    assert simplify_website(broken) == "acme.example"


def test_builtin_job_page_links_only_the_companys_own_profile() -> None:
    page = "https://www.builtinnyc.com/job/analyst/1"
    assert builtin_company_link(JOB_PAGE, page, "Acme") == "https://www.builtinnyc.com/company/acme"
    assert builtin_company_link(JOB_PAGE, page, "Nobody") is None


def test_find_website_prefers_a_stored_apply_link_with_no_request() -> None:
    requester = FakeRequester({})
    found = find_website(
        row("Acme", "simplify", ["https://jobs.lever.co/acme/1", "https://careers.acme.example/1"]),
        requester,
        no_wait(),
    )
    assert (found.domain, found.requests, requester.seen) == ("acme.example", 0, [])


def test_find_website_reads_the_builtin_profile() -> None:
    url = "https://www.builtinnyc.com/company/acme"
    found = find_website(
        row("Acme", "builtin_nyc", [url]), FakeRequester({url: BUILTIN_PAGE}), no_wait()
    )
    assert (found.domain, found.found_at, found.requests) == ("acme.example", url, 1)


def test_find_website_reads_the_simplify_profile_by_name() -> None:
    url = "https://simplify.jobs/c/Acme-Rockets"
    requester = FakeRequester({url: SIMPLIFY_PAGE})
    found = find_website(
        row("Acme Rockets", "simplify", ["https://job-boards.greenhouse.io/acme/jobs/1"]),
        requester,
        no_wait(),
    )
    assert found.domain == "acmerockets.com.au"
    assert requester.seen == [url]


def test_find_website_follows_a_builtin_job_page_to_the_profile() -> None:
    job = "https://www.builtinnyc.com/job/analyst/1"
    profile = "https://www.builtinnyc.com/company/acme"
    requester = FakeRequester({job: JOB_PAGE, profile: BUILTIN_PAGE})
    found = find_website(row("Acme", "builtin_jobs", [job]), requester, no_wait())
    assert (found.domain, found.requests) == ("acme.example", 2)


def test_find_website_survives_errors_and_missing_pages() -> None:
    url = "https://www.builtinnyc.com/company/acme"
    found = find_website(
        row("Acme", "builtin_nyc,simplify", [url]), FakeRequester({}, fail={url}), no_wait()
    )
    assert found.domain is None and found.requests == 2
    nothing = find_website(
        {"name": "Acme", "quality_signals": "{bad"}, FakeRequester({}), no_wait()
    )
    assert nothing.requests == 0


def test_backfill_stores_found_domains_and_counts_every_attempt(tmp_db: Any) -> None:
    profile = "https://www.builtinnyc.com/company/acme"
    store(tmp_db, "Acme", "builtin_nyc", [profile])
    store(tmp_db, "Quiet", "builtin_nyc", ["https://www.builtinnyc.com/company/quiet"])
    results = backfill_domains(
        tmp_db, 10, now=NOW, requester=FakeRequester({profile: BUILTIN_PAGE}), limiter=no_wait()
    )
    assert {r.name: r.domain for r in results} == {"Acme": "acme.example", "Quiet": None}
    rows = {r["name"]: r for r in db.list_companies(tmp_db)}
    assert rows["Acme"]["domain"] == "acme.example"
    assert rows["Quiet"]["domain"] is None
    assert rows["Quiet"]["domain_attempts"] == 1 and rows["Quiet"]["domain_checked_at"]
    # nothing is due again the same day: no refetch
    again = backfill_domains(tmp_db, 10, now=NOW, requester=FakeRequester({}), limiter=no_wait())
    assert again == []


def test_backfill_never_overwrites_a_stored_domain(tmp_db: Any) -> None:
    store(tmp_db, "Acme", "yc", [], domain="acme.io")
    assert backfill_domains(tmp_db, 10, now=NOW, requester=FakeRequester({})) == []
    db.save_company_domain(tmp_db, "Acme", "other.example")
    assert db.list_companies(tmp_db)[0]["domain"] == "acme.io"
    # discovery fills an empty domain but does not replace one either
    store(tmp_db, "Acme", "simplify", [], domain="lifeatacme.example")
    assert db.list_companies(tmp_db)[0]["domain"] == "acme.io"


def test_retry_after_thirty_days_at_most_three_times() -> None:
    base = {"name": "Acme", "domain": None}
    assert websites.due({**base, "domain_attempts": 0}, NOW)
    recent = "2026-11-01T00:00:00+00:00"
    old = "2026-09-01T00:00:00"
    assert not websites.due({**base, "domain_attempts": 1, "domain_checked_at": recent}, NOW)
    assert websites.due({**base, "domain_attempts": 2, "domain_checked_at": old}, NOW)
    assert not websites.due({**base, "domain_attempts": 3, "domain_checked_at": old}, NOW)
    assert websites.due({**base, "domain_attempts": 1, "domain_checked_at": "garbage"}, NOW)
    assert not websites.due({**base, "domain": "acme.io"}, NOW)


def test_backfill_orders_new_companies_first_and_honours_the_limit(tmp_db: Any) -> None:
    for name in ("Retried", "Fresh"):
        store(tmp_db, name, "builtin_nyc", [])
    db.save_company_domain(tmp_db, "Retried", None)
    tmp_db.execute("UPDATE companies SET domain_checked_at='2026-01-01T00:00:00+00:00'")
    tmp_db.commit()
    results = backfill_domains(tmp_db, 1, now=NOW, requester=FakeRequester({}), limiter=no_wait())
    assert [r.name for r in results] == ["Fresh"]
    with pytest.raises(ValueError, match="negative"):
        backfill_domains(tmp_db, -1)


def test_backfill_starts_no_company_after_the_budget(tmp_db: Any) -> None:
    for name in ("One", "Two", "Three"):
        store(tmp_db, name, "builtin_nyc", [])
    ticks = iter([0.0, 0.0, 30.0, 61.0, 61.0])
    results = backfill_domains(
        tmp_db,
        10,
        minutes=1,
        workers=1,
        now=NOW,
        clock=lambda: next(ticks),
        requester=FakeRequester({}),
        limiter=no_wait(),
    )
    assert [r.name for r in results] == ["One", "Two"]
    untouched = next(r for r in db.list_companies(tmp_db) if r["name"] == "Three")
    assert untouched["domain_attempts"] == 0  # not started, so not counted


def test_simplify_discovery_keeps_the_apply_host_as_website() -> None:
    postings = [
        {"company": "Acme", "url": "https://careers.acme.example/1", "employment_type": "intern"},
        {"company": "Board", "url": "https://jobs.lever.co/board/1"},
        {"company": "", "url": "https://x.example"},
    ]
    with patch("sourcer.sources.fetch_simplify", return_value=postings):
        rows = simplify.fetch(None)  # type: ignore[arg-type]
    assert [(r.name, r.domain) for r in rows] == [("Acme", "acme.example"), ("Board", None)]


def test_cli_runs_the_backfill_before_the_probe(
    tmp_db: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []
    found = [websites.WebsiteResult("Acme", "acme.example", "u", 1)]

    def backfill_step(*args: Any, **kwargs: Any) -> list[websites.WebsiteResult]:
        calls.append("web")
        return found

    def probe_step(*args: Any, **kwargs: Any) -> list[Any]:
        calls.append("probe")
        return []

    empty = type(
        "R", (), {"candidates": (), "new_candidates": (), "errors": (), "source_counts": {}}
    )
    with (
        patch.object(cli.db, "connect", return_value=tmp_db),
        patch.object(cli, "discover", return_value=empty()),
        patch.object(cli, "backfill_domains", side_effect=backfill_step) as backfill,
        patch.object(cli, "probe_pending", side_effect=probe_step),
    ):
        assert cli.main(["--backfill-domains", "5", "--backfill-minutes", "2"]) == 0
    assert calls == ["web", "probe"]
    assert backfill.call_args.kwargs["minutes"] == 2
    assert "website backfill: 1/1 found" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv", [["--backfill-domains", "-1"], ["--backfill-domains", "1", "--backfill-minutes", "0"]]
)
def test_cli_rejects_bad_backfill_bounds(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(argv)


def test_snowflake_domain_save_fills_only_an_empty_domain() -> None:
    conn, store_ = make_conn()
    snowdb.save_company_domain(conn, "Acme", "acme.example")
    verb, sql, params = store_["executed"][-1]
    assert verb == "UPDATE" and "COALESCE(NULLIF(domain, ''), ?)" in sql
    assert params[0] == "acme.example" and params[-1] == "Acme"
    assert "domain_attempts=COALESCE(domain_attempts, 0) + 1" in sql
