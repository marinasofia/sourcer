"""CLI modules: track, rescore, alerts, verify, batch, score_listings. No network, no API."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
import requests

from sourcer import alerts, batch, db, rescore, score_listings, track
from sourcer.apply import verify
from sourcer.apply.recheck import Official
from tests.conftest import GOOD_PARSE_SCORE, SKILLS, current_trace, make_client, worker_replies

# --------------------------------------------------------------- track


def seed_board(conn, posting_row):
    db.insert_posting(conn, posting_row(hash="1", title="AI Engineer", score=90))
    db.insert_posting(conn, posting_row(hash="2", title="Listing", url="u2", score=None))
    db.insert_posting(conn, posting_row(hash="3", title="Sr", url="u3", score=0, excluded=True))
    conn.commit()


def test_track_list_and_filters(tmp_db, posting_row, monkeypatch, capsys):
    seed_board(tmp_db, posting_row)
    monkeypatch.setattr(track.db, "connect", lambda: tmp_db)
    assert track.main([]) == 0
    out = capsys.readouterr().out
    assert "2 postings" in out and "[ 90]" in out and "[  -]" in out
    db.set_status(tmp_db, 1, "applied")
    assert track.main(["list", "applied"]) == 0
    assert "1 postings" in capsys.readouterr().out
    assert track.main(["list", "bogus"]) == 2
    assert "unknown status" in capsys.readouterr().err


def test_track_set_paths(tmp_db, posting_row, monkeypatch, capsys):
    seed_board(tmp_db, posting_row)
    monkeypatch.setattr(track.db, "connect", lambda: tmp_db)
    assert track.main(["set", "1", "shortlist", "looks", "good"]) == 0
    assert "updated" in capsys.readouterr().out
    row = tmp_db.execute("SELECT status, notes FROM postings WHERE id=1").fetchone()
    assert row["status"] == "shortlist" and row["notes"] == "looks good"
    assert track.main(["set", "999", "applied"]) == 0
    assert "no posting with id 999" in capsys.readouterr().out
    assert track.main(["set", "1", "bogus"]) == 2
    assert "status must be one of" in capsys.readouterr().err
    assert track.main(["set", "1"]) == 2
    assert "Application tracker" in capsys.readouterr().err


def test_track_main_reads_argv(tmp_db, monkeypatch, capsys):
    monkeypatch.setattr(track.db, "connect", lambda: tmp_db)
    monkeypatch.setattr("sys.argv", ["track", "list"])
    assert track.main() == 0
    assert "0 postings" in capsys.readouterr().out


# --------------------------------------------------------------- rescore


def test_rescore_row_success_and_failure(tmp_db, posting_row):
    db.insert_posting(tmp_db, posting_row(hash="1", employment_type="unspecified", start_date=None))
    tmp_db.commit()
    row = tmp_db.execute(rescore.SELECT_SCORED).fetchone()
    client = make_client([json.dumps({"score": 55, "rationale": "Partial fit."})])
    assert rescore.rescore_row(tmp_db, client, rescore.llm.Usage(), row, SKILLS) is True
    stored = tmp_db.execute("SELECT score, rationale, timing_unclear FROM postings").fetchone()
    assert stored["score"] == 55 and stored["timing_unclear"] == 1
    assert "[timing unclear" in stored["rationale"]
    bad = make_client(["x", "y"])
    assert rescore.rescore_row(tmp_db, bad, rescore.llm.Usage(), row, SKILLS) is False


def test_rescore_main_with_cost_cap(tmp_db, paths, posting_row, monkeypatch, capsys):
    db.insert_posting(tmp_db, posting_row(hash="1"))
    db.insert_posting(tmp_db, posting_row(hash="2", url="u2", location="Remote US"))
    tmp_db.commit()
    monkeypatch.setattr(rescore.db, "connect", lambda: tmp_db)
    monkeypatch.setattr(
        rescore.llm,
        "get_client",
        lambda: make_client([json.dumps({"score": 60, "rationale": "ok"})]),
    )
    # one call costs 100 in + 50 out tokens: 0.00035 USD; cap after the first
    monkeypatch.setattr(rescore.config, "COST_CAP_USD", 0.0003)
    assert rescore.main() == 0
    out = capsys.readouterr().out
    assert "rescored 1/2" in out and "skipped_by_cost_cap=1" in out


def test_rescore_main_side_by_side_counts_a_failed_call(
    tmp_db, paths, posting_row, monkeypatch, capsys
):
    db.insert_posting(tmp_db, posting_row(hash="1"))
    db.insert_posting(tmp_db, posting_row(hash="2", url="u2", location="Remote US"))
    tmp_db.commit()
    monkeypatch.setattr(rescore.db, "connect", lambda: tmp_db)
    client = make_client([])
    replies = iter([json.dumps({"score": 60, "rationale": "ok"})])

    def create(**_):
        try:
            return make_client([next(replies)]).messages.create()
        except StopIteration:
            raise RuntimeError("claude CLI timed out") from None

    client.messages.create.side_effect = create
    monkeypatch.setattr(rescore.llm, "get_client", lambda: client)
    assert rescore.main(["--workers", "2"]) == 0
    out = capsys.readouterr().out
    assert "rescored 1/2" in out and "failures=1" in out


def test_rescore_stale_reads_only_scores_an_older_scorer_made(
    tmp_db, paths, posting_row, monkeypatch, capsys
):
    from sourcer import config  # noqa: PLC0415

    now = current_trace(json.loads(config.SKILLS_PATH.read_text()))
    db.insert_posting(tmp_db, {**posting_row(hash="1"), "score_trace": now})
    db.insert_posting(tmp_db, posting_row(hash="2", url="u2", location="Remote US"))
    tmp_db.commit()
    monkeypatch.setattr(rescore.db, "connect", lambda: tmp_db)
    client = make_client([json.dumps({"score": 60, "rationale": "ok"})])
    monkeypatch.setattr(rescore.llm, "get_client", lambda: client)
    assert rescore.main(["--stale"]) == 0
    out = capsys.readouterr().out
    assert "stale scores: {'no score trace': 1}" in out and "rescored 1/1" in out


# --------------------------------------------------------------- alerts


def alert_rows(conn, posting_row):
    # full time rows: only the score decides, not the internship rule
    db.insert_posting(
        conn, posting_row(hash="1", title="Great", score=95, employment_type="full time")
    )
    db.insert_posting(
        conn, posting_row(hash="2", title="Meh", url="u2", score=50, employment_type="full time")
    )
    conn.commit()


def test_alerts_ci_mode(tmp_db, posting_row, monkeypatch, capsys):
    alert_rows(tmp_db, posting_row)
    monkeypatch.setattr(alerts.db, "connect", lambda: tmp_db)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/repo")
    monkeypatch.setattr(alerts.shutil, "which", lambda name: "/usr/bin/gh")
    calls = []
    monkeypatch.setattr(alerts.subprocess, "run", lambda argv, **kw: calls.append(argv))
    assert alerts.main() == 0
    assert calls and calls[0][0] == "/usr/bin/gh" and "repos/me/repo/issues" in calls[0]
    assert "Sourcer: 1 new target posting(s)" in calls[0][4]
    assert tmp_db.execute("SELECT alerted FROM postings WHERE id=1").fetchone()["alerted"] == 1
    assert tmp_db.execute("SELECT alerted FROM postings WHERE id=2").fetchone()["alerted"] == 0
    assert alerts.main() == 0
    assert "no new alertable postings" in capsys.readouterr().out


def test_alerts_local_mode_and_missing_gh(tmp_db, posting_row, monkeypatch, capsys):
    alert_rows(tmp_db, posting_row)
    monkeypatch.setattr(alerts.db, "connect", lambda: tmp_db)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert alerts.main() == 0
    out = capsys.readouterr().out
    assert "Great" in out and "Meh" not in out
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/repo")
    monkeypatch.setattr(alerts.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="gh CLI not found"):
        alerts.create_issue("t", "b")


def test_alerts_flood_cap(monkeypatch):
    monkeypatch.setattr(alerts, "ALERT_CAP", 2)
    rows = [
        {
            "id": i,
            "company": f"C{i}",
            "title": f"T{i}",
            "url": "u",
            "score": None,
            "employment_type": None,
            "start_date": None,
            "posted_at": None,
        }
        for i in range(4)
    ]
    body = alerts.build_body(rows)
    assert "and 2 more" in body and "[ - ]" in body and "T3" not in body


# --------------------------------------------------------------- verify


def resp(status=200, text="", url="https://job-boards.greenhouse.io/x/jobs/1"):
    r = MagicMock()
    r.status_code = status
    r.ok = 200 <= status < 300
    r.text = text
    r.url = url
    return r


WD = "https://tenant.wd5.myworkdayjobs.com/en-US/careers/job/City/Role_R-1"


def test_check_workday_branches(monkeypatch):
    assert verify.check_workday("https://example.com/x") is None
    monkeypatch.setattr(verify.net, "get", lambda *a, **k: resp(404))
    assert verify.check_workday(WD) == "dead"
    monkeypatch.setattr(verify.net, "get", lambda *a, **k: resp(200, '{"jobPostingInfo": 1}'))
    assert verify.check_workday(WD) == "verified"
    monkeypatch.setattr(verify.net, "get", lambda *a, **k: resp(200, "{}"))
    assert verify.check_workday(WD) is None

    def boom(*a, **k):
        raise requests.ConnectionError()

    monkeypatch.setattr(verify.net, "get", boom)
    assert verify.check_workday(WD) is None


def test_check_one_workday_and_non_ok(monkeypatch):
    row = {"id": 7, "url": WD, "title": "Role", "company": "Tenant"}
    monkeypatch.setattr(verify.net, "get", lambda *a, **k: resp(404))
    assert verify.check_one(row) == (7, "dead")
    row = {"id": 8, "url": "https://x.com/j", "title": "Role", "company": "X"}
    monkeypatch.setattr(verify.net, "get", lambda *a, **k: resp(500))
    assert verify.check_one(row) == (8, "unknown")
    monkeypatch.setattr(verify.net, "get", lambda *a, **k: resp(410))
    assert verify.check_one(row) == (8, "dead")


def test_classify_page_unknown_host():
    row = {"id": 1, "url": "u", "title": "Quantum Researcher", "company": "Acme"}
    r = resp(200, "<div></div>", url="https://random-site.org/jobs/1")
    assert verify.classify_page(row, r) == "unknown"
    row = {"id": 1, "url": "u", "title": "", "company": ""}
    assert verify.classify_page(row, r) == "unknown"


def test_apply_results_archives_dead_and_closed(tmp_db, posting_row):
    for i, status in enumerate(("dead", "closed", "live"), 1):
        db.insert_posting(
            tmp_db,
            posting_row(hash=str(i), url=f"u{i}", source="simplify", location=f"New York {i}"),
        )
        del status
    db.set_status(tmp_db, 3, "applied")
    tmp_db.commit()
    counts = verify.apply_results(tmp_db, [(1, "dead"), (2, "closed"), (3, "dead")])
    assert counts == {"dead": 2, "closed": 1}
    rows = {r["id"]: r for r in tmp_db.execute("SELECT id, status, notes, verified FROM postings")}
    assert rows[1]["status"] == "archived" and "dead" in rows[1]["notes"]
    assert rows[2]["status"] == "archived"
    assert rows[3]["status"] == "applied" and rows[3]["verified"] == "dead"


def test_verify_main(tmp_db, posting_row, monkeypatch, capsys):
    db.insert_posting(tmp_db, posting_row(hash="a", source="ats"))
    db.insert_posting(
        tmp_db,
        posting_row(hash="s", url="https://x.co/1", source="simplify", location="Remote US"),
    )
    tmp_db.commit()
    monkeypatch.setattr(verify.db, "connect", lambda: tmp_db)
    monkeypatch.setattr(verify.net, "get", lambda *a, **k: resp(404))
    assert verify.main([]) == 0
    assert '"dead": 1' in capsys.readouterr().out
    assert (
        tmp_db.execute("SELECT verified FROM postings WHERE source='ats'").fetchone()[0] == "source"
    )
    assert verify.main([]) == 0
    assert "nothing to verify" in capsys.readouterr().out
    assert verify.main(["--all"]) == 0
    assert "verified 1 listings" in capsys.readouterr().out


# --------------------------------------------------------------- score_listings


def listing(posting_row, **over):
    base = {
        "hash": "l1",
        "title": "AI Engineer Intern",
        "url": "https://boards.greenhouse.io/x/1",
        "score": None,
        "source": "simplify",
        "employment_type": "intern",
        "start_date": "summer 2027",
        "requirements": [],
        "tech_stack": [],
        "rationale": None,
    }
    base.update(over)
    return posting_row(**base)


class Page:
    text = "<p>" + "Python and SQL. " * 60 + "</p>"

    def raise_for_status(self):
        pass


def test_score_listings_main_happy_and_cap(tmp_db, paths, posting_row, monkeypatch, capsys):
    monkeypatch.setattr(score_listings, "check_titles", lambda *a: 0)  # tested on its own
    db.insert_posting(tmp_db, listing(posting_row))
    db.insert_posting(
        tmp_db,
        listing(
            posting_row,
            hash="l2",
            url="https://boards.greenhouse.io/x/2",
            location="Remote US",
        ),
    )
    tmp_db.commit()
    monkeypatch.setattr(score_listings.db, "connect", lambda: tmp_db)
    monkeypatch.setattr(score_listings.net, "get", lambda *a, **k: Page())
    monkeypatch.setattr(score_listings.llm, "get_client", lambda: make_client(worker_replies() * 2))
    # room for one posting's two calls (facts, then fit), not a second posting
    monkeypatch.setattr(score_listings.config, "COST_CAP_USD", 0.0006)
    assert score_listings.main([]) == 0
    out = capsys.readouterr().out
    assert "[1/2]" in out and "cost cap reached after 1" in out
    scored = tmp_db.execute(
        "SELECT score FROM postings WHERE url='https://boards.greenhouse.io/x/1'"
    ).fetchone()["score"]
    assert scored == 88
    # the twin now covers the second row without a model call
    monkeypatch.setattr(score_listings.config, "COST_CAP_USD", 2.5)
    monkeypatch.setattr(score_listings.llm, "get_client", lambda: make_client([]))
    assert score_listings.main([]) == 0
    assert "reused twin score 88" in capsys.readouterr().out
    assert score_listings.main([]) == 0
    assert "nothing to score" in capsys.readouterr().out


def test_check_titles_archives_unwanted_listings_once(tmp_db, paths, posting_row):
    db.insert_posting(tmp_db, listing(posting_row, title="Data Analyst Intern"))
    db.insert_posting(
        tmp_db,
        listing(posting_row, title="Nurse", hash="l2", url="https://boards.greenhouse.io/x/2"),
    )
    tmp_db.commit()
    client = make_client([json.dumps({"match": [1]})])
    assert score_listings.check_titles(tmp_db, client, score_listings.llm.Usage()) == 1
    rows = dict(tmp_db.execute("SELECT title, status FROM postings").fetchall())
    assert rows == {"Data Analyst Intern": "new", "Nurse": "archived"}
    note = tmp_db.execute("SELECT notes FROM postings WHERE title='Nurse'").fetchone()[0]
    assert note == score_listings.TITLE_NOTE
    # judged titles are never sent again
    assert score_listings.check_titles(tmp_db, make_client([]), score_listings.llm.Usage()) == 0


def test_score_one_fetch_error_and_parse_error(tmp_db, paths, posting_row, monkeypatch):
    db.insert_posting(tmp_db, listing(posting_row, location="Remote US"))
    tmp_db.commit()
    row = score_listings.select_rows(tmp_db, 10)[0]

    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(score_listings.net, "get", boom)
    out = score_listings.score_one(
        tmp_db, make_client([]), score_listings.llm.Usage(), row, skills=SKILLS, profile=None
    )
    assert out.startswith("fetch failed")
    monkeypatch.setattr(score_listings.net, "get", lambda *a, **k: Page())
    out = score_listings.score_one(
        tmp_db,
        make_client(["x", "y"]),
        score_listings.llm.Usage(),
        row,
        skills=SKILLS,
        profile=None,
    )
    assert out.startswith("parse+score:")


def test_a_stale_listing_is_held_back_before_any_fetch_or_model_call(
    tmp_db, paths, posting_row, monkeypatch
):
    db.insert_posting(tmp_db, listing(posting_row, location="Remote US"))
    tmp_db.commit()
    row = score_listings.select_rows(tmp_db, 10)[0]
    monkeypatch.setattr(
        score_listings,
        "RECHECK",
        lambda r: ("posted 144 days ago on the company's board", Official(True, "2026-04-03")),
    )

    def no_fetch(*a, **k):
        raise AssertionError("a stale listing must not be fetched")

    monkeypatch.setattr(score_listings.net, "get", no_fetch)
    out = score_listings.score_one(
        tmp_db, make_client([]), score_listings.llm.Usage(), row, skills=SKILLS, profile=None
    )
    assert out == "held back: posted 144 days ago on the company's board"
    got = tmp_db.execute(
        "SELECT score, excluded, exclusion_reason FROM postings WHERE id=?", (row["id"],)
    ).fetchone()
    assert (got["score"], got["excluded"]) == (0, 1)
    assert got["exclusion_reason"].startswith("stale: posted 144 days")
    assert score_listings.select_rows(tmp_db, 10) == []  # never picked again


def test_apply_parsed_excluded_and_board_fields_win(tmp_db, posting_row):
    db.insert_posting(
        tmp_db, listing(posting_row, employment_type="full time", start_date="2028 new grad")
    )
    tmp_db.commit()
    row = score_listings.select_rows(tmp_db, 10)[0]
    parsed = {**GOOD_PARSE_SCORE, "years_experience_min": 6}
    out = score_listings.apply_parsed(tmp_db, row, parsed, None)
    assert out.startswith("excluded (requires 6+")
    stored = tmp_db.execute("SELECT excluded, score FROM postings").fetchone()
    assert stored["excluded"] == 1 and stored["score"] == 0
    parsed = dict(GOOD_PARSE_SCORE)
    assert score_listings.apply_parsed(tmp_db, row, parsed, None) == "scored 88"
    assert parsed["employment_type"] == "full time" and parsed["start_date"] == "2028 new grad"


def test_ashby_board_failure_is_cached(monkeypatch):
    score_listings._ashby_boards.clear()
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise requests.ConnectionError()

    monkeypatch.setattr(score_listings.net, "get", boom)
    assert score_listings._ashby_board("org-x") == []
    assert score_listings._ashby_board("org-x") == []
    assert len(calls) == 1
    text, err = score_listings.fetch_description("https://jobs.ashbyhq.com/org-x/abcdef12-3456")
    assert text is None and "not on the ashby board" in err
    score_listings._ashby_boards.clear()


def test_ashby_success_and_unreadable_page(monkeypatch):
    score_listings._ashby_boards.clear()
    board = {
        "jobs": [
            {
                "id": "abcdef12-3456",
                "jobUrl": "https://jobs.ashbyhq.com/org-z/abcdef12-3456",
                "descriptionHtml": "<p>" + "Build agents in Python. " * 40 + "</p>",
            }
        ]
    }

    class Board:
        def raise_for_status(self):
            pass

        def json(self):
            return board

    monkeypatch.setattr(score_listings.net, "get", lambda *a, **k: Board())
    text, err = score_listings.fetch_description("https://jobs.ashbyhq.com/org-z/abcdef12-3456")
    assert err is None and text.startswith("Build agents")
    score_listings._ashby_boards.clear()

    class Tiny:
        text = "<div>shell</div>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(score_listings.net, "get", lambda *a, **k: Tiny())
    assert score_listings.fetch_description("https://boards.greenhouse.io/x/9") == (
        None,
        "page not readable",
    )


def test_apply_parsed_keeps_model_fields_when_row_blank(tmp_db, posting_row):
    db.insert_posting(tmp_db, listing(posting_row, employment_type=None, start_date=None))
    tmp_db.commit()
    row = score_listings.select_rows(tmp_db, 10)[0]
    parsed = dict(GOOD_PARSE_SCORE)
    assert score_listings.apply_parsed(tmp_db, row, parsed, None) == "scored 88"
    assert parsed["employment_type"] == "intern" and parsed["start_date"] == "summer 2027"


def test_ashby_short_description(monkeypatch):
    score_listings._ashby_boards.clear()
    score_listings._ashby_boards["org-y"] = [
        {"jobUrl": "https://jobs.ashbyhq.com/org-y/abcdef12-3456", "descriptionPlain": "short"}
    ]
    text, err = score_listings.fetch_description("https://jobs.ashbyhq.com/org-y/abcdef12-3456")
    assert text is None and err == "ashby description too short"
    score_listings._ashby_boards.clear()


# --------------------------------------------------------------- batch


def fake_batch_client(results):
    client = MagicMock()
    client.messages.batches.create.return_value = MagicMock(id="b1")
    running = MagicMock(id="b1", processing_status="in_progress")
    ended = MagicMock(id="b1", processing_status="ended")
    client.messages.batches.retrieve.side_effect = [running, ended]
    client.messages.batches.results.return_value = results
    return client


def batch_result(custom_id, text=None, kind="succeeded"):
    r = MagicMock()
    r.custom_id = custom_id
    r.result.type = kind
    block = MagicMock()
    block.type = "text"
    block.text = text or ""
    other = MagicMock()
    other.type = "tool_use"
    r.result.message.content = [other, block]
    return r


def test_submit_and_apply(tmp_db, posting_row, monkeypatch):
    db.insert_posting(tmp_db, listing(posting_row, location="Remote US"))
    tmp_db.commit()
    rid = tmp_db.execute("SELECT id FROM postings").fetchone()["id"]
    monkeypatch.setattr(batch.time, "sleep", lambda s: None)
    client = fake_batch_client(
        [batch_result(str(rid), json.dumps(GOOD_PARSE_SCORE)), batch_result("999", kind="errored")]
    )
    reqs = [{"custom_id": str(rid), "params": {}}]
    assert batch.submit_and_apply(tmp_db, client, reqs, None) == (1, 1)
    assert tmp_db.execute("SELECT score FROM postings").fetchone()["score"] == 88
    assert client.messages.batches.retrieve.call_count == 2


def test_apply_result_row_vanished_and_bad_reply(tmp_db):
    assert batch.apply_result(tmp_db, 12345, json.dumps(GOOD_PARSE_SCORE)) == "row vanished"
    assert batch.apply_result(tmp_db, 12345, "{}").startswith("parse+score: missing key")


def test_build_requests_reuses_twin(tmp_db, posting_row, monkeypatch):
    twin = posting_row(hash="scored", title="AI Engineer Intern", score=66)
    db.insert_posting(tmp_db, {**twin, "score_trace": current_trace(SKILLS)})
    db.insert_posting(tmp_db, listing(posting_row, location="Remote US"))
    tmp_db.commit()
    rows = batch.select_rows(tmp_db, 10)
    reqs, skipped, reused = batch.build_requests(tmp_db, rows, SKILLS)
    assert (reqs, skipped, reused) == ([], [], 1)
    assert (
        tmp_db.execute(
            "SELECT score FROM postings WHERE url='https://boards.greenhouse.io/x/1'"
        ).fetchone()["score"]
        == 66
    )


def batch_setup(tmp_db, posting_row, monkeypatch, n=1):
    for i in range(n):
        db.insert_posting(
            tmp_db,
            listing(
                posting_row,
                hash=f"b{i}",
                url=f"https://boards.greenhouse.io/x/{i}",
                location=f"New York {i}",
            ),
        )
    tmp_db.commit()
    monkeypatch.setattr(batch.db, "connect", lambda: tmp_db)
    monkeypatch.setattr(score_listings.net, "get", lambda *a, **k: Page())


def test_batch_main_nothing_to_score(tmp_db, paths, monkeypatch, capsys):
    monkeypatch.setattr(batch.db, "connect", lambda: tmp_db)
    assert batch.main([]) == 0
    assert "nothing to score" in capsys.readouterr().out


def test_batch_main_nothing_to_send(tmp_db, paths, posting_row, monkeypatch, capsys):
    batch_setup(tmp_db, posting_row, monkeypatch)

    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(score_listings.net, "get", boom)
    assert batch.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "unfetchable 1" in out and "nothing to send" in out


def test_batch_main_dry_run_and_cap(tmp_db, paths, posting_row, monkeypatch, capsys):
    batch_setup(tmp_db, posting_row, monkeypatch)
    assert batch.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "estimated batch cost" in out and "dry run: nothing submitted" in out
    assert tmp_db.execute("SELECT score FROM postings").fetchone()["score"] is None
    monkeypatch.setattr(batch.config, "COST_CAP_USD", 0.0)
    assert batch.main([]) == 1
    assert "exceeds SOURCER_COST_CAP" in capsys.readouterr().out


def test_batch_main_submit_path(tmp_db, paths, posting_row, monkeypatch, capsys):
    batch_setup(tmp_db, posting_row, monkeypatch)
    rid = tmp_db.execute("SELECT id FROM postings").fetchone()["id"]
    monkeypatch.setattr(batch.time, "sleep", lambda s: None)
    client = fake_batch_client([batch_result(str(rid), json.dumps(GOOD_PARSE_SCORE))])
    monkeypatch.setattr(batch.llm, "get_client", lambda: client)
    assert batch.main([]) == 0
    out = capsys.readouterr().out
    assert "submitted batch b1" in out and "done: 1 applied, 0 failed" in out
    assert tmp_db.execute("SELECT score FROM postings").fetchone()["score"] == 88


def test_apply_parsed_flags_unclear_timing(tmp_db, posting_row) -> None:
    """A reply with unspecified timing is scored and flagged, not excluded."""
    db.insert_posting(tmp_db, posting_row(score=None, employment_type=None, start_date=None))
    row = tmp_db.execute(
        "SELECT id, title, employment_type, start_date FROM postings LIMIT 1"
    ).fetchone()
    parsed = {
        **GOOD_PARSE_SCORE,
        "employment_type": "unspecified",
        "start_date": None,
        "work_mode": "remote",
    }
    assert score_listings.apply_parsed(tmp_db, row, parsed, None) == "scored 88"
    stored = tmp_db.execute("SELECT rationale, timing_unclear FROM postings").fetchone()
    assert stored["timing_unclear"] == 1
    assert "timing unclear" in stored["rationale"]


def test_apply_results_leaves_live_rows_in_the_inbox(tmp_db, posting_row) -> None:
    db.insert_posting(tmp_db, posting_row(source="simplify"))
    pid = tmp_db.execute("SELECT id FROM postings").fetchone()["id"]
    counts = verify.apply_results(tmp_db, [(pid, "verified")])
    row = tmp_db.execute("SELECT status, verified FROM postings").fetchone()
    assert counts == {"verified": 1}
    assert (row["status"], row["verified"]) == ("new", "verified")
