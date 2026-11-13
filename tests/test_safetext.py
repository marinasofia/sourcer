"""Text from boards is stripped of control characters where it enters."""

from __future__ import annotations

from sourcer import digest, sources, track
from sourcer.safetext import multi_line, one_line

ESC = chr(0x1B)
HOSTILE_TITLE = f"Intern{ESC}]8;;https://evil.example{chr(7)}click\n 1. [100] FAKE - forged row"


def test_one_line_and_multi_line() -> None:
    assert one_line(f"a{ESC}[2Jb\nc") == "a [2Jb c"
    assert multi_line(f"line one{ESC}[H\r\nline two\tend") == "line one[H\nline two\tend"


def test_fetched_postings_are_stripped_on_the_way_in(monkeypatch) -> None:
    def fake_fetch(company: dict) -> list[dict]:
        del company
        return [
            {
                "title": HOSTILE_TITLE,
                "company": f"Acme{ESC}[2J",
                "location": "NYC",
                "description": f"Para one{ESC}[31m\nPara two",
                "tags": [f"x{ESC}y"],
            }
        ]

    monkeypatch.setitem(sources.FETCHERS, "fake", fake_fetch)
    [p] = sources.fetch_postings({"company": "Acme", "ats": "fake"})
    assert ESC not in str(p) and "\n" not in p["title"]
    assert p["description"] == "Para one[31m\nPara two"
    assert p["tags"] == ["x y"]


def test_digest_and_tracker_never_print_escapes_from_stored_rows() -> None:
    p = {
        "score": 99,
        "company": f"Acme{ESC}[2J{ESC}[H",
        "title": HOSTILE_TITLE,
        "location": "NYC",
        "employment_type": "intern",
        "start_date": None,
        "rationale": "ok",
        "url": "https://jobs.example/1",
    }
    out = digest.render_digest({"scored": [p], "stats": {}})
    assert ESC not in out and "\n 1. [100] FAKE" not in out
    row = {**p, "id": 1, "status": "new"}
    line = track.format_row(row)
    assert ESC not in line and "\n" not in line
