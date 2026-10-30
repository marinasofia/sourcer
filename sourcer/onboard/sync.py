"""``sourcer sync``: recheck every source and queue what is new or changed.

Each collector skips what it has already read: a resume or LinkedIn export
whose file hash is the same, a GitHub repo with nothing pushed since. What
is left goes through the same scan as ``sourcer init``: the summary, the
one line cost gate (or ``--yes``), the preview, redaction and the call log.
New facts wait for review; nothing is approved here.

``--quiet`` is for a scheduled run: no prompts and one line of output. When
model calls are needed and ``--yes`` was not given, nothing is sent or
queued; the run says so and the next interactive ``sourcer sync`` asks.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..log import get_logger
from . import store
from .cli import Console, _scan, plural, recompute_weights

log = get_logger(__name__)


def _pending() -> int:
    return sum(1 for f in store.load_facts()["facts"] if f.get("status") == "pending")


def end_line(added: int, pending: int) -> str:
    if added:
        return f"{plural(added, 'new fact')} waiting. Run `sourcer facts review`."
    if pending:
        return f"No new facts; {pending} still waiting. Run `sourcer facts review`."
    return "No new facts. Everything is up to date."


def sync(
    console: Console,
    backend_choice: str = "auto",
    *,
    quiet: bool = False,
    dry_run: bool = False,
    client_factory: Callable[[], Any] | None = None,
) -> int:
    """Recheck every source in sources.json. Returns the exit code."""
    if not store.load_sources()["sources"]:
        console.say("No sources yet. Add some with `sourcer init`.")
        return 1
    inner = console
    if quiet:  # the scan's own lines are dropped; one line is printed below
        inner = Console(yes=console.yes, interactive=False, out=lambda _line: None)
    outcome = _scan(
        inner, backend_choice, dry_run=dry_run, word="This sync", client_factory=client_factory
    )
    store.mark_synced()
    if dry_run:
        if quiet:
            console.say("Dry run: the preview file shows what would be sent. Nothing was sent.")
        return outcome.code
    if outcome.code:
        console.say("Sync stopped: no model backend found. Run `sourcer sync` to see why."
                    if quiet else "Sync stopped.")  # fmt: skip
        return outcome.code
    recompute_weights(inner, store.load_facts()["facts"])
    if outcome.waiting:
        log.info("sync: %d model calls need a yes; nothing sent or queued", outcome.waiting)
        console.say(
            f"Sync found changes that need {plural(outcome.waiting, 'model call')}; nothing was "
            "sent. Run `sourcer sync` to review them, or schedule it with --yes."
        )
        return 0
    console.say(end_line(outcome.added, _pending()))
    return 0
