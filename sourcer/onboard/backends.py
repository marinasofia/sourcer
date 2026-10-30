"""Which model backend onboarding uses, detected from the machine.

A backend is the ``SOURCER_LLM`` value ``llm.get_client`` already knows.
To add another CLI (codex, gemini), give it a client in ``sourcer/`` that
mimics ``messages.create`` the way ``claude_cli`` does, teach
``llm.get_client`` its name, and add a ``Backend`` entry here.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from .. import claude_cli, config

ANTHROPIC_PRIVACY = "https://www.anthropic.com/legal/privacy"


@dataclass(frozen=True)
class Backend:
    name: str  # the SOURCER_LLM value
    label: str  # how the candidate sees it
    available: Callable[[], bool]
    destination: str  # what the audit log records for a model call
    priced: bool  # True when calls cost dollars on the candidate's own key
    privacy_url: str = ANTHROPIC_PRIVACY


def _has_claude_cli() -> bool:
    return claude_cli._find_binary() is not None


def _has_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


BACKENDS: dict[str, Backend] = {
    "claude-code": Backend(
        "claude-code", "your Claude subscription, through the claude CLI", _has_claude_cli,
        "anthropic via claude CLI", priced=False,
    ),
    "api": Backend(
        "api", "your Anthropic API key", _has_api_key, "api.anthropic.com", priced=True,
    ),
}  # fmt: skip
NONE_FOUND = (
    "No model found. Sourcer needs one of these, both your own:\n"
    "  1. The Claude Code CLI signed in to your Claude subscription (no API cost):\n"
    "     install it from https://claude.com/claude-code, run `claude` once to sign in.\n"
    "  2. An Anthropic API key: put ANTHROPIC_API_KEY=... in .env (paid per call).\n"
    "Then run `sourcer init` again."
)


def detect(choice: str = "auto") -> tuple[Backend | None, str]:
    """The backend to use and a message. ``auto`` prefers the CLI, then a key."""
    if choice != "auto":
        backend = BACKENDS.get(choice)
        if backend is None:
            return None, f"unknown backend {choice!r}; use auto, {', '.join(BACKENDS)}"
        if not backend.available():
            return None, f"{backend.label} is not available on this machine.\n{NONE_FOUND}"
        return backend, ""
    for backend in BACKENDS.values():
        if backend.available():
            return backend, ""
    return None, NONE_FOUND


def current() -> Backend:
    """The backend the process is configured for (SOURCER_LLM)."""
    return BACKENDS.get(config.LLM_BACKEND, BACKENDS["api"])
