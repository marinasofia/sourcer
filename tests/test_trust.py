from __future__ import annotations

from pathlib import Path

import pytest

from sourcer import trust


@pytest.mark.parametrize(
    ("company", "url", "tier"),
    [
        ("Anthropic", "https://job-boards.greenhouse.io/anthropic/jobs/1", "official"),
        ("Scale AI", "https://boards.greenhouse.io/embed/job_app?for=scaleai&token=1", "official"),
        ("Capital One", "https://capitalone.wd12.myworkdayjobs.com/Capital_One/job/x", "official"),
        ("Northrop Grumman", "https://ngc.wd1.myworkdayjobs.com/x", "official"),
        (
            "Johns Hopkins Applied Physics Laboratory",
            "https://careers.jhuapl.edu/jobs/1",
            "official",
        ),
        ("TikTok", "https://lifeattiktok.com/search/1", "official"),
        ("Lyft", "https://app.careerpuck.com/job-board/lyft/job/1", "official"),
        ("Oracle", "https://egug.fa.us2.oraclecloud.com/hcmUI/job/1", "official"),
        # enterprise systems: an abbreviated tenant is still the company's
        ("Fidelity Investments", "https://fmr.wd1.myworkdayjobs.com/x", "official"),
        # self serve boards: a different name is a red flag
        ("Felix Magazine", "https://jobs.smartrecruiters.com/LuxeMediaLLC/1", "unverified"),
        ("AlphaHire", "https://apply.workable.com/alphax/j/1", "unverified"),
        ("Acme", "https://www.linkedin.com/jobs/view/1", "platform"),
        ("Acme", "https://app.joinhandshake.com/jobs/1", "platform"),
        ("Acme", "https://jobs.example-careers.net/acme", "unverified"),
        ("Acme", "", "unverified"),
    ],
)
def test_link_tiers(company: str, url: str, tier: str) -> None:
    assert trust.classify(company, url).tier == tier


def test_known_domain_counts_as_official() -> None:
    t = trust.classify(
        "Altice USA", "https://www.optimumcareers.com/job/1", domain="optimumcareers.com"
    )
    assert t.tier == "official"


@pytest.mark.parametrize(
    "text",
    [
        "Selected candidates pay a one time training fee of $500.",
        "Applicants must send a deposit before onboarding.",
        "Email your resume to hiring.team@gmail.com",
        "Interview will be conducted via Telegram.",
        "We are hiring on behalf of our client, a leading bank.",
        "We offer training and placement with our clients. C2C welcome.",
    ],
)
def test_red_flags_hold_back_official_links(text: str) -> None:
    t = trust.classify("Anthropic", "https://job-boards.greenhouse.io/anthropic/jobs/1", text)
    assert t.tier == "unverified"


@pytest.mark.parametrize(
    "text",
    [
        "Build the systems that let customers send money across borders.",
        "We do not accept unsolicited resumes from staffing agencies.",
        "Work on the WhatsApp Business messaging platform.",
        "Gift cards and payments are our core product.",
    ],
)
def test_ordinary_product_language_is_not_a_red_flag(text: str) -> None:
    t = trust.classify("Anthropic", "https://job-boards.greenhouse.io/anthropic/jobs/1", text)
    assert t.tier == "official"


def test_blocklist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "blocklist.txt"
    path.write_text("# ruled out\nMapjects  # bulk intern postings\n")
    monkeypatch.setattr(trust, "BLOCKLIST_PATH", path)
    trust.blocklist.cache_clear()
    try:
        held = trust.classify("Mapjects, Inc.", "https://jobs.smartrecruiters.com/Mapjectscom/1")
        assert held == trust.Trust("unverified", "on your blocklist")
    finally:
        trust.blocklist.cache_clear()
