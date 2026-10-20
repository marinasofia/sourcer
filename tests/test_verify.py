"""Verification classifier tests: no network."""

from unittest.mock import MagicMock, patch

import requests

from sourcer.apply.verify import WORKDAY_RE, check_one, title_tokens


def row(**over):
    base = {
        "id": 1,
        "url": "https://job-boards.greenhouse.io/x/jobs/1",
        "title": "Machine Learning Platform Intern",
        "company": "Acme",
    }
    base.update(over)
    return base


def resp(status=200, text="", url=None):
    r = MagicMock()
    r.status_code = status
    r.ok = 200 <= status < 300
    r.text = text
    r.url = url or "https://job-boards.greenhouse.io/x/jobs/1"
    return r


def test_dead_on_404():
    with patch("sourcer.net.get", return_value=resp(404)):
        assert check_one(row())[1] == "dead"


def test_closed_text_detected():
    page = "<html>This job is no longer accepting applications</html>"
    with patch("sourcer.net.get", return_value=resp(200, page)):
        assert check_one(row())[1] == "closed"


def test_verified_when_title_present():
    page = "<h1>Machine Learning Platform Intern</h1> apply now"
    with patch("sourcer.net.get", return_value=resp(200, page)):
        assert check_one(row())[1] == "verified"


def test_live_on_ats_host_without_title():
    page = "<div id=app></div>"  # JS shell
    with patch("sourcer.net.get", return_value=resp(200, page)):
        assert check_one(row())[1] == "live"


def test_live_when_company_in_domain():
    page = "<div id=app></div>"
    r = resp(200, page, url="https://careers.acme.com/jobs/1")
    with patch("sourcer.net.get", return_value=r):
        assert check_one(row(url="https://careers.acme.com/jobs/1"))[1] == "live"


def test_unknown_on_network_error():
    with patch("sourcer.net.get", side_effect=requests.ConnectionError()):
        assert check_one(row())[1] == "unknown"


def test_workday_url_parses():
    m = WORKDAY_RE.match(
        "https://tenant.wd5.myworkdayjobs.com/careers/job/City-Name/Role-Title_R-123"
    )
    assert m and m.group(1) == "tenant" and m.group(4) == "Role-Title_R-123"


def test_title_tokens_skip_generic():
    toks = title_tokens("Senior Software Engineer Intern - Ads Infrastructure")
    assert "intern" not in toks and "software" not in toks
    assert "infrastructure" in toks or "ads" not in toks  # 4+ letter rule
