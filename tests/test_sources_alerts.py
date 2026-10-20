"""New source mappers, listing bypass, and alert selection. No network, no API."""

from unittest.mock import patch

from sourcer import db
from sourcer.alerts import build_body, select_alertable
from sourcer.pipeline.scoring import parse_node, score_node
from sourcer.sources import fetch_ashby, fetch_simplify


class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        pass


def test_ashby_mapper():
    payload = {
        "jobs": [
            {
                "title": " AI Engineer Intern ",
                "location": "New York, NY",
                "secondaryLocations": [{"location": "SF"}],
                "isRemote": True,
                "isListed": "true",
                "jobUrl": "https://jobs.ashbyhq.com/x/1",
                "descriptionPlain": "Build agents. Python required.",
                "publishedAt": "2026-08-20T10:00:00+00:00",
            },
            {"title": "Hidden", "isListed": "false"},
        ]
    }
    with patch("sourcer.net.get", return_value=FakeResp(payload)):
        jobs = fetch_ashby("x")
    assert len(jobs) == 1
    j = jobs[0]
    assert j["title"] == "AI Engineer Intern"
    # isRemote says nothing about country: the mapper appends a bare
    # "Remote" and config.location_in_scope decides from the full list
    assert j["location"] == "New York, NY; SF; Remote"
    assert j["posted_at"] == "2026-08-20"
    assert "Python" in j["description"]


def test_simplify_mapper_filters_and_maps():
    payload = [
        {
            "active": True,
            "is_visible": True,
            "category": "AI/ML/Data",
            "company_name": "Acme",
            "title": "ML Intern",
            "locations": ["NYC"],
            "terms": ["Summer 2027"],
            "url": "https://a.co/1",
            "date_posted": 1787000000,
        },
        {
            "active": False,
            "is_visible": True,
            "category": "AI/ML/Data",
            "company_name": "Dead",
            "title": "x",
            "locations": [],
            "url": "u",
        },
        {
            "active": True,
            "is_visible": True,
            "category": "Hardware",
            "company_name": "HW",
            "title": "x",
            "locations": [],
            "url": "u",
        },
    ]
    with patch("sourcer.net.get", return_value=FakeResp(payload)):
        out = fetch_simplify()
    # two list fetches (intern + new grad) both return the same payload here
    interns = [o for o in out if o["employment_type"] == "intern"]
    grads = [o for o in out if o["employment_type"] == "full time"]
    assert len(interns) == 1 and len(grads) == 1
    assert interns[0]["company"] == "Acme"
    assert interns[0]["source"] == "simplify"
    assert interns[0]["start_date"] == "Summer 2027"
    assert grads[0]["start_date"] == "new grad Summer 2027"
    assert interns[0]["description"] == ""


def test_listing_bypasses_llm_and_scores_null():
    listing = {
        "source": "simplify",
        "company": "Acme",
        "title": "ML Intern",
        "url": "u",
        "location": "NYC",
        "hash": "h",
        "employment_type": "intern",
        "start_date": "Summer 2027",
        "posted_at": "2026-08-20",
    }
    state = {
        "new_postings": [listing],
        "stats": {},
        "usage": None,
        "client": None,
        "skills": {"skills": []},
    }
    state = parse_node(state)  # usage/client never touched for listings
    assert state["parsed"][0]["source"] == "simplify"
    state = score_node(state)
    p = state["scored"][0]
    assert p["score"] is None and p["excluded"] is False


def test_alert_selection_and_flood_cap(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    base = {"url": "u", "excluded": False}
    rows = [
        {"hash": "h1", "company": "A", "title": "Sr thing", "score": 90, **base},
        {
            "hash": "h2",
            "company": "B",
            "title": "ML Intern",
            "score": None,
            "employment_type": "intern",
            "source": "simplify",
            **base,
        },
        {
            "hash": "h3",
            "company": "C",
            "title": "NG",
            "score": 40,
            "start_date": "2027 new grad",
            **base,
        },
        {"hash": "h4", "company": "D", "title": "Mid role", "score": 60, **base},
    ]
    for r in rows:
        db.insert_posting(conn, r)
    conn.commit()
    sel = select_alertable(conn)
    names = {p["company"] for p in sel}
    assert names == {"A", "B", "C"}  # 90+, intern, new grad; not the 60
    body = build_body(sel)
    assert "ML Intern" in body and "Mid role" not in body
