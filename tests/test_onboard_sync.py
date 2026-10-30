"""sourcer sync: only new or changed items are read, the same gate and log
apply, and a quiet run never sends without --yes. Made up sources only."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from sourcer import config, llm
from sourcer.onboard import backends, cli, github, store, sync
from tests.conftest import make_client
from tests.test_onboard_cli import FACT, RESUME, Script
from tests.test_onboard_collectors import FakeResponse

NEW_LINE = "Shipped a churn dashboard in Tableau used by the sales team.\n"
NEW_FACT = {
    "item": "i1",
    "text": "Shipped a churn dashboard in Tableau.",
    "skills": ["Tableau"],
    "kind": "job",
    "group": "Data Intern, Example Corp",
    "dates": "2024",
    "quote": "Shipped a churn dashboard in Tableau used by the sales team",
}


@pytest.fixture
def box(onboard_paths: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The claude CLI, a resume source and a model that answers with ``replies``."""
    monkeypatch.setattr(backends.claude_cli, "_find_binary", lambda: "/usr/bin/claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resume = onboard_paths / "cv.txt"
    resume.write_text(RESUME)
    data = store.load_sources()
    store.add_source(data, cli.source_spec("resume", str(resume), label="cv.txt"))
    store.save_sources(data)
    state: dict[str, Any] = {"root": onboard_paths, "resume": resume, "client": None}

    def model(*replies: dict[str, Any]) -> MagicMock:
        client = make_client([json.dumps({"facts": list(r)}) for r in replies])
        monkeypatch.setattr(llm, "get_client", MagicMock(return_value=client))
        state["client"] = client
        return client

    state["model"] = model
    return state


def run(*argv: str, script: Script | None = None, **kw: Any) -> tuple[int, Script]:
    script = script or Script()
    return cli.main(["sync", *argv], console=script.console(**kw)), script


def test_sync_reads_only_what_changed(box: dict[str, Any]) -> None:
    client = box["model"]([FACT])
    code, script = run("--yes", yes=True)
    assert code == 0 and client.messages.create.call_count == 1
    assert "This sync reads 1 file" in script.text
    assert script.lines[-1] == "1 new fact waiting. Run `sourcer facts review`."
    seen = store.load_sources()["sources"][0]["seen"]
    assert len(seen["file"]) == 64  # the resume's sha256
    # unchanged: the file is not read again and no call is made
    code, script = run("--yes", yes=True)
    assert code == 0 and client.messages.create.call_count == 1
    assert "unchanged since the last scan" in script.text and "Nothing new to read." in script.text
    assert script.lines[-1] == "No new facts; 1 still waiting. Run `sourcer facts review`."
    # a changed resume: only the new fact is queued, the old one is not repeated
    box["resume"].write_text(RESUME + "\n" + NEW_LINE)
    client = box["model"]([FACT, NEW_FACT])
    code, script = run("--yes", yes=True)
    assert code == 0 and client.messages.create.call_count == 1
    facts = store.load_facts()["facts"]
    assert [f["text"] for f in facts] == [FACT["text"], NEW_FACT["text"]]
    assert script.lines[-1] == "1 new fact waiting. Run `sourcer facts review`."
    assert {e["destination"] for e in store.read_audit()} == {"anthropic via claude CLI"}


def test_sync_asks_once_and_a_no_reads_again_later(box: dict[str, Any]) -> None:
    client = box["model"]([FACT])
    code, script = run(script=Script("n"))
    assert code == 0 and client.messages.create.call_count == 0
    assert "Continue? [Y/n]" in script.prompts[0] and "This sync reads" in script.prompts[0]
    assert "seen" not in store.load_sources()["sources"][0]  # not marked as read
    code, script = run(script=Script("p", "y"))
    assert client.messages.create.call_count == 1
    assert "Preview written to" in script.text
    assert store.load_sources()["synced_at"]


def test_quiet_without_yes_sends_and_queues_nothing(
    box: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    client = box["model"]([FACT])
    code, script = run("--quiet", interactive=False)
    assert code == 0 and client.messages.create.call_count == 0
    assert script.lines == [
        "Sync found changes that need 1 model call; nothing was sent. Run `sourcer sync` to "
        "review them, or schedule it with --yes."
    ]
    assert store.load_facts()["facts"] == [] and not store.read_audit()
    # with --yes the scheduled run reads it, still in one line
    code, script = run("--quiet", "--yes", yes=True, interactive=False)
    assert client.messages.create.call_count == 1
    assert script.lines == ["1 new fact waiting. Run `sourcer facts review`."]
    assert [f["status"] for f in store.load_facts()["facts"]] == ["pending"]  # never approved


def test_quiet_dry_run_and_no_model(box: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    box["model"]()
    code, script = run("--quiet", "--dry-run", interactive=False)
    assert code == 0 and script.lines[-1].startswith("Dry run:")
    assert config.ONBOARD_PREVIEW_PATH.exists()
    monkeypatch.setattr(backends.claude_cli, "_find_binary", lambda: None)
    code, script = run("--quiet", interactive=False)
    assert code == 1 and script.lines == [
        "Sync stopped: no model backend found. Run `sourcer sync` to see why."
    ]
    code, script = run()
    assert code == 1 and script.lines[-1] == "Sync stopped."


def test_sync_without_sources(onboard_paths: Path) -> None:
    code, script = run()
    assert code == 1 and "No sources yet" in script.text


def test_sync_skips_github_repos_with_nothing_pushed(
    box: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    data = store.load_sources()
    data["sources"] = [cli.source_spec("github", "octo-example", label="octo-example")]
    store.save_sources(data)
    repos = [
        {"name": n, "pushed_at": f"2026-0{i}-01T00:00:00Z", "fork": False,
         "html_url": f"https://github.com/octo-example/{n}", "description": f"{n} project"}
        for i, n in ((1, "alpha"), (2, "beta"))
    ]  # fmt: skip
    fetched: list[str] = []

    def get(url: str, **kw: Any) -> FakeResponse:
        path = url.removeprefix(github.API).split("?")[0]
        fetched.append(path)
        if path == "/users/octo-example/repos":
            return FakeResponse(200, repos)
        if path.endswith("/languages"):
            return FakeResponse(200, {"Python": 10})
        return FakeResponse(200, "README text")

    monkeypatch.setattr(github.net, "get", get)
    box["model"]([], [])
    assert run("--yes", yes=True)[0] == 0
    assert sum(p.endswith("/readme") for p in fetched) == 2
    fetched.clear()
    repos[1]["pushed_at"] = "2026-10-01T00:00:00Z"
    client = box["model"]([])
    code, script = run("--yes", yes=True)
    assert code == 0 and client.messages.create.call_count == 1
    assert [p for p in fetched if p.endswith("/readme")] == ["/repos/octo-example/beta/readme"]
    assert "1 repos unchanged since the last scan" in script.text


def test_linkedin_export_unchanged_is_not_read(box: dict[str, Any]) -> None:
    export = box["root"] / "export.zip"
    with zipfile.ZipFile(export, "w") as zf:
        zf.writestr("Skills.csv", "Name\nPython\n")
    data = store.load_sources()
    data["sources"] = [cli.source_spec("linkedin", str(export), label="export.zip")]
    store.save_sources(data)
    box["model"]([])
    assert run("--yes", yes=True)[0] == 0
    _, script = run("--yes", yes=True)
    assert "unchanged since the last scan" in script.text and "Nothing new" in script.text


def test_review_reminder_for_the_digest(onboard_paths: Path) -> None:
    assert store.review_reminder() == ""
    facts = store.load_facts()
    store.merge_facts(facts, [{"id": "a", "source_id": "s", "quote": "x", "text": "t"}])
    store.save_facts(facts)
    assert store.review_reminder() == "1 fact waiting for your review. Run `sourcer facts review`."
    facts["facts"][0]["status"] = "approved"
    store.save_facts(facts)
    assert store.review_reminder() == ""


def test_merge_facts_skips_the_same_quote_from_the_same_source() -> None:
    data: dict[str, Any] = {"facts": []}
    one = {"id": "a", "source_id": "s1", "quote": "Built  It", "text": "t"}
    assert store.merge_facts(data, [one]) == 1
    assert store.merge_facts(data, [{**one, "id": "b", "quote": "built it"}]) == 0
    assert store.merge_facts(data, [{**one, "id": "c", "source_id": "s2"}]) == 1


def test_end_line() -> None:
    assert sync.end_line(3, 3) == "3 new facts waiting. Run `sourcer facts review`."
    assert sync.end_line(0, 0) == "No new facts. Everything is up to date."
