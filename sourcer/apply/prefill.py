"""Application prefill. Reserved: not implemented.

Intended to fill an application form from the profile and a tailored resume.
Submission stays behind the ``submit_enabled`` flag on the companies table,
which defaults to off.
"""

from __future__ import annotations

from typing import Any


def prefill(posting: dict[str, Any], profile: dict[str, Any] | None) -> None:
    """Fill the application form for one posting without submitting it."""
    raise NotImplementedError(f"prefill is not implemented ({posting.get('title')})")
