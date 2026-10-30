"""sourcer init, sourcer facts and sourcer forget, end to end on made up
sources with a fake model and a fake GitHub."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from sourcer import config, llm, main
from sourcer.onboard import backends, cli, github, store
from tests.test_onboard_collectors import FAKE_EMAIL, FAKE_KEY, FakeResponse, make_project

RESUME = (
    "Alex Example\n"
    f"{FAKE_EMAIL}\n\n"
    "Data Intern, Example Corp, June 2024 to August 2024\n"
    "Built a forecasting model in Python and SQL for weekly demand.\n"
)
FACT = {
    "item": "i1",
    "text": "Built a forecasting model in Python and SQL.",
    "skills": ["Python", "SQL"],
    "kind": "job",
    "group": "Data Intern, Example Corp",
    "dates": "June 2024 to August 2024",
    "quote": "Built a forecasting model in Python and SQL for weekly demand",
}


class Script:
    """Scripted answers for Console; records every prompt and line."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.lines: list[str] = []

    def input(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def console(self, **kw: Any) -> cli.Console:
        kw.setdefault("interactive", True)
        return cli.Console(input_fn=self.input, out=self.lines.append, **kw)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def machine(
    onboard_paths: Path, monkeypatch: pytest.MonkeyPatch, fake_client: Any
) -> dict[str, Any]:
    """A machine with the claude CLI, no API key, an empty GitHub account and
    a fake model that finds one fact per call."""
    monkeypatch.setattr(backends.claude_cli, "_find_binary", lambda: "/usr/bin/claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(github.net, "get", lambda url, **kw: FakeResponse(200, []))
    client = fake_client([json.dumps({"facts": [FACT]})] * 5)
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(llm, "get_client", factory)
    (onboard_paths / "cv.txt").write_text(RESUME)
    return {"root": onboard_paths, "client": client, "factory": factory}


def sent_text(client: Any) -> str:
    return json.dumps([c.kwargs for c in client.messages.create.call_args_list], default=str)


def test_init_with_flags_and_yes(machine: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    root = machine["root"]
    monkeypatch.setattr(config, "utc_now", lambda: config.datetime(2026, 10, 30, tzinfo=config.UTC))
    project = make_project(root / "code" / "thesis")
    script = Script()
    argv = ["init", "--yes", "--resume", str(root / "cv.txt"), "--github", "octo-example",
            "--folder", str(project), "--grad", "2027-05", "--work-auth", "sponsorship",
            "--metros", "nyc,sf,mars"]  # fmt: skip
    assert cli.main(argv, console=script.console(yes=True)) == 0
    assert cli.PRIVACY_SHORT in script.text
    sources = json.loads((root / "sources.json").read_text())["sources"]
    assert [s["type"] for s in sources] == ["resume", "github", "folder"]
    profile = json.loads((root / "profile.json").read_text())
    assert profile == {"education": "Bachelor's", "in_progress": True, "grad_year": 2027,
                       "grad_month": 5, "max_years_experience": 2, "us_citizen": False,
                       "needs_sponsorship": True}  # fmt: skip
    env = (root / ".env").read_text()
    assert "SOURCER_LLM=claude-code" in env and "SOURCER_EVIDENCE_SCORING=1" in env
    assert "SOURCER_METROS=nyc,sf" in env
    assert json.loads((root / "skills_vector.json").read_text())["skills"] == []
    assert "on your Claude subscription" in script.text and "Found 1 facts" in script.text
    facts = store.load_facts()["facts"]
    assert [f["status"] for f in facts] == ["pending"]  # --yes never approves a fact
    assert not config.EVIDENCE_PATH.exists()
    # the excerpts that went out hold no secret and no email address
    payload = sent_text(machine["client"])
    assert FAKE_KEY not in payload and FAKE_EMAIL not in payload
    assert "Forecasting demand with PyTorch" in payload
    destinations = {e["destination"] for e in store.read_audit()}
    assert destinations == {"api.github.com", "anthropic via claude CLI"}


def test_dry_run_preview_is_redacted_and_sends_nothing(machine: dict[str, Any]) -> None:
    root = machine["root"]
    project = make_project(root / "thesis")
    script = Script()
    argv = [
        "init",
        "--yes",
        "--dry-run",
        "--folder",
        str(project),
        "--resume",
        str(root / "cv.txt"),
    ]
    assert cli.main(argv, console=script.console(yes=True)) == 0
    preview = config.ONBOARD_PREVIEW_PATH.read_text()
    assert "Forecasting demand with PyTorch" in preview and "Data Intern, Example Corp" in preview
    assert FAKE_KEY not in preview and FAKE_EMAIL not in preview and "[email]" in preview
    machine["factory"].assert_not_called()
    assert all(e["destination"] != "anthropic via claude CLI" for e in store.read_audit())


def test_init_conversation_preview_then_decline(machine: dict[str, Any]) -> None:
    root = machine["root"]
    project = make_project(root / "code" / "thesis")
    (project / "build").mkdir()
    script = Script(
        "details", "",  # privacy screen
        str(root / "cv.txt"), "p", "n",  # resume: preview, then decline the calls
        "",  # no GitHub
        str(project), "e", "build/*", "y", "n",  # folder with an exclude, calls declined
        "",  # no other folder
        "https://example.org/me, not a thing!!",
        "", "", "1", "sf",  # profile
    )  # fmt: skip
    assert cli.main(["init"], console=script.console()) == 0
    assert "Anthropic's Claude, the only model provider" in script.text
    assert "never trains anything on it" in script.text and "until you delete them" in script.text
    assert "Take it back anytime with `sourcer forget --source resume`." in script.text
    gates = [p for p in script.prompts if "Continue?" in p]
    assert gates[0].startswith("First scan reads 1 file, about 1 model call on your Claude")
    assert "(p to preview" in gates[0] and gates[-1].startswith("This scan reads 2 files")
    consent = next(p for p in script.prompts if "Include?" in p)
    assert "2 files to read" in consent and "dependencies skipped" in consent
    assert "skipped: not a folder, link, zip or resume file" in script.text
    assert "Sourcer does not read links yet" in script.text
    sources = store.load_sources()["sources"]
    assert [s["type"] for s in sources] == ["resume", "folder", "link"]
    assert sources[1]["excludes"] == ["build/*"]
    assert config.ONBOARD_PREVIEW_PATH.exists() and "No model calls made." in script.text
    machine["factory"].assert_not_called()
    profile = json.loads((root / "profile.json").read_text())
    assert profile["us_citizen"] is True and "SOURCER_METROS=sf" in (root / ".env").read_text()
    assert script.lines[-1] == cli.CLOSING


def test_init_conversation_shows_value_then_asks_for_more(
    machine: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = {
        "/users/octo-example/repos": FakeResponse(200, [{"name": "demo", "fork": False}]),
        "/repos/octo-example/demo/languages": FakeResponse(200, {"Go": 10}),
    }
    monkeypatch.setattr(
        github.net,
        "get",
        lambda url, **kw: routes.get(
            url.removeprefix(github.API).split("?")[0], FakeResponse(404, {})
        ),
    )
    root = machine["root"]
    script = Script("", str(root / "cv.txt"), "", "octo-example", "", "", "", "", "", "", "", "a")
    assert cli.main(["init"], console=script.console()) == 0
    assert "Found 1 facts from 1 sources. Top skills: Python, SQL." in script.text
    assert "A few of them:\n  Built a forecasting model in Python and SQL." in script.text
    assert "That was 1 public repos." in script.text
    assert "I found 1 facts so far. Adding a project folder usually finds more." in script.text
    assert "Take it back anytime with `sourcer forget --source github`." in script.text
    assert "Built a forecasting model" in config.EVIDENCE_PATH.read_text()
    assert machine["client"].messages.create.call_count == 2


def test_scan_then_approve_all(machine: dict[str, Any]) -> None:
    store.save_sources({"sources": [cli.source_spec("resume", str(machine["root"] / "cv.txt"))]})
    script = Script("y", "a")
    assert cli.scan(script.console(), "auto") == 0
    assert "Found 1 facts from 1 sources. Top skills: Python, SQL." in script.text
    evidence = config.EVIDENCE_PATH.read_text()
    assert "Data Intern, Example Corp (job, June 2024 to August 2024)" in evidence
    assert "- Built a forecasting model in Python and SQL. Skills: Python, SQL." in evidence
    # unchanged sources are not read again
    again = Script()
    assert cli.scan(again.console(), "auto") == 0
    assert "Nothing new to read." in again.text
    assert machine["client"].messages.create.call_count == 1


def test_no_call_without_confirmation(machine: dict[str, Any]) -> None:
    store.save_sources({"sources": [cli.source_spec("resume", str(machine["root"] / "cv.txt"))]})
    script = Script()
    assert cli.scan(script.console(interactive=False), "auto") == 0
    assert "Not a terminal: no model calls" in script.text
    declined = Script("n")
    assert cli.scan(declined.console(), "auto") == 0
    machine["factory"].assert_not_called()
    assert store.read_audit() == []


def test_scan_stops_before_calls(machine: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    script = Script()
    assert cli.scan(script.console(), "auto") == 1  # no sources
    store.save_sources({"sources": [cli.source_spec("resume", str(machine["root"] / "cv.txt"))]})
    monkeypatch.setattr(config, "COST_CAP_USD", 0.0)
    assert cli.scan(script.console(yes=True), "auto") == 0
    assert "SOURCER_COST_CAP is 0" in script.text
    monkeypatch.setattr(backends.claude_cli, "_find_binary", lambda: None)
    assert cli.scan(script.console(yes=True), "auto") == 1
    assert "No model found" in script.text
    machine["factory"].assert_not_called()


def test_api_backend_shows_dollars_and_cap(
    machine: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(config, "COST_CAP_USD", 0.000001)
    store.save_sources({"sources": [cli.source_spec("resume", str(machine["root"] / "cv.txt"))]})
    script = Script("n")
    cli.scan(script.console(), "api")
    gate = next(p for p in script.prompts if "Continue?" in p)
    assert "on your API key" in gate and "$" in gate
    assert "cost cap" in script.text


def test_init_without_a_model_stops(onboard_paths: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backends.claude_cli, "_find_binary", lambda: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    script = Script("")
    assert cli.main(["init"], console=script.console()) == 1
    assert "Claude Code CLI" in script.text and "ANTHROPIC_API_KEY" in script.text
    assert not config.SOURCES_PATH.exists()
    stop = Script("q")
    assert cli.main(["init"], console=stop.console()) == 0
    assert "Nothing was read or saved" in stop.text


def test_init_add_keeps_profile(machine: dict[str, Any]) -> None:
    root = machine["root"]
    (root / "profile.json").write_text(json.dumps({"grad_year": 2028}))
    script = Script()
    argv = ["init", "--add", "--link", "https://example.org/blog", "--link", "ftp://x",
            "--linkedin-export", str(root / "missing.zip")]  # fmt: skip
    assert cli.main(argv, console=script.console(interactive=False)) == 0
    assert json.loads((root / "profile.json").read_text()) == {"grad_year": 2028}
    assert "links start with https://" in script.text and "zip not found" in script.text
    assert [s["type"] for s in store.load_sources()["sources"]] == ["link"]
    assert cli.PRIVACY_SHORT not in script.text
    again = Script()
    assert cli.main(["init"], console=again.console(interactive=False)) == 0
    assert "Using your existing profile.json" in again.text


def pending_facts() -> dict[str, Any]:
    def fact(n: int, source: str) -> dict[str, Any]:
        return {"id": f"f{n}", "text": f"Did thing {n}.", "skills": ["Python"], "kind": "project",
                "group": f"Project {source}", "dates": "2025", "source_type": "folder",
                "source_id": source, "source_ref": ".", "item_id": f"{source}/.#0",
                "quote": f"thing {n} quote", "status": "pending"}  # fmt: skip

    return {"facts": [fact(1, "A"), fact(2, "A"), fact(3, "A"), fact(4, "B"), fact(5, "B"),
                      fact(6, "B")], "scanned": {"A/.#0": "x", "B/.#0": "y"}}  # fmt: skip


def test_review_flow_keeps_hand_written_evidence(onboard_paths: Path) -> None:
    config.EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.EVIDENCE_PATH.write_text("Hand written job\n- Ran the robotics club.\n")
    store.save_facts(pending_facts())
    script = Script("a", "r", "e", "Edited  text here.", "", "s")
    assert cli.main(["facts", "review"], console=script.console()) == 0
    status = {f["id"]: f["status"] for f in store.load_facts()["facts"]}
    assert status == {"f1": "approved", "f2": "rejected", "f3": "approved", "f4": "approved",
                      "f5": "approved", "f6": "approved"}  # fmt: skip
    evidence = config.EVIDENCE_PATH.read_text()
    assert evidence.startswith("Hand written job\n- Ran the robotics club.\n")
    assert store.EVIDENCE_START in evidence and "- Edited text here. Skills: Python." in evidence
    assert "Did thing 2" not in evidence and "Project B (project, 2025)" in evidence
    assert "[1/6] project, Project A, 2025" in script.text
    # a later review rewrites only the onboarding part
    assert cli.main(["facts", "review"], console=Script().console()) == 0
    assert config.EVIDENCE_PATH.read_text() == evidence


def test_review_quit_changes_nothing(onboard_paths: Path) -> None:
    store.save_facts(pending_facts())
    assert cli.main(["facts", "review"], console=Script("q").console()) == 0
    assert {f["status"] for f in store.load_facts()["facts"]} == {"pending"}
    assert not config.EVIDENCE_PATH.exists()


def test_facts_list_and_log(onboard_paths: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main.main(["facts", "list"]) == 0
    assert "No facts yet" in capsys.readouterr().out
    store.save_facts(pending_facts())
    assert main.main(["facts", "list", "--status", "pending"]) == 0
    out = capsys.readouterr().out
    assert "[pending] project, Project A: Did thing 1." in out and "6 pending" in out
    assert main.main(["facts", "log"]) == 0
    assert "No outbound calls recorded." in capsys.readouterr().out
    store.audit("api.github.com", "GET /users/x/repos", ["github:1"], 1234)
    config.ONBOARD_LOG_PATH.open("a").write("not json\n")
    assert main.main(["facts", "log"]) == 0
    assert "api.github.com  GET /users/x/repos  1,234 chars  github:1" in capsys.readouterr().out


def test_facts_scan_uses_saved_backend(
    machine: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOURCER_LLM", "claude-code")
    monkeypatch.setattr(config, "LLM_BACKEND", "claude-code")
    assert cli.default_backend() == "claude-code"
    script = Script()
    assert (
        cli.main(["facts", "scan", "--dry-run", "--cost-cap", "1"], console=script.console()) == 1
    )
    assert "No sources yet" in script.text and config.COST_CAP_USD == 1


def test_forget_one_source_and_everything(onboard_paths: Path) -> None:
    store.save_sources({"sources": [{"id": "A", "type": "folder", "value": "/x", "label": "~/x"},
                                    {"id": "B", "type": "github", "value": "octo"}]})  # fmt: skip
    data = pending_facts()
    for fact in data["facts"]:
        fact["status"] = "approved"
    store.save_facts(data)
    config.EVIDENCE_PATH.write_text("Hand written\n")
    store.write_evidence(data["facts"])
    listing = Script()
    assert cli.main(["forget"], console=listing.console()) == 0
    assert "A  folder ~/x  (3 facts)" in listing.text
    assert cli.main(["forget", "--source", "nope"], console=Script().console()) == 1
    assert cli.main(["forget", "--source", "github"], console=Script("n").console()) == 0
    store.add_source(sources := store.load_sources(), {"id": "C", "type": "github", "value": "b"})
    store.save_sources(sources)
    many = Script()
    assert cli.main(["forget", "--source", "github"], console=many.console()) == 1
    assert "You have 2 github sources" in many.text
    assert cli.main(["forget", "--source", "~/x"], console=Script("n").console()) == 0
    assert len(store.load_facts()["facts"]) == 6
    assert cli.main(["forget", "--source", "~/x"], console=Script("y").console()) == 0
    left = store.load_facts()
    assert {f["source_id"] for f in left["facts"]} == {"B"} and list(left["scanned"]) == ["B/.#0"]
    assert "Did thing 1" not in config.EVIDENCE_PATH.read_text()
    store.audit("api.github.com", "GET", ["B"], 1)
    config.ONBOARD_PREVIEW_PATH.write_text("preview")
    assert cli.main(["forget", "--all"], console=Script("").console()) == 0
    assert config.FACTS_PATH.exists()
    assert cli.main(["forget", "--all", "--yes"]) == 0
    for path in (config.FACTS_PATH, config.SOURCES_PATH, config.ONBOARD_PREVIEW_PATH,
                 config.ONBOARD_LOG_PATH):  # fmt: skip
        assert not path.exists()
    assert config.EVIDENCE_PATH.read_text() == "Hand written\n"


def test_write_evidence_removes_an_empty_file(onboard_paths: Path) -> None:
    approved = [{**pending_facts()["facts"][0], "status": "approved", "skills": []}]
    assert store.write_evidence(approved) == 1
    assert "- Did thing 1.\n" in config.EVIDENCE_PATH.read_text()
    assert config.load_evidence().startswith(store.EVIDENCE_START)
    store.write_evidence([])
    assert not config.EVIDENCE_PATH.exists()
    assert store.split_evidence(f"a\n{store.EVIDENCE_START}\nb") == ("a", "b")


def test_set_env_keeps_other_lines(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=keep-me\nexport SOURCER_LLM=api\n")
    assert cli.set_env({"SOURCER_LLM": "claude-code", "SOURCER_METROS": "nyc"}, env) == [
        "SOURCER_LLM", "SOURCER_METROS",
    ]  # fmt: skip
    assert (
        env.read_text()
        == "ANTHROPIC_API_KEY=keep-me\nSOURCER_LLM=claude-code\nSOURCER_METROS=nyc\n"
    )
    assert cli.set_env({"SOURCER_LLM": "claude-code"}, env) == []
    assert env.stat().st_mode & 0o077 == 0


def test_classify_and_check(tmp_path: Path) -> None:
    (tmp_path / "cv.pdf").write_text("x")
    (tmp_path / "export.zip").write_text("x")
    assert cli.classify("https://example.org") == ("link", "https://example.org")
    assert cli.classify(str(tmp_path)) == ("folder", str(tmp_path.resolve()))
    assert cli.classify(str(tmp_path / "export.zip"))[0] == "linkedin"
    assert cli.classify(str(tmp_path / "cv.pdf"))[0] == "resume"
    assert cli.classify("@octo-example") == ("github", "octo-example")
    assert cli.check_source("resume", str(tmp_path / "cv.doc")) is not None
    assert cli.check_source("github", "bad name") is not None
    assert cli.check_source("folder", str(tmp_path / "nope")) is not None
    assert cli.check_source("folder", str(tmp_path)) is None


def test_profile_defaults_and_bad_dates(onboard_paths: Path) -> None:
    args = cli._parser().parse_args(["init", "--grad", "soon", "--graduated"])
    script = Script()
    profile, metros = cli.build_profile(script.console(yes=True), args)
    assert "is not YYYY-MM" in script.text and profile["in_progress"] is False
    assert metros == "nyc" and "us_citizen" not in profile


def test_privacy_details_name_every_local_file(onboard_paths: Path) -> None:
    text = cli.privacy_details()
    for name in ("sources.json", "data/facts.json", "data/evidence.txt", "data/onboard-log.jsonl",
                 "data/onboard-preview.txt", backends.ANTHROPIC_PRIVACY):  # fmt: skip
        assert name in text
    assert cli.show(Path.home() / "code") == "~/code"
    assert cli.show(Path("/nonexistent-root/x")) == "/nonexistent-root/x"
