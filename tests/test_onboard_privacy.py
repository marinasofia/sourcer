"""Onboarding redaction: hostile text must stay cheap, and secrets stay masked."""

from __future__ import annotations

import base64
import time

from sourcer.onboard import links
from sourcer.onboard.privacy import REDACTED, contact_detail, redact


def test_long_dotted_text_redacts_in_linear_time() -> None:
    hostile = "a." * 100_000
    started = time.perf_counter()
    redact(hostile)
    assert time.perf_counter() - started < 5  # was minutes before the quantifiers were bounded


def test_redact_with_a_limit_cuts_before_scanning() -> None:
    page = "x" * 2_000_000 + " key sk-" + "A" * 30
    assert redact(page, 100) == "x" * 100
    # a secret that starts before the cut is still masked whole
    assert redact("abc sk-" + "B" * 40, 10) == f"abc {REDACTED}"[:10]


def test_cut_in_links_uses_the_limit() -> None:
    assert links._cut("y" * 500_000, 50) == "y" * 50


def test_more_secret_shapes_are_masked() -> None:
    cases = {
        'password = "correct horse battery staple"': "password = [redacted]",
        "DB_PASS=hunter2hunter2": "DB_PASS=[redacted]",
        # built at run time so secret scanners do not flag a fake header
        "Authorization: Basic " + base64.b64encode(b"sample-user:sample-value").decode(): REDACTED,
        "aws wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY end": f"aws {REDACTED} end",
    }
    for text, expected in cases.items():
        assert redact(text) == expected


def test_obfuscated_emails_are_masked_and_counted_as_contact_details() -> None:
    assert redact("write to jane [at] example [dot] com") == "write to [email]"
    assert contact_detail("jane (at) example (dot) org")


def test_ordinary_words_and_commit_hashes_are_left_alone() -> None:
    for text in (
        "commit 0123456789abcdef0123456789abcdef01234567",
        "compass = north star",
        "Compass: navigation app",
        "Bypass: approvals queue",
        "compass = northstarnavigator",
        "a basic understanding of SQL",
        "passport number pending",
    ):
        assert redact(text) == text
