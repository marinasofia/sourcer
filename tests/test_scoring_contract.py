"""Budget and model-output contracts across application boundaries."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sourcer import config, evaluate, llm, mcp_server, serve
from sourcer.pipeline import scoring
from tests.conftest import GOOD_PARSE_SCORE, make_client


@pytest.mark.parametrize("value", [-1, 101, 0.5, True, math.nan, math.inf])
def test_invalid_scores_are_rejected(value):
    data, error = llm.extract_json(
        json.dumps({"score": value, "rationale": "x"}), llm.SCORE_REQUIRED
    )
    assert data is None and error


@pytest.mark.parametrize(
    "field,value",
    [
        ("years_experience_min", True),
        ("years_experience_min", -1),
        ("years_experience_min", math.inf),
        ("requirements", [3]),
        ("tech_stack", [None]),
        ("seniority", "executive"),
        ("employment_type", "other"),
        ("work_mode", "elsewhere"),
    ],
)
def test_invalid_extracted_fields_are_rejected(field, value):
    posting = {**GOOD_PARSE_SCORE, field: value}
    assert llm.extract_json(json.dumps(posting), llm.PARSE_REQUIRED)[0] is None


def test_unrecognized_fields_do_not_cross_model_boundary():
    data, error = llm.extract_json(
        json.dumps({**GOOD_PARSE_SCORE, "status": "applied"}), llm.PARSE_SCORE_REQUIRED
    )
    assert error is None and data == GOOD_PARSE_SCORE


@pytest.mark.parametrize("cap", [0, -1, math.nan, math.inf])
def test_both_interfaces_refuse_disabled_budget_before_client_creation(monkeypatch, cap):
    monkeypatch.setattr(config, "COST_CAP_USD", cap)
    client_factory = Mock()
    monkeypatch.setattr(llm, "get_client", client_factory)
    assert "budget" in serve.score({"posting_text": "x"})["error"]
    assert "budget" in mcp_server.score_posting("x")["error"]
    client_factory.assert_not_called()


def test_retry_admission_uses_unrounded_cost(monkeypatch):
    monkeypatch.setattr(config, "COST_CAP_USD", 0.00001)
    monkeypatch.setattr(config, "PRICE_IN_PER_MTOK", 0.1)
    monkeypatch.setattr(config, "PRICE_OUT_PER_MTOK", 0.1)
    monkeypatch.setattr(llm, "MODEL_PRICES", {})  # price at the configured rates
    client = make_client(["invalid"])
    usage = llm.Usage()
    data, error = llm.json_call(client, "s", "u", llm.SCORE_REQUIRED, usage)
    assert usage.est_cost_usd == 0.0 and usage.raw_cost_usd > config.COST_CAP_USD
    assert data is None and "budget" in error
    assert client.messages.create.call_count == 1


def test_budget_is_checked_between_parsing_and_scoring(paths, monkeypatch):
    monkeypatch.setattr(config, "COST_CAP_USD", 0.000001)
    client = make_client([json.dumps(GOOD_PARSE_SCORE)])
    monkeypatch.setattr(llm, "get_client", lambda: client)
    assert "budget" in scoring.score_text("Intern posting")["error"]
    assert client.messages.create.call_count == 1


@pytest.mark.parametrize("reason", ["max_tokens", "refusal"])
def test_incomplete_model_responses_are_not_accepted(reason):
    client = Mock()
    client.messages.create.return_value = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        content=[SimpleNamespace(type="text", text=json.dumps(GOOD_PARSE_SCORE))],
        stop_reason=reason,
    )
    data, error = llm.json_call(client, "s", "u", llm.SCORE_REQUIRED, llm.Usage())
    assert data is None and "truncated or refused" in error
    assert client.messages.create.call_count == 1


def test_model_response_joins_text_blocks_and_ignores_other_blocks():
    client = Mock()
    client.messages.create.return_value = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        stop_reason="end_turn",
        content=[
            SimpleNamespace(type="thinking"),
            SimpleNamespace(type="text", text='{"score":'),
            SimpleNamespace(type="text", text='80, "rationale":"ok"}'),
        ],
    )
    assert llm.json_call(client, "s", "u", llm.SCORE_REQUIRED, llm.Usage())[0]["score"] == 80


@pytest.mark.parametrize(
    "text,title,company",
    [
        ("", "", ""),
        (" ", "", ""),
        (1, "", ""),
        ("x", None, ""),
        ("x", "", []),
        ("x" * (config.MAX_DESCRIPTION_CHARS + 1), "", ""),
    ],
)
def test_invalid_service_inputs_fail_before_model(text, title, company, monkeypatch):
    factory = Mock()
    monkeypatch.setattr(llm, "get_client", factory)
    with pytest.raises(ValueError):
        scoring.score_text(text, title, company)
    factory.assert_not_called()


def test_default_evaluation_uses_fixture_policy(monkeypatch, capsys):
    monkeypatch.setattr(config, "load_profile", lambda *_: {"max_years_experience": 0})
    assert evaluate.main([]) == 0
    assert json.loads(capsys.readouterr().out)["rules"]["mismatches"] == []


def test_dotenv_loaded_before_settings_and_process_environment_wins(tmp_path):
    (tmp_path / ".env").write_text("SOURCER_COST_CAP=0\nSOURCER_MODEL=fixture-model\n")
    env = {**os.environ, "SOURCER_ROOT": str(tmp_path), "SOURCER_MODEL": "process-model"}
    env.pop("SOURCER_COST_CAP", None)
    env.pop("PYTHON_DOTENV_DISABLED", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from sourcer import config; print(config.COST_CAP_USD, config.MODEL)",
        ],
        env=env,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "0.0 process-model"


def test_usage_is_priced_by_the_model_that_ran():
    def resp(i, o):
        r = Mock()
        r.usage.input_tokens, r.usage.output_tokens = i, o
        r.usage.cache_creation_input_tokens = r.usage.cache_read_input_tokens = 0
        return r

    usage = llm.Usage()
    usage.add(resp(1_000_000, 0), "claude-sonnet-5-5")
    usage.add(resp(0, 1_000_000), "claude-haiku-4-5-20251001")
    assert usage.raw_cost_usd == 2.00 + 5.00
    assert llm.model_prices("claude-opus-5-5") == (4.00, 20.00)


@pytest.mark.xfail(
    reason="config.EXTRACT_MODEL (claude-haiku-5-5) has no MODEL_PRICES entry, so the cost "
    "cap prices extraction calls at the fallback SOURCER_PRICE_IN/OUT rates; add its list "
    "price to llm.MODEL_PRICES",
    strict=True,
)
def test_every_default_model_has_a_price():
    for model in (config.MODEL, config.EXTRACT_MODEL):
        assert any(model.startswith(name) for name in llm.MODEL_PRICES), model


def test_run_minutes_stops_new_calls(monkeypatch):
    client = make_client([json.dumps(GOOD_PARSE_SCORE)])
    monkeypatch.setattr(config, "RUN_MINUTES", 0.0)
    data, error = llm.json_call(client, "s", "u", llm.SCORE_REQUIRED, llm.Usage())
    assert data is None and "budget" in error
    client.messages.create.assert_not_called()
    monkeypatch.setattr(config, "RUN_MINUTES", math.inf)
    assert llm.Usage().can_call()
