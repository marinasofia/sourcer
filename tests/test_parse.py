"""parse_posting, kept for callers outside the package, reads with the live
extraction. A mocked Anthropic client stands in for the model."""

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
        "employment_type": "intern",
        "employment_quote": "Summer 2027 internship",
        "start_date": "Summer 2027",
        "start_quote": "Summer 2027 internship",
        "work_mode": "onsite",
        "work_mode_quote": "",
        "years_experience_min": None,
        "years_quote": "",
        "seniority": "intern",
        "seniority_quote": "Summer 2027 internship",
        "requirements": ["Python", "LangGraph"],
        "tech_stack": ["Python", "LangGraph"],
    }
)


def test_parse_valid_json():
    client = make_client([GOOD_PARSE])
    usage = llm.Usage()
    data, err = llm.parse_posting(client, MOCK_POSTING, usage)
    assert err is None
    assert data["employment_type"] == "intern"
    assert data["start_date"] == "Summer 2027"
    assert data["years_experience_min"] is None
    # a fact without the posting's words is unknown, never trusted
    assert data["work_mode"] == "unspecified" and data["checked"]["work_mode"] is False
    sent = client.messages.create.call_args.kwargs["system"][0]["text"]
    assert sent.startswith(llm.EXTRACT_SYSTEM)
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
