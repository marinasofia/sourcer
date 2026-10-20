"""ATS probe candidate generation, ordering, persistence, and CLI behavior."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sourcer import main, probe, sqlitedb
from sourcer.probe import (
    ProbeResult,
    ProbeTarget,
    RateLimiter,
    board_owned_by,
    build_target,
    has_job_listing,
    linked_boards,
    probe_target,
)

TITLE = "<title>Jobs at TestCo</title>"


class FakeResponse:
    def __init__(self, status_code: int, text: str, url: str) -> None:
        self.status_code = status_code
        self.text = text
        self.url = url


class FakeRequester:
    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(url)
        return self.responses.get(url, FakeResponse(404, "", url))


def test_build_target_derives_name_domain_and_suffix_candidates() -> None:
    target = build_target("Arize AI")
    assert target.name == "Arize AI"
    assert target.domains == ("arizeai.com",)
    assert target.slugs[:2] == ("arizeai", "arize")
    assert "arizeaihq" in target.slugs

    url_target = build_target("https://www.example-labs.ai/careers")
    assert url_target.name == "example-labs"
    assert url_target.domains == ("example-labs.ai",)
    assert url_target.slugs[:2] == ("examplelabs", "example")

    board_target = build_target("https://jobs.ashbyhq.com/OpenAI")
    assert board_target.name == "OpenAI"
    assert board_target.slugs[0] == "openai"

    hyphenated = build_target("https://apply.workable.com/eramtalent-1")
    assert hyphenated.slugs[:2] == ("eramtalent-1", "eramtalent1")


def test_probe_uses_platform_order_and_stops_at_first_hit() -> None:
    target = ProbeTarget("TestCo", ("testco.com",), ("testco",))
    ashby = "https://jobs.ashbyhq.com/testco"
    greenhouse = "https://boards.greenhouse.io/testco"
    requester = FakeRequester(
        {
            ashby: FakeResponse(404, "", ashby),
            greenhouse: FakeResponse(
                200,
                '<title>Jobs at TestCo</title><a href="/testco/jobs/123">Engineer</a>',
                greenhouse,
            ),
        }
    )
    result = probe_target(target, requester=requester, limiter=RateLimiter(interval=0))
    assert result.ats_type == "greenhouse"
    assert result.ats_slug == "testco"
    assert requester.calls == [ashby, greenhouse]


def test_probe_skips_a_namesakes_board() -> None:
    """A guessed slug can land on another company's board: it must name ours."""
    target = ProbeTarget("Atoms", (), ("atoms", "atomsai"))
    other = "https://boards.greenhouse.io/atoms"
    ours = "https://boards.greenhouse.io/atomsai"
    listing = '<a href="/x/jobs/1">Engineer</a>'
    requester = FakeRequester(
        {
            other: FakeResponse(200, f"<title>Jobs at Quantum Matter</title>{listing}", other),
            ours: FakeResponse(200, f"<title>Jobs at Atoms</title>{listing}", ours),
        }
    )
    result = probe_target(target, requester=requester, limiter=RateLimiter(interval=0))
    assert result.ats_slug == "atomsai"


@pytest.mark.parametrize(
    ("page", "own_domain", "owned"),
    [
        ("<title>Hugging Face - Current Openings</title>", False, True),
        ('<meta property="og:site_name" content="TestCo">', False, True),
        ('<title>Jobs</title><script>{"name":"TestCo, Inc."}</script>', False, True),
        ("<title>Jobs at TestCompany</title>", False, False),
        ('<title>Careers</title><a href="https://testco.com">Home</a>', False, True),
        ('<title>Careers</title><a href="https://testco.com">Home</a>', True, False),
        ("<title>Jobs at Other</title>", False, False),
    ],
)
def test_board_ownership_evidence(page: str, own_domain: bool, owned: bool) -> None:
    name = "Hugging Face" if "Hugging" in page else "TestCo"
    target = ProbeTarget(name, ("testco.com",), ("testco",))
    response = FakeResponse(200, page, "https://boards.greenhouse.io/testco")
    assert board_owned_by(response, target, own_domain=own_domain) is owned


def test_board_may_drop_the_company_suffix() -> None:
    target = ProbeTarget("Metropolis Technologies", (), ("metropolis",))
    response = FakeResponse(200, "<title>Jobs at Metropolis</title>", "https://x")
    assert board_owned_by(response, target)


def test_board_redirecting_to_the_company_domain_is_owned() -> None:
    target = ProbeTarget("Justworks", ("justworks.com",), ("justworks",))
    response = FakeResponse(200, "<title>Careers</title>", "https://justworks.com/careers")
    assert board_owned_by(response, target)


def test_probe_records_every_attempt_when_unresolved() -> None:
    target = ProbeTarget("TestCo", ("testco.com",), ("testco",))
    requester = FakeRequester({})
    result = probe_target(target, requester=requester, limiter=RateLimiter(interval=0))
    assert result.status == "unresolved"
    assert result.hit_url is None
    assert result.urls_tried == tuple(requester.calls)
    assert len(result.urls_tried) == 10  # 6 hosted boards, 3 careers paths, the homepage


def test_listing_detection_requires_200_and_listing_evidence() -> None:
    url = "https://jobs.lever.co/testco"
    listing = '<a href="/testco/abc-123">Engineer</a>'
    assert has_job_listing(FakeResponse(200, listing, url), "lever")
    assert not has_job_listing(FakeResponse(404, listing, url), "lever")
    assert not has_job_listing(FakeResponse(200, "<h1>Careers</h1>", url), "lever")


def test_listing_detection_rejects_generic_markers_and_unrelated_links() -> None:
    workable = "https://apply.workable.com/testco"
    empty_page = '<script>window.labels = {"openings": "Current openings"}</script>'
    assert not has_job_listing(FakeResponse(200, empty_page, workable), "workable")

    smart = "https://careers.smartrecruiters.com/testco"
    corporate_link = '<a href="https://testco.example/en/jobs/open">Search jobs</a>'
    assert not has_job_listing(FakeResponse(200, corporate_link, smart), "smartrecruiters")


@pytest.mark.parametrize(
    ("ats_type", "verification_url", "verification_body"),
    [
        (
            "ashby",
            "https://api.ashbyhq.com/posting-api/job-board/testco",
            '{"jobs": [{"id": "one"}]}',
        ),
        (
            "greenhouse",
            "https://boards-api.greenhouse.io/v1/boards/testco/jobs?content=false",
            '{"jobs": [{"id": 1}]}',
        ),
        (
            "lever",
            "https://api.lever.co/v0/postings/testco?mode=json&limit=1",
            '[{"id": "one"}]',
        ),
        (
            "workable",
            "https://apply.workable.com/testco/llms.txt",
            "TestCo has 3 current openings",
        ),
    ],
)
def test_probe_uses_rate_limited_platform_verification(
    ats_type: str, verification_url: str, verification_body: str
) -> None:
    target = ProbeTarget("TestCo", ("testco.com",), ("testco",))
    platform_urls = {
        "ashby": "https://jobs.ashbyhq.com/testco",
        "greenhouse": "https://boards.greenhouse.io/testco",
        "lever": "https://jobs.lever.co/testco",
        "workable": "https://apply.workable.com/testco",
    }
    selected_url = platform_urls[ats_type]
    requester = FakeRequester(
        {
            selected_url: FakeResponse(200, f"{TITLE}<h1>Current openings</h1>", selected_url),
            verification_url: FakeResponse(200, verification_body, verification_url),
        }
    )
    result = probe_target(target, requester=requester, limiter=RateLimiter(interval=0))
    assert result.ats_type == ats_type
    assert result.urls_tried[-2:] == (selected_url, verification_url)


def test_workable_zero_openings_is_not_a_hit() -> None:
    target = ProbeTarget("TestCo", (), ("testco",))
    workable = "https://apply.workable.com/testco"
    manifest = f"{workable}/llms.txt"
    requester = FakeRequester(
        {
            workable: FakeResponse(200, f"{TITLE}<h1>Current openings</h1>", workable),
            manifest: FakeResponse(200, "TestCo has 0 current openings", manifest),
        }
    )
    result = probe_target(target, requester=requester, limiter=RateLimiter(interval=0))
    assert result.status == "unresolved"
    assert manifest in result.urls_tried


def test_empty_or_invalid_verification_payload_is_not_a_hit() -> None:
    target = ProbeTarget("TestCo", (), ("testco",))
    ashby = "https://jobs.ashbyhq.com/testco"
    verification = "https://api.ashbyhq.com/posting-api/job-board/testco"
    for body in ('{"jobs": []}', "not json"):
        requester = FakeRequester(
            {
                ashby: FakeResponse(200, f"{TITLE}<h1>Careers</h1>", ashby),
                verification: FakeResponse(200, body, verification),
            }
        )
        result = probe_target(target, requester=requester, limiter=RateLimiter(interval=0))
        assert result.status == "unresolved"


def test_rate_limiter_waits_between_requests() -> None:
    now = [10.0]
    sleeps: list[float] = []

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(interval=1.0, clock=clock, sleep=sleep)
    limiter.wait()
    now[0] += 0.25
    limiter.wait()
    assert sleeps == [0.75]


def test_probe_result_persistence_resolved_and_unresolved(tmp_db) -> None:
    resolved = ProbeResult(
        "ResolvedCo",
        "resolved.example",
        "ashby",
        "resolvedco",
        "https://jobs.ashbyhq.com/resolvedco",
        ("https://jobs.ashbyhq.com/resolvedco",),
    )
    sqlitedb.save_company_probe(tmp_db, resolved.__dict__)
    unresolved = ProbeResult(
        "UnknownCo",
        "unknown.example",
        None,
        None,
        None,
        ("https://jobs.ashbyhq.com/unknownco",),
    )
    sqlitedb.save_company_probe(tmp_db, unresolved.__dict__)
    rows = {row["name"]: row for row in sqlitedb.list_companies(tmp_db)}
    assert rows["ResolvedCo"]["probe_status"] == "resolved"
    assert rows["ResolvedCo"]["ats_type"] == "ashby"
    assert rows["ResolvedCo"]["submit_enabled"] == 0
    assert rows["UnknownCo"]["probe_status"] == "unresolved"
    assert json.loads(rows["UnknownCo"]["probe_urls"]) == list(unresolved.urls_tried)


def test_sourcer_main_dispatches_probe_without_running_pipeline() -> None:
    with patch("sourcer.probe.main", return_value=7) as probe_main:
        assert main.main(["probe", "TestCo"]) == 7
    probe_main.assert_called_once_with(["TestCo"])


def test_probe_all_selects_only_companies_without_ats() -> None:
    rows = [
        {"name": "KnownCo", "domain": "known.example", "ats_type": "ashby"},
        {"name": "UnknownCo", "domain": "unknown.example", "ats_type": None},
    ]
    result = ProbeResult("UnknownCo", "unknown.example", None, None, None, ("u",))
    with (
        patch.object(probe.db, "connect", return_value=MagicMock()),
        patch.object(probe.db, "list_companies", return_value=rows),
        patch.object(probe, "probe_target", return_value=result) as run_probe,
        patch.object(probe, "_save") as save,
    ):
        assert probe.main(["--all"]) == 1
    assert run_probe.call_args.args[0].name == "UnknownCo"
    save.assert_called_once()


def test_linked_boards_reads_hosted_board_links() -> None:
    page = (
        '<a href="https://job-boards.greenhouse.io/squarespace">Jobs</a>'
        '<script src="https://boards.greenhouse.io/embed/job_board/js?for=noom"></script>'
        '<a href="https://dataminr.wd12.myworkdayjobs.com/en-US/Dataminr">Open roles</a>'
        '<a href="https://jobs.ashbyhq.com/hebbia-ai/123">Role</a>'
        '<a href="https://boards.greenhouse.io/squarespace/jobs/9">Again</a>'
        '<a href="https://paypal.eightfold.ai/careers">Careers</a>'
        '<a href="https://egug.fa.us2.oraclecloud.com/">Colleagues</a>'
        '<a href="https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/">x</a>'
    )
    assert linked_boards(page) == [
        ("greenhouse", "squarespace"),
        ("greenhouse", "noom"),
        ("ashby", "hebbia-ai"),
        ("workday", "dataminr/wd12/Dataminr"),
        ("eightfold", "paypal"),
        ("oracle", "egug.fa.us2.oraclecloud.com/CX_1"),
        ("oracle", "jpmc.fa.oraclecloud.com/CX_1001"),
    ]


def test_probe_follows_a_board_link_on_the_company_site() -> None:
    target = ProbeTarget("TestCo", ("testco.com",), ("testco",))
    careers = FakeResponse(
        200,
        '<a href="https://jobs.lever.co/fake/1">x</a><a href="https://jobs.lever.co/testco-hq">Jobs</a>',
        "https://testco.com/careers",
    )
    requester = FakeRequester({"https://testco.com/careers": careers})
    readable = {("lever", "testco-hq")}
    result = probe_target(
        target,
        requester=requester,
        limiter=RateLimiter(interval=0),
        board_reader=lambda ats, slug, name: (ats, slug) in readable,
    )
    assert (result.ats_type, result.ats_slug) == ("lever", "testco-hq")
    assert result.hit_url == "https://testco.com/careers"


def test_a_known_site_is_read_before_guessing_board_names() -> None:
    target = build_target("TestCo", "https://testco.com")
    assert target.known_site
    careers = FakeResponse(
        200, '<a href="https://jobs.lever.co/testco-hq">Jobs</a>', "https://testco.com/careers"
    )
    requester = FakeRequester({"https://testco.com/careers": careers})
    result = probe_target(
        target,
        requester=requester,
        limiter=RateLimiter(interval=0),
        board_reader=lambda ats, slug, name: (ats, slug) == ("lever", "testco-hq"),
    )
    assert (result.ats_type, result.ats_slug) == ("lever", "testco-hq")
    assert not any("ashbyhq" in url or "greenhouse" in url for url in result.urls_tried)
    assert not build_target("TestCo").known_site
