"""Text from outside made safe to print and store.

Job boards, discovered company pages and onboarding sources are all written
by someone else. A terminal escape in a title can clear the screen or forge a
line of the digest, and bidi marks can reorder what a reader sees, so every
field from outside goes through here once, at the point it enters.
"""

from __future__ import annotations

import re
from typing import Any

# control characters, and bidi and line separator marks that can reorder a line
CONTROL_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)
# the same, but keeping line breaks and tabs for multi line text
CONTROL_KEEP_LINES_RE = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)


def printable(text: Any) -> str:
    """Text safe to print: control characters (terminal escapes) removed."""
    return CONTROL_RE.sub("", str(text or ""))


def clean(text: Any, limit: int) -> str:
    """One line of plain text: no control characters (a terminal escape in a
    source cannot reach the review screen), single spaces, capped."""
    return " ".join(CONTROL_RE.sub(" ", str(text or "")).split())[:limit]


def one_line(text: Any) -> str:
    """A field meant for one line: control characters and line breaks become
    single spaces, so it cannot start a forged line of its own."""
    return " ".join(CONTROL_RE.sub(" ", str(text)).split())


def multi_line(text: Any) -> str:
    """Multi line text with control characters removed and line breaks kept."""
    return CONTROL_KEEP_LINES_RE.sub("", str(text)).replace("\r", "")
