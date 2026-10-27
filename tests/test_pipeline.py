"""End-to-end graph run: fake client, patched board fetch, temporary database."""

from __future__ import annotations

import json
from unittest.mock import patch

from sourcer import config, llm, sqlitedb
from sourcer.digest import render_digest
from sourcer.main import build_state, run
from sourcer.pipeline import graph
from tests.conftest import FROZEN_NOW, GOOD_PARSE_SCORE, make_client, worker_replies

GREENHOUSE = {
    "jobs": [
        {
            "title": "AI Engineer Intern",
            "location": {"name": "New York, NY"},
            "absolute_url": "https://boards.greenhouse.io/testco/jobs/1",
            "content": "<p>Summer 2027 internship. Python and LangGraph.</p>",
            "first_published": "2026-08-20T00:00:00Z",
        },
        {
            "title": "Senior Data Scientist",
            "location": {"name": "New York, NY"},
            "absolute_url": "https://boards.greenhouse.io/testco/jobs/2",
            "content": "<p>Internship listing that still requires ten years.</p>",
            "first_published": "2026-08-20T00:00:00Z",
        },
        {
            "title": "Barista",
            "location": {"name": "Toronto"},
            "absolute_url": "https://boards.greenhouse.io/testco/jobs/3",
            "content": "<p>Coffee.</p>",
            "first_published": "2026-08-20T00:00:00Z",
        },
    ]
}
LEVER = [
    {
        "text": "Data Analyst",
        "categories": {"location": "Remote US"},
        "hostedUrl": "https://jobs.lever.co/leverco/1",
        "descriptionPlain": "Entry level. SQL and dashboards. Start immediately, full time.",
        "createdAt": 1787000000000,
    }
]


# the title check admits both titles that pass the gates
TITLES_OK = json.dumps({"match": [1, 2]})


class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        pass


def fake_get(url, **kwargs):
    if "greenhouse" in url:
        return FakeResp(GREENHOUSE)
    if "lever" in url:
        return FakeResp(LEVER)
    return FakeResp([])  # simplify lists


def test_graph_has_five_nodes_in_order():
    assert [name for name, _ in graph.NODES] == ["source", "dedupe", "parse", "score", "output"]
    assert graph.build_graph() is not None


def test_full_run_writes_rows_run_log_and_digest(paths, monkeypatch):
    monkeypatch.setattr(config, "COST_CAP_USD", 2.5)
    analyst = {
        **GOOD_PARSE_SCORE,
        "title": "Data Analyst",
        "employment_type": "full time",
        "start_date": "immediate start",
        "seniority": "entry",
        "score": 75,
    }
    client = make_client(
        [
            TITLES_OK,
            *worker_replies(GOOD_PARSE_SCORE, {"start_quote": "Summer 2027 internship"}),
            # the rules stop the immediate full time role on its checked facts: never scored
            *worker_replies(
                analyst,
                {"start_quote": "Start immediately, full time", "employment_quote": "full time"},
                scored=False,
            ),
        ]
    )
    with patch("sourcer.net.get", side_effect=fake_get):
        final = run(build_state(client))

    stats = final["stats"]
    assert stats["companies_covered"] == 2
    assert stats["postings_seen"] == 4
    assert stats["prefiltered_out"] == 2  # the Toronto barista and senior title
    assert stats["new_postings"] == 2
    assert stats["parse_failures"] == 0
    assert stats["excluded_count"] == 1  # immediate-start full time
    # one title check, two fact reads, one fit score; the senior title never reached the model
    assert client.messages.create.call_count == 4
    assert stats["duration_s"] >= 0

    conn = sqlitedb.connect(config.DB_PATH)
    rows = {r["title"]: dict(r) for r in conn.execute("SELECT * FROM postings")}
    assert rows["AI Engineer Intern"]["score"] == 88
    assert rows["AI Engineer Intern"]["description"]  # the text is kept for matching
    assert rows["AI Engineer Intern"]["level_fit"] == "fit"
    assert "Senior Data Scientist" not in rows
    assert rows["Data Analyst"]["exclusion_reason"] == "full time with immediate start"
    runs = [dict(r) for r in conn.execute("SELECT * FROM run_logs")]
    assert len(runs) == 1
    assert runs[0]["postings_seen"] == 4
    assert json.loads(runs[0]["node_metrics"])["parse"]["parsed"] == 2

    text = render_digest(final)
    assert " 1. [ 88] TestCo - AI Engineer Intern" in text
    assert "Senior Data Scientist" not in text


def test_second_run_pays_nothing_for_seen_postings(paths, monkeypatch):
    monkeypatch.setattr(config, "COST_CAP_USD", 2.5)
    replies = [
        TITLES_OK,
        *worker_replies(),
        *worker_replies({**GOOD_PARSE_SCORE, "score": 40}),
    ]
    with patch("sourcer.net.get", side_effect=fake_get):
        run(build_state(make_client(replies)))
        second = make_client([])
        final = run(build_state(second))
    second.messages.create.assert_not_called()
    assert final["stats"]["new_postings"] == 0
    assert final["stats"]["dedupe_count"] == 2
    conn = sqlitedb.connect(config.DB_PATH)
    assert conn.execute("SELECT COUNT(*) AS n FROM run_logs").fetchone()["n"] == 2


def test_run_with_zero_cap_spends_nothing(paths, monkeypatch):
    monkeypatch.setattr(config, "COST_CAP_USD", 0.0)
    with (
        patch("sourcer.net.get", side_effect=fake_get),
        patch("sourcer.llm.get_client") as get_client,
    ):
        final = run()
    get_client.assert_not_called()
    assert final["usage"].est_cost_usd == 0.0
    # every title waits on the title check, which a zero cap skips: nothing is read
    assert final["stats"]["skipped_cost_cap"] == 0
    assert final["stats"]["new_postings"] == 0
    assert isinstance(final["usage"], llm.Usage)


def test_experienced_roles_never_reach_paid_scoring(paths, monkeypatch):
    monkeypatch.setattr(config, "COST_CAP_USD", 2.5)
    posted_at = FROZEN_NOW.isoformat()
    jobs = [
        {
            "title": "AI Solutions Engineer",
            "location": "New York, NY",
            "url": "u1",
            "description": "5+ years of experience. Full-time.",
            "posted_at": posted_at,
        },
        {
            "title": "AI Solutions Strategist",
            "location": "New York, NY",
            "url": "u2",
            "description": "4+ years of relevant experience. Full-time.",
            "posted_at": posted_at,
        },
    ]
    client = make_client([])
    with (
        patch("sourcer.sources.fetch_postings", return_value=jobs),
        patch("sourcer.sources.fetch_simplify", return_value=[]),
    ):
        final = run(build_state(client))
    client.messages.create.assert_not_called()
    assert final["stats"]["prefiltered_out"] == 4
    assert final["stats"]["new_postings"] == 0
    assert final["usage"].est_cost_usd == 0


def test_description_location_rejected_before_triage_or_scoring(paths, monkeypatch):
    monkeypatch.setattr(config, "COST_CAP_USD", 2.5)
    posted_at = FROZEN_NOW.isoformat()
    jobs = [
        {
            "title": title,
            "location": "San Francisco, California; Remote",
            "url": f"https://example.com/{index}",
            "description": (
                "This internship will take place from January 25 through April 16 "
                "and you will need to be able to work out of our SF office during this time."
            ),
            "posted_at": posted_at,
        }
        for index, title in enumerate(["Data Science Intern", "Product Intern"])
    ]
    client = make_client([])
    with (
        patch("sourcer.sources.fetch_postings", return_value=jobs),
        patch("sourcer.sources.fetch_simplify", return_value=[]),
    ):
        final = run(build_state(client))
    client.messages.create.assert_not_called()
    assert final["stats"]["prefiltered_out"] == 4
    assert final["stats"]["new_postings"] == 0
    assert final["usage"].est_cost_usd == 0
