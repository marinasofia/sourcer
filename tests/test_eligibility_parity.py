"""Original description eligibility parity, without live model calls."""

from __future__ import annotations

import json

import pytest

from sourcer import config, llm, mcp_server, serve
from sourcer.pipeline import scoring
from sourcer.pipeline import scoring as pipeline_scoring
from tests.conftest import GOOD_PARSE_SCORE, make_client


@pytest.mark.parametrize(
    "text,profile,reason",
    [
        ("No visa sponsorship.", {"needs_sponsorship": True}, "no_sponsorship"),
        ("US citizenship required.", {"us_citizen": False}, "citizenship_required"),
        ("Active security clearance required.", {}, "clearance_required"),
        ("PhD required.", {"education": "Bachelor's"}, "phd_required"),
        (
            "Candidates must graduate between December 2025 and June 2026.",
            {"grad_year": 2027, "grad_month": 5},
            "graduation window",
        ),
        # a summer 2027 internship may be anywhere in the US, so the conflict is abroad
        ("Must be based in London.", {}, "location required outside scope"),
    ],
)
@pytest.mark.parametrize("interface", ["pipeline", "text", "http", "mcp"])
def test_original_eligibility_text_reaches_every_scoring_interface(
    paths, monkeypatch, text, profile, reason, interface
):
    monkeypatch.setattr(config, "load_profile", lambda: profile)
    parsed = {
        **GOOD_PARSE_SCORE,
        "company": "TestCo",
        "url": "https://example.com/jobs/eligibility",
        "employment_type": "full time",
    }
    client = make_client([json.dumps(parsed)])
    monkeypatch.setattr(llm, "get_client", lambda: client)
    if interface == "pipeline":
        result = pipeline_scoring.score_node(
            {"parsed": [{**parsed, "description": text}], "profile": profile, "stats": {}}
        )["scored"][0]
        client.messages.create.assert_not_called()
    elif interface == "http":
        result = serve.score({"posting_text": text, "title": "AI Engineer Intern"})
    elif interface == "mcp":
        result = mcp_server.score_posting(text, "AI Engineer Intern")
    else:
        result = scoring.score_text(text, "AI Engineer Intern")
    assert result["excluded"] is True
    assert reason in result["exclusion_reason"]
    if interface != "pipeline":
        assert client.messages.create.call_count == 1
    if interface in ("text", "mcp"):
        assert result["parsed"]["description"] == text
        assert result["parsed"]["employment_type"] == "intern"


@pytest.mark.parametrize(
    "text",
    ["Sponsorship available.", "PhD preferred.", "Build Python APIs.", "Must be based in Austin."],
)
def test_optional_or_absent_eligibility_does_not_exclude(paths, monkeypatch, text):
    monkeypatch.setattr(config, "load_profile", lambda: {"education": "Bachelor's"})
    client = make_client([json.dumps(GOOD_PARSE_SCORE), '{"score": 88, "rationale": "fit"}'])
    monkeypatch.setattr(llm, "get_client", lambda: client)
    result = scoring.score_text(text, "AI Engineer Intern", "TestCo")
    assert result["excluded"] is False
    assert result["parsed"]["description"] == text
    assert result["parsed"]["company"] == "TestCo"
    assert client.messages.create.call_count == 2
