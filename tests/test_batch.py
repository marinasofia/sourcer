"""Batch scoring and twin-reuse tests. No API calls, no network."""

import json
from unittest.mock import patch

from sourcer import batch, config, db, llm, score_listings
from sourcer.score_listings import apply_twin
from tests.conftest import current_trace

SKILLS = {
    "skills": [
        {"skill": "Python", "weight": 10, "category": "Languages", "synonyms": []},
        {"skill": "SQL", "weight": 9, "category": "Data", "synonyms": ["Postgres"]},
    ]
}

REPLY = {
    "title": "AI Engineer Intern",
    "location": "New York",
    "requirements": ["Python", "SQL"],
    "seniority": "intern",
    "tech_stack": ["Python"],
    "employment_type": "intern",
    "start_date": "summer 2027",
    "years_experience_min": 0,
    "work_mode": "remote",
    "score": 77,
    "rationale": "Strong Python and SQL match, gap in cloud.",
}


def make_db(tmp_path):
    return db.connect(tmp_path / "t.db")


def posting(**over):
    base = {
        "hash": "h1",
        "company": "TestCo",
        "title": "AI Engineer",
        "url": "https://boards.greenhouse.io/1",
        "score": None,
        "excluded": False,
        "tech_stack": [],
        "requirements": [],
        "source": "simplify",
        "location": "New York, NY",
    }
    base.update(over)
    return base


def rows_by_title(conn, title):
    return conn.execute(
        "SELECT id, company, title, url, location, employment_type, start_date "
        "FROM postings WHERE title=?",
        (title,),
    ).fetchall()


# --- prompt parity: batch must send exactly what the sync path sends ---


def test_batch_prompt_matches_sync_prompt():
    p = {
        "company": "TestCo",
        "title": "AI Engineer",
        "location": "NYC",
        "url": "https://x.com/1",
        "description": "Python and SQL.",
    }
    system, user = llm.parse_score_prompt(p, SKILLS)
    params = llm.call_params(system, user)
    assert params["model"] == config.MODEL
    assert params["system"][0]["text"] == system
    assert params["messages"][0]["content"] == user


# --- the cache marker must only appear when the API would honour it ---


def test_no_cache_marker_below_minimum():
    blocks = llm.system_blocks("x" * (config.CACHE_MIN_CHARS - 1))
    assert "cache_control" not in blocks[0]


def test_cache_marker_at_or_above_minimum():
    blocks = llm.system_blocks("x" * config.CACHE_MIN_CHARS)
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


# --- twin reuse ---


def test_find_scored_twin_matches_case_and_whitespace(tmp_path):
    conn = make_db(tmp_path)
    db.insert_posting(conn, posting(hash="a", score=80, rationale="r"))
    conn.commit()
    assert db.find_scored_twin(conn, "  testco ", "AI ENGINEER")["score"] == 80


def test_find_scored_twin_ignores_unscored(tmp_path):
    conn = make_db(tmp_path)
    db.insert_posting(conn, posting(hash="a", score=None))
    conn.commit()
    assert db.find_scored_twin(conn, "TestCo", "AI Engineer") is None


def test_find_scored_twin_none_for_unknown_role(tmp_path):
    conn = make_db(tmp_path)
    assert db.find_scored_twin(conn, "Nobody", "Nothing") is None


def test_apply_twin_copies_score_and_exclusion(tmp_path):
    conn = make_db(tmp_path)
    db.insert_posting(
        conn,
        posting(
            hash="a",
            score=0,
            excluded=True,
            exclusion_reason="senior-level title",
            rationale="excluded",
        ),
    )
    db.insert_posting(
        conn,
        posting(hash="b", url="https://boards.greenhouse.io/2", location="Remote US"),
    )
    conn.commit()
    target = rows_by_title(conn, "AI Engineer")[1]
    twin = db.find_scored_twin(conn, "TestCo", "AI Engineer")
    apply_twin(conn, target, twin)
    conn.commit()
    got = conn.execute(
        "SELECT score, excluded, exclusion_reason FROM postings WHERE id=?", (target["id"],)
    ).fetchone()
    assert got["score"] == 0
    assert got["excluded"] == 1
    assert got["exclusion_reason"] == "senior-level title"


# --- building requests ---


class FakePage:
    status_code = 200
    text = "<html><body>" + ("Python and SQL pipelines. " * 40) + "</body></html>"

    def raise_for_status(self):
        pass


def test_build_requests_uses_posting_id_as_custom_id(tmp_path):
    conn = make_db(tmp_path)
    db.insert_posting(conn, posting(hash="a"))
    db.insert_posting(
        conn, posting(hash="b", title="Data Analyst", url="https://boards.greenhouse.io/2")
    )
    conn.commit()
    rows = batch.select_rows(conn, 10)
    with patch("sourcer.net.get", return_value=FakePage()):
        reqs, skipped, reused = batch.build_requests(conn, rows, SKILLS)
    assert len(reqs) == 2
    assert {r["custom_id"] for r in reqs} == {str(r["id"]) for r in rows}
    assert reused == 0 and skipped == []


def test_build_requests_reuses_twin_instead_of_sending(tmp_path):
    conn = make_db(tmp_path)
    db.insert_posting(
        conn, posting(hash="a", score=80, rationale="r", score_trace=current_trace(SKILLS))
    )
    db.insert_posting(
        conn,
        posting(hash="b", url="https://boards.greenhouse.io/2", location="Remote US"),
    )
    conn.commit()
    rows = batch.select_rows(conn, 10)
    with patch("sourcer.net.get", return_value=FakePage()):
        reqs, _, reused = batch.build_requests(conn, rows, SKILLS)
    assert reused == 1
    assert reqs == []


def test_dry_run_does_not_write(tmp_path):
    conn = make_db(tmp_path)
    db.insert_posting(conn, posting(hash="a", score=80, rationale="r"))
    db.insert_posting(
        conn,
        posting(hash="b", url="https://boards.greenhouse.io/2", location="Remote US"),
    )
    conn.commit()
    rows = batch.select_rows(conn, 10)
    with patch("sourcer.net.get", return_value=FakePage()):
        batch.build_requests(conn, rows, SKILLS, write=False)
    still_null = conn.execute("SELECT COUNT(*) n FROM postings WHERE score IS NULL").fetchone()["n"]
    assert still_null == 1


def test_unfetchable_page_is_skipped_not_sent(tmp_path):
    conn = make_db(tmp_path)
    db.insert_posting(conn, posting(hash="a"))
    conn.commit()
    rows = batch.select_rows(conn, 10)

    class Tiny(FakePage):
        text = "<html>too short</html>"

    with patch("sourcer.net.get", return_value=Tiny()):
        reqs, skipped, _ = batch.build_requests(conn, rows, SKILLS)
    assert reqs == []
    assert skipped and skipped[0][1] == "page not readable"


# --- applying results ---


def seed_one(tmp_path):
    conn = make_db(tmp_path)
    db.insert_posting(conn, posting(hash="a"))
    conn.commit()
    return conn, conn.execute("SELECT id FROM postings").fetchone()["id"]


def test_apply_result_writes_score(tmp_path):
    conn, rid = seed_one(tmp_path)
    assert batch.apply_result(conn, rid, json.dumps(REPLY)) == "scored 77"
    conn.commit()
    assert conn.execute("SELECT score FROM postings WHERE id=?", (rid,)).fetchone()["score"] == 77


def test_apply_result_accepts_fenced_json(tmp_path):
    conn, rid = seed_one(tmp_path)
    fenced = "```json\n" + json.dumps(REPLY) + "\n```"
    assert batch.apply_result(conn, rid, fenced) == "scored 77"


def test_apply_result_rejects_malformed_json(tmp_path):
    conn, rid = seed_one(tmp_path)
    assert batch.apply_result(conn, rid, "not json").startswith("parse+score:")
    conn.commit()
    assert conn.execute("SELECT score FROM postings WHERE id=?", (rid,)).fetchone()["score"] is None


def test_apply_result_rejects_missing_keys(tmp_path):
    conn, rid = seed_one(tmp_path)
    out = batch.apply_result(conn, rid, json.dumps({"score": 50}))
    assert "missing key" in out


def test_apply_result_applies_exclusion(tmp_path):
    conn = make_db(tmp_path)
    db.insert_posting(conn, posting(hash="a", title="Senior AI Engineer"))
    conn.commit()
    rid = conn.execute("SELECT id FROM postings").fetchone()["id"]
    out = batch.apply_result(conn, rid, json.dumps(REPLY))
    conn.commit()
    assert out.startswith("excluded")
    row = conn.execute("SELECT score, excluded FROM postings WHERE id=?", (rid,)).fetchone()
    assert row["score"] == 0 and row["excluded"] == 1


def test_estimate_cost_is_half_of_standard():
    p = {"company": "C", "title": "T", "location": "L", "url": "u", "description": "d" * 4000}
    system, user = llm.parse_score_prompt(p, SKILLS)
    reqs = [{"custom_id": "1", "params": llm.call_params(system, user)}]
    chars = len(system) + len(user)
    full = chars / 4 / 1e6 * config.PRICE_IN_PER_MTOK + 350 / 1e6 * config.PRICE_OUT_PER_MTOK
    assert batch.estimate_cost(reqs) == round(full * 0.5, 4)


# --- ashby postings resolve via the json api, not html scraping ---


class FakeAshbyBoard:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {
            "jobs": [
                {
                    "id": "abc12345-1111-2222-3333-444455556666",
                    "jobUrl": "https://jobs.ashbyhq.com/acme/abc12345-1111-2222-3333-444455556666",
                    "descriptionPlain": "Build data pipelines in Python and SQL. " * 20,
                }
            ]
        }


ASHBY_URL = "https://jobs.ashbyhq.com/acme/abc12345-1111-2222-3333-444455556666"


def test_ashby_url_resolved_through_json_api():
    score_listings._ashby_boards.clear()
    with patch("sourcer.net.get", return_value=FakeAshbyBoard()) as g:
        text, err = score_listings.fetch_description(ASHBY_URL)
    assert err is None
    assert "Python" in text
    # the board api was called, not the posting page
    assert "api.ashbyhq.com/posting-api/job-board/acme" in g.call_args[0][0]


def test_ashby_board_fetched_once_per_org():
    score_listings._ashby_boards.clear()
    with patch("sourcer.net.get", return_value=FakeAshbyBoard()) as g:
        score_listings.fetch_description(ASHBY_URL)
        score_listings.fetch_description(ASHBY_URL)
    assert g.call_count == 1


def test_ashby_job_absent_from_board_reports_closed():
    score_listings._ashby_boards.clear()
    missing = "https://jobs.ashbyhq.com/acme/99999999-0000-0000-0000-000000000000"
    with patch("sourcer.net.get", return_value=FakeAshbyBoard()):
        text, err = score_listings.fetch_description(missing)
    assert text is None
    assert "not on the ashby board" in err


def test_non_ashby_url_still_scrapes_html():
    score_listings._ashby_boards.clear()
    with patch("sourcer.net.get", return_value=FakePage()) as g:
        text, err = score_listings.fetch_description("https://boards.greenhouse.io/1")
    assert err is None and "Python" in text
    assert g.call_args[0][0] == "https://boards.greenhouse.io/1"
