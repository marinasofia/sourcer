"""Parse function tests with a mocked Anthropic client."""

import json
from unittest.mock import MagicMock

from sourcer import llm


def make_client(replies):
    """A fake client whose messages.create returns the given texts in order."""
    client = MagicMock()
    responses = []
    for text in replies:
        r = MagicMock()
        r.content = [MagicMock(type="text", text=text)]
        r.usage.cache_creation_input_tokens = 0
        r.usage.cache_read_input_tokens = 0
        r.stop_reason = "end_turn"
        r.usage.input_tokens = 100
        r.usage.output_tokens = 50
        responses.append(r)
    client.messages.create.side_effect = responses
    return client


MOCK_POSTING = {
    "company": "TestCo",
    "title": "AI Engineer Intern",
    "location": "New York, NY",
    "url": "https://example.com/job/1",
    "description": "Summer 2027 internship. Requires Python and LangGraph.",
}

GOOD_PARSE = json.dumps(
    {
        "title": "AI Engineer Intern",
        "location": "New York, NY",
        "requirements": ["Python", "LangGraph"],
        "seniority": "intern",
        "tech_stack": ["Python", "LangGraph"],
        "employment_type": "intern",
        "start_date": "summer 2027",
        "years_experience_min": None,
        "work_mode": "onsite",
    }
)


def test_parse_valid_json():
    client = make_client([GOOD_PARSE])
    usage = llm.Usage()
    data, err = llm.parse_posting(client, MOCK_POSTING, usage)
    assert err is None
    assert data["employment_type"] == "intern"
    assert data["start_date"] == "summer 2027"
    assert data["years_experience_min"] is None
    assert client.messages.create.call_count == 1
    assert usage.prompt_tokens == 100


def test_parse_retries_once_then_succeeds():
    client = make_client(["not json at all", GOOD_PARSE])
    data, err = llm.parse_posting(client, MOCK_POSTING, llm.Usage())
    assert err is None
    assert data["seniority"] == "intern"
    assert client.messages.create.call_count == 2  # exactly one retry


def test_parse_fails_after_one_retry_no_infinite_loop():
    client = make_client(["garbage", "still garbage"])
    data, err = llm.parse_posting(client, MOCK_POSTING, llm.Usage())
    assert data is None
    assert err is not None
    assert client.messages.create.call_count == 2  # never a third call


def test_parse_rejects_missing_key():
    incomplete = json.dumps({"title": "X"})
    client = make_client([incomplete, incomplete])
    data, err = llm.parse_posting(client, MOCK_POSTING, llm.Usage())
    assert data is None
    assert "missing key" in err


def test_parse_strips_markdown_fences():
    fenced = "```json\n" + GOOD_PARSE + "\n```"
    client = make_client([fenced])
    data, err = llm.parse_posting(client, MOCK_POSTING, llm.Usage())
    assert err is None
    assert data["title"] == "AI Engineer Intern"
