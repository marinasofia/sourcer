"""Entry point tests: state construction, run, and the CLI wrapper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sourcer import config, llm, main


def test_build_state_reads_configured_inputs(paths, monkeypatch):
    monkeypatch.setattr(config, "COST_CAP_USD", 1.0)
    fake = MagicMock()
    with patch("sourcer.llm.get_client", return_value=fake) as get_client:
        state = main.build_state()
    get_client.assert_called_once()
    assert state["client"] is fake
    assert state["companies"][0]["company"] == "TestCo"
    assert state["skills"]["skills"][0]["skill"] == "Python"
    assert state["profile"]["grad_year"] == 2027
    assert isinstance(state["usage"], llm.Usage)
    assert state["stats"] == {}
    assert (paths / "data" / "sourcer.db").exists()


def test_build_state_with_zero_cap_creates_no_client(paths, monkeypatch):
    monkeypatch.setattr(config, "COST_CAP_USD", 0.0)
    with patch("sourcer.llm.get_client") as get_client:
        state = main.build_state()
    get_client.assert_not_called()
    assert state["client"] is None


def test_build_state_uses_injected_client(paths, monkeypatch):
    monkeypatch.setattr(config, "COST_CAP_USD", 1.0)
    injected = MagicMock()
    with patch("sourcer.llm.get_client") as get_client:
        state = main.build_state(injected)
    get_client.assert_not_called()
    assert state["client"] is injected


def test_run_invokes_graph_and_logs_run(paths):
    conn = MagicMock()
    state = {"conn": conn, "stats": {}}
    final = {"conn": conn, "stats": {"new_postings": 1}}
    compiled = MagicMock()
    compiled.invoke.return_value = final
    with (
        patch("sourcer.main.build_graph", return_value=compiled),
        patch("sourcer.main.db.insert_run_log") as insert,
    ):
        out = main.run(state)  # type: ignore[arg-type]
    compiled.invoke.assert_called_once_with(state)
    assert out is final
    assert "duration_s" in out["stats"]
    insert.assert_called_once_with(conn, final["stats"])


def test_run_builds_state_when_none_given(paths):
    built = {"conn": MagicMock(), "stats": {}}
    compiled = MagicMock()
    compiled.invoke.side_effect = lambda s: s
    with (
        patch("sourcer.main.build_state", return_value=built),
        patch("sourcer.main.build_graph", return_value=compiled),
        patch("sourcer.main.db.insert_run_log"),
    ):
        out = main.run()
    assert out is built


def test_main_prints_digest_and_reports_source_failures(capsys):
    final = {
        "scored": [],
        "stats": {"source_failures": ["TestCo: down"], "companies_covered": 0},
    }
    with patch("sourcer.main.run", return_value=final), patch("sourcer.main.load_dotenv"):
        assert main.main([]) == 0
    out, err = capsys.readouterr()
    assert "SOURCER DIGEST" in out
    assert "source fetch failures: ['TestCo: down']" in err


def test_main_quiet_on_stderr_without_failures(capsys):
    final = {"scored": [], "stats": {}}
    with patch("sourcer.main.run", return_value=final), patch("sourcer.main.load_dotenv"):
        assert main.main([]) == 0
    _, err = capsys.readouterr()
    assert err == ""


def test_main_cost_cap_flag_overrides_config(monkeypatch, capsys) -> None:
    seen = {}

    def fake_run() -> dict:
        seen["cap"] = config.COST_CAP_USD
        return {"stats": {}, "scored": []}

    monkeypatch.setattr(main, "run", fake_run)
    assert main.main(["--cost-cap", "0.25"]) == 0
    assert seen["cap"] == 0.25
    assert "SOURCER DIGEST" in capsys.readouterr().out


def test_main_version_flag(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main.main(["--version"])
    assert exc.value.code == 0
    assert "sourcer 0." in capsys.readouterr().out
