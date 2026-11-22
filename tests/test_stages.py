"""A split run (fetch with no model, then read with no browser) saves what one
collect run saves, and the read stage takes only plain, well formed postings
from the fetch file (Nov 22 2026: the cloud's browsing job holds no token)."""

from __future__ import annotations

import json
from unittest.mock import patch

from sourcer import config, sqlitedb, stages
from sourcer.main import build_state
from tests.conftest import GOOD_PARSE_SCORE, make_client, worker_replies
from tests.test_pipeline import TITLES_OK, fake_get


def test_fetch_then_read_saves_what_one_run_saves(paths, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "COST_CAP_USD", 2.5)
    out = tmp_path / "fetched.json"
    with patch("sourcer.net.get", side_effect=fake_get):
        body = stages.fetch(out, build_state(client=False))
    assert body["unsure"] and all("title" in p for p in body["unsure"])  # left for the title check
    analyst = {**GOOD_PARSE_SCORE, "title": "Data Analyst", "employment_type": "full time",
               "start_date": "immediate start", "seniority": "entry", "score": 75}  # fmt: skip
    client = make_client(
        [
            TITLES_OK,
            *worker_replies(GOOD_PARSE_SCORE, {"start_quote": "Summer 2027 internship"}),
            *worker_replies(
                analyst,
                {"start_quote": "Start immediately, full time", "employment_quote": "full time"},
                scored=False,
            ),
        ]
    )
    with patch("sourcer.net.get", side_effect=AssertionError("the read stage opens no board")):
        final = stages.read(out, build_state(client))
    stats = final["stats"]
    assert (
        stats["postings_seen"] == 4 and stats["new_postings"] == 2 and stats["prefiltered_out"] == 2
    )
    assert stats["excluded_count"] == 1 and stats["fetch_rejected"] == 0
    assert client.messages.create.call_count == 4  # the same calls as one collect run
    conn = sqlitedb.connect(config.DB_PATH)
    rows = {r["title"]: dict(r) for r in conn.execute("SELECT * FROM postings")}
    assert rows["AI Engineer Intern"]["score"] == 88
    assert rows["Data Analyst"]["exclusion_reason"] == "full time with immediate start"
    assert conn.execute("SELECT COUNT(*) AS n FROM run_logs").fetchone()["n"] == 1


def test_the_read_stage_takes_only_plain_well_formed_postings(tmp_path):
    good = {"company": "Acme", "title": "Data Intern", "url": "https://boards.example/1",
            "location": "NYC", "tags": ["a"], "_events": [{"stage": "found"}]}  # fmt: skip
    bad = [
        {**good, "url": "http://boards.example/1"},
        {**good, "url": "https://user:pw@boards.example/1"},
        {**good, "company": ""},
        {**good, "extra": {"nested": "object"}},
        {**good, "description": "x" * (stages.MAX_TEXT + 1)},
        "not a dict",
    ]
    assert stages.clean_posting(good) == good
    assert all(stages.clean_posting(b) is None for b in bad)
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"format": 1, "postings": [good, *bad], "unsure": [], "stats": {}}))
    postings, unsure, stats = stages.load(path)
    assert postings == [good] and unsure == [] and stats["fetch_rejected"] == len(bad)
    path.write_text(json.dumps({"format": 2, "postings": [], "unsure": []}))
    try:
        stages.load(path)
    except ValueError:
        pass
    else:
        raise AssertionError("a file of another format is refused")
