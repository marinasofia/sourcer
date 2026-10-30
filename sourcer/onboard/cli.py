"""``sourcer init``, ``sourcer facts`` and ``sourcer forget``.

init: a short privacy note, the model backend, the sources (each opt in),
a starter profile, the settings that turn on evidence scoring, then the
first scan. facts: scan, review, list and the outbound call log. forget:
remove one source's facts or every onboarding file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config, llm, log
from . import backends, folders, links, store, weights
from .collect import collect_all
from .extract import clean, estimate, printable, run, write_preview
from .github import valid_username
from .items import SourceItem, source_spec
from .resume import SUFFIXES as RESUME_SUFFIXES

# Two lines because most people press Enter. "Keeps" is about storage: every
# file Sourcer writes is local; excerpts sent to the model are in `details`.
PRIVACY_SHORT = """Sourcer keeps your data only on this computer. Nothing is sent to us.
Press Enter to start, or type `details`."""


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def show(path: Path) -> str:
    """A path as the candidate reads it: relative to the project or with ~."""
    path = Path(path).expanduser()
    for base, label in ((config.PROJECT_ROOT, ""), (Path.home(), "~/")):
        try:
            return label + path.resolve().relative_to(Path(base).resolve()).as_posix()
        except ValueError:
            continue
    return str(path)


def privacy_details() -> str:
    """The full explanation behind the short note (docs/privacy.md says the same)."""
    return f"""What Sourcer reads
  Only the sources you add, when you add them and when you run `sourcer sync`:
  your resume file, the public GitHub repositories of the username you give,
  the project folders you include, the LinkedIn data export zip you
  downloaded, and links you add (robots.txt first, never another site).
  In a folder it reads READMEs and package manifests at each project root,
  counts files by type, and asks git for first and last commit dates. It never
  opens .env files, keys, certificates, credential or secret files, anything
  .gitignore excludes, dependency or build folders, hidden files, links,
  binaries, or files over {config.ONBOARD_MAX_FILE_BYTES:,} bytes. Secrets, email
  addresses and phone numbers are masked in every excerpt.

What leaves this computer, and who sees it
  1. Excerpts to Anthropic's Claude, the only model provider, through your own
     Claude CLI login or your own API key. With the CLI each call runs with no
     tools, no file access, no MCP servers and an empty working folder, so the
     model sees only the excerpts Sourcer passes it. With an API key they go
     straight to api.anthropic.com. Anthropic's own policy covers what it
     keeps: {backends.ANTHROPIC_PRIVACY}
  2. Requests to api.github.com for the username you give (public data only).
  3. Requests to the links you add, and to the public APIs of Hugging Face and
     Credly for those links. Sourcer reads robots.txt first, names itself in
     every request, reads at most a few pages per link and never follows a
     link to another site.
  This project never collects your data and never trains anything on it. It
  has no server, no telemetry and no account. Before any model call you see
  how much will be read, and `p` writes the exact excerpts to a preview file
  instead. `sourcer facts log` lists every outbound call.

How long it is kept
  In the local files below until you delete them. Sourcer keeps nothing
  anywhere else.
  sources:   {show(config.SOURCES_PATH)}
  facts:     {show(config.FACTS_PATH)}
  evidence:  {show(config.EVIDENCE_PATH)}
  profile:   {show(config.PROFILE_PATH)}
  preview:   {show(config.ONBOARD_PREVIEW_PATH)}
  call log:  {show(config.ONBOARD_LOG_PATH)}
  `sourcer forget --source github` removes one source and its facts.
  `sourcer forget --all` deletes every onboarding file; profile.json and .env stay."""


class Console:
    """Prompts with defaults. ``yes`` accepts every confirmation; without a
    terminal and without ``yes`` every confirmation is declined, so a script
    never starts model calls by accident."""

    def __init__(
        self,
        *,
        yes: bool = False,
        interactive: bool | None = None,
        input_fn: Callable[[str], str] = input,
        out: Callable[[str], None] | None = None,
    ) -> None:
        self.yes = yes
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.input_fn = input_fn
        self.out = out or print

    def say(self, text: str = "") -> None:
        self.out(text)

    def ask(self, prompt: str, default: str = "") -> str:
        if not self.interactive or self.yes:
            return default
        try:
            answer = self.input_fn(prompt).strip()
        except EOFError:
            return default
        return answer or default

    def confirm(self, prompt: str, default: bool = False) -> bool:
        if self.yes:
            return True
        if not self.interactive:
            return False
        answer = self.ask(prompt, "").lower()
        return default if not answer else answer in ("y", "yes")


# ---------------------------------------------------------------- settings


def set_env(updates: dict[str, str], path: Path | None = None) -> list[str]:
    """Set keys in the project .env, keeping every other line. Returns the keys changed."""
    path = path or config.PROJECT_ROOT / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    changed = []
    for key, value in updates.items():
        line = f"{key}={value}"
        for n, existing in enumerate(lines):
            if re.match(rf"^\s*(export\s+)?{re.escape(key)}\s*=", existing):
                if existing.strip() != line:
                    lines[n] = line
                    changed.append(key)
                break
        else:
            lines.append(line)
            changed.append(key)
    if changed:
        store.write_private(path, "\n".join(lines) + "\n")
    return changed


def _next_may() -> str:
    today = config.utc_now().date()
    return f"{today.year + (1 if today.month > 5 else 0)}-05"


WORK_AUTH = {
    "1": {"us_citizen": True, "needs_sponsorship": False},
    "citizen": {"us_citizen": True, "needs_sponsorship": False},
    "2": {"us_citizen": False, "needs_sponsorship": False},
    "resident": {"us_citizen": False, "needs_sponsorship": False},
    "3": {"us_citizen": False, "needs_sponsorship": True},
    "sponsorship": {"us_citizen": False, "needs_sponsorship": True},
}


def build_profile(console: Console, args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    """The starter profile and the metros, asked in one short step."""
    console.say("About you (Enter keeps the default):")
    grad = args.grad or console.ask(f"  Graduation, YYYY-MM [{_next_may()}]: ", _next_may())
    match = re.fullmatch(r"(20\d\d)-(0?[1-9]|1[0-2])", grad.strip())
    if not match:
        console.say(f"  {grad!r} is not YYYY-MM; using {_next_may()}.")
        match = re.fullmatch(r"(20\d\d)-(\d\d)", _next_may())
    assert match is not None
    year, month = int(match.group(1)), int(match.group(2))
    degree = args.degree or console.ask("  Degree [Bachelor's]: ", "Bachelor's")
    auth = args.work_auth or console.ask(
        "  Work authorization: 1 US citizen, 2 no sponsorship needed, 3 need sponsorship"
        " [Enter skips]: ",
        "",
    )
    choices = ", ".join(config.METRO_TERMS)
    metros_raw = args.metros or console.ask(f"  Cities, from {choices} [nyc]: ", "nyc")
    metros = [
        m.strip().lower() for m in metros_raw.split(",") if m.strip().lower() in config.METRO_TERMS
    ]
    today = config.utc_now().date()
    profile: dict[str, Any] = {
        "education": degree,
        "in_progress": not args.graduated and (year, month) >= (today.year, today.month),
        "grad_year": year,
        "grad_month": month,
        "max_years_experience": args.max_years,
    }
    profile.update(WORK_AUTH.get(auth.strip().lower(), {}))
    return profile, ",".join(metros or ["nyc"])


# ---------------------------------------------------------------- sources


def classify(entry: str) -> tuple[str, str] | str:
    """(type, value) for a free form source entry, or an error message."""
    entry = entry.strip()
    if re.match(r"^https?://", entry, re.I):
        return "link", entry
    path = Path(entry).expanduser()
    if path.is_dir():
        return "folder", str(path.resolve())
    if path.is_file() and path.suffix.lower() == ".zip":
        return "linkedin", str(path.resolve())
    if path.is_file() and path.suffix.lower() in RESUME_SUFFIXES:
        return "resume", str(path.resolve())
    if valid_username(entry.lstrip("@")) and not path.exists():
        return "github", entry.lstrip("@")
    return f"not a folder, link, zip or resume file: {entry}"


def label(kind: str, value: str) -> str:
    if kind in ("resume", "linkedin", "publications"):
        return Path(value).name
    if kind == "folder":
        return show(Path(value))
    return value


def folder_line(path: Path, excludes: list[str]) -> str:
    found = folders.plan(path, excludes)
    n = len(found.files)
    return (
        f"{show(path)}: {plural(n, 'file')} to read in {plural(len(found.projects), 'project')}, "
        "secrets, ignored files and dependencies skipped."
    )


def consent_folder(console: Console, path: Path, excludes: list[str]) -> list[str] | None:
    """The one line consent for a folder. Returns its exclude globs, or None
    when the candidate declines."""
    while True:
        line = folder_line(path, excludes)
        if console.yes or not console.interactive:
            console.say(line)
            return excludes if console.yes else None
        answer = console.ask(f"{line} Include? [Y/n]  (e to exclude subfolders) ", "y").lower()
        if answer == "e":
            more = console.ask("  Globs to skip, comma separated (like data/*, old): ", "")
            excludes = [*excludes, *(g.strip() for g in more.split(",") if g.strip())]
            continue
        return excludes if answer in ("y", "yes") else None


def check_source(kind: str, value: str) -> str | None:
    """Why a source cannot be added, or None."""
    if kind in links.LINK_TYPES:
        found = links.detect(value)
        if isinstance(found, str):
            return found
        return None if found[0] == kind else f"that is a {found[0]} link, not {kind}: {value}"
    path = Path(value)
    suffix = path.suffix.lower()
    problems = {
        "resume": (not path.is_file() or suffix not in RESUME_SUFFIXES,
                   f"resume not found or not {', '.join(RESUME_SUFFIXES)}: {path.name}"),
        "github": (not valid_username(value), f"not a GitHub username: {value!r}"),
        "folder": (not path.is_dir(), f"folder not found: {show(path)}"),
        "linkedin": (not (path.is_file() and suffix == ".zip"),
                     f"LinkedIn export zip not found: {path.name}"),
        "publications": (not (path.is_file() and suffix in links.PUBLICATION_SUFFIXES),
                         f"publications list not found or not TXT or MD: {path.name}"),
    }  # fmt: skip
    bad, why = problems.get(kind, (False, ""))
    return why if bad else None


def revoke_hint(data: dict[str, Any], spec: dict[str, Any]) -> str:
    """One line on taking a source back, naming it the shortest exact way."""
    same = [s for s in data["sources"] if s["type"] == spec["type"]]
    name = spec["type"] if len(same) == 1 else spec["id"]
    return f"Take it back anytime with `sourcer forget --source {name}`."


def add_one(
    console: Console, data: dict[str, Any], kind: str, value: str, excludes: list[str] | None = None
) -> dict[str, Any] | None:
    """Check one source, ask consent for a folder, and save it. Returns its
    entry, or None when it was not added. A link is saved as the kind its
    URL names (a feed, a Hugging Face profile...)."""
    if kind == "link":
        found = links.detect(value)
        if isinstance(found, str):
            console.say(f"  skipped: {found}")
            return None
        kind, value = found
    problem = check_source(kind, value)
    if problem:
        console.say(f"  skipped: {problem}")
        return None
    extra: dict[str, Any] = {"label": label(kind, value)}
    if kind == "folder":
        chosen = consent_folder(console, Path(value), list(excludes or []))
        if chosen is None:
            console.say(f"Okay, leaving {show(Path(value))} out.")
            return None
        extra["excludes"] = chosen
    spec = source_spec(kind, value, **extra)
    if not store.add_source(data, spec):
        console.say(f"{extra['label']} is already added.")
    store.save_sources(data)
    return next(s for s in data["sources"] if s["id"] == spec["id"])


def flag_sources(args: argparse.Namespace) -> list[tuple[str, str]]:
    def where(p: str) -> str:
        return str(Path(p).expanduser().resolve())

    return [
        *(("resume", where(p)) for p in args.resume or []),
        *(("github", u.lstrip("@")) for u in args.github or []),
        *(("folder", where(p)) for p in args.folder or []),
        *(("link", u) for u in args.link or []),
        *(("linkedin", where(p)) for p in args.linkedin_export or []),
        *(("publications", where(p)) for p in args.publications or []),
    ]


# ---------------------------------------------------------------- scan


def _pending_items(collected: dict[str, Any], scanned: dict[str, str]) -> list[SourceItem]:
    """Items that are new or changed since the last scan."""
    return [i for c in collected.values() for i in c.items if scanned.get(i.id) != i.sha]


# what one entry of Collected.read is, by source type; anything else is a file
READ_NOUNS = {"github": "repo", "huggingface": "item", "credly": "badge", "feed": "post",
              "scholar": "list", "page": "page", "kaggle": "page", "devpost": "page",
              "link": "page"}  # fmt: skip


def _noun(kind: str) -> str:
    return READ_NOUNS.get(kind, "file")


def _reads_line(sources: list[dict[str, Any]], collected: dict[str, Any]) -> str:
    counts: Counter[str] = Counter()
    for source in sources:
        got = collected.get(source["id"])
        if got is not None:
            counts[_noun(source["type"])] += len(got.read)
    parts = [plural(n, noun) for noun, n in counts.items() if n]
    return " and ".join(parts) or plural(0, "file")


def _summary(sources: list[dict[str, Any]], collected: dict[str, Any], console: Console) -> None:
    for source in sources:
        got = collected.get(source["id"])
        if got is None:
            continue
        names = ", ".join(got.read[:6]) + (", ..." if len(got.read) > 6 else "")
        what = plural(len(got.read), _noun(source["type"]))
        console.say(printable(f"  {source['type']} {source.get('label') or source['value']}: "
                          f"{what}{' (' + names + ')' if names else ''}"))  # fmt: skip
        for note in got.notes:
            console.say(f"    {printable(note)}")


def _gate(console: Console, items: list[SourceItem], line: str) -> bool:
    """Yes, no, or p to write the preview and ask again."""
    if console.yes:
        console.say(line.partition(" Continue?")[0])
        return True
    if not console.interactive:
        console.say(line.partition(" Continue?")[0])
        console.say(
            "Not a terminal: no model calls. Run with --yes to start, or --dry-run to preview."
        )
        return False
    while True:
        answer = console.ask(line + " ", "y").lower()
        if answer == "p":
            console.say(f"Preview written to {show(write_preview(items))}. Nothing was sent.")
            continue
        return answer in ("y", "yes")


def top_skills(facts: list[dict[str, Any]], n: int = 6) -> list[str]:
    """The top skills of the facts already approved plus the ones just found,
    ranked by the weight formula (weights.compute); shown, not written."""
    approved = [f for f in store.load_facts()["facts"] if f.get("status") == "approved"]
    return weights.ranking([*approved, *({**f, "status": "approved"} for f in facts)], n)


@dataclass
class Outcome:
    code: int = 0
    facts: list[dict[str, Any]] = field(default_factory=list)
    collected: dict[str, Any] = field(default_factory=dict)
    added: int = 0  # facts newly queued for review
    waiting: int = 0  # model calls that need a yes nobody could give (no terminal)


def _scan(
    console: Console,
    backend_choice: str = "auto",
    *,
    only: set[str] | None = None,
    dry_run: bool = False,
    first: bool = False,
    word: str = "",
    client_factory: Callable[[], Any] | None = None,
) -> Outcome:
    """Read the sources (all, or the ids in ``only``), show what will be sent,
    and with consent extract candidate facts. No model call without a yes.
    Afterwards each source keeps the stamps of what was fully read."""
    outcome = _scan_sources(console, backend_choice, only=only, dry_run=dry_run, first=first,
                            word=word, client_factory=client_factory)  # fmt: skip
    if outcome.collected:
        store.save_stamps(outcome.collected, store.load_facts()["scanned"])
    return outcome


def _scan_sources(  # noqa: PLR0911 (each early stop says why no call ran)
    console: Console,
    backend_choice: str,
    *,
    only: set[str] | None,
    dry_run: bool,
    first: bool,
    word: str,
    client_factory: Callable[[], Any] | None,
) -> Outcome:
    sources = [s for s in store.load_sources()["sources"] if only is None or s["id"] in only]
    if not sources:
        console.say("No sources yet. Add some with `sourcer init --add`.")
        return Outcome(1)
    backend, problem = backends.detect(backend_choice)
    if backend is None and not dry_run:
        console.say(problem)
        return Outcome(1)
    collected = collect_all(sources)
    _summary(sources, collected, console)
    facts = store.load_facts()
    items = _pending_items(collected, facts["scanned"])
    if not items:
        console.say("Nothing new to read.")
        return Outcome(0, collected=collected)
    if dry_run or backend is None:
        console.say(f"Preview written to {show(write_preview(items))}. Nothing was sent.")
        return Outcome(0, collected=collected)
    if config.COST_CAP_USD <= 0:
        console.say("SOURCER_COST_CAP is 0, so no model calls run. Use --dry-run to preview.")
        return Outcome(0, collected=collected)
    est = estimate(items, backend)
    cost = (
        f"about ${est.usd:.2f} on your API key"
        if est.usd is not None
        else "on your Claude subscription"
    )
    calls = f"about {plural(est.calls, 'model call')} {cost}"
    word = word or ("First scan" if first else "This scan")
    line = (
        f"{word} reads {_reads_line(sources, collected)}, {calls}. Continue? [Y/n]"
        "  (p to preview what will be sent)"
    )
    if est.usd is not None and est.usd > config.COST_CAP_USD:
        console.say(f"Note: the cost cap of ${config.COST_CAP_USD:.2f} stops the scan early; "
                    "raise it with --cost-cap.")  # fmt: skip
    if not _gate(console, items, line):
        console.say("No model calls made.")
        waiting = est.calls if not (console.yes or console.interactive) else 0
        return Outcome(0, collected=collected, waiting=waiting)
    config.LLM_BACKEND = backend.name
    result = run((client_factory or llm.get_client)(), items, backend, llm.Usage())
    added = store.merge_facts(facts, result.facts)
    for item in result.done:
        facts["scanned"][item.id] = item.sha
    store.save_facts(facts)
    new_sources = {f["source_id"] for f in result.facts}
    skills = ", ".join(top_skills(result.facts)) or "none named yet"
    console.say(
        f"Found {plural(added, 'fact')} from {plural(len(new_sources), 'source')}. "
        f"Top skills: {skills}."
    )
    if result.dropped:
        console.say(f"Left out {result.dropped} facts whose quote was not in the source.")
    if result.failed_calls:
        console.say(f"{result.failed_calls} calls failed; run `sourcer facts scan` to retry them.")
    if result.stopped:
        console.say(f"Stopped early: {result.stopped}.")
    return Outcome(0, result.facts, collected, added)


def scan(
    console: Console,
    backend_choice: str = "auto",
    *,
    dry_run: bool = False,
    first: bool = False,
    client_factory: Callable[[], Any] | None = None,
) -> int:
    """Scan every source, then offer to review what was found."""
    outcome = _scan(
        console, backend_choice, dry_run=dry_run, first=first, client_factory=client_factory
    )
    if outcome.facts:
        offer_review(console, store.load_facts())
    return outcome.code


# ---------------------------------------------------------------- review


def offer_review(console: Console, facts: dict[str, Any]) -> None:
    pending = [f for f in facts["facts"] if f["status"] == "pending"]
    if console.yes or not console.interactive:
        console.say(f"{len(pending)} facts wait for you: `sourcer facts review`.")
        return
    answer = console.ask(
        f"Approve all {len(pending)} now (a), review one by one (r), or later (Enter): ", ""
    ).lower()
    if answer == "a":
        for fact in pending:
            fact["status"] = "approved"
        finish_review(console, facts)
    elif answer == "r":
        review(console, facts)


def finish_review(console: Console, facts: dict[str, Any]) -> None:
    store.save_facts(facts)
    count = store.write_evidence(facts["facts"])
    console.say(
        f"{plural(count, 'approved fact')} in {show(config.EVIDENCE_PATH)}; scoring uses them."
    )
    recompute_weights(console, facts["facts"])


def recompute_weights(console: Console, facts: list[dict[str, Any]]) -> None:
    """Rewrite skills_vector.json from the approved facts, keeping overrides."""
    done = weights.recompute(facts)
    if not done.written:
        console.say(f"Skill weights: {done.note}.")
        return
    top = ", ".join(f"{e['skill']} {e['weight']}" for e in done.skills[:5]) or "none yet"
    console.say(f"Skill weights updated: {top}. `sourcer skills` shows why.")


def _describe(fact: dict[str, Any], n: int, total: int) -> str:
    head = ", ".join(x for x in (fact.get("kind"), fact.get("group"), fact.get("dates")) if x)
    skills = ", ".join(fact.get("skills") or [])
    return (
        f"\n[{n}/{total}] {head}\n  {fact['text']}\n"
        + (f"  skills: {skills}\n" if skills else "")
        + f'  quote: "{fact["quote"]}"\n'
        + f"  from: {printable(fact['source_type'] + ' ' + str(fact['source_ref']))}"
    )


def review(console: Console, facts: dict[str, Any]) -> int:
    """Walk pending facts. Returns how many were decided."""
    pending = [f for f in facts["facts"] if f["status"] == "pending"]
    if not pending:
        console.say("No facts waiting for review.")
        return 0
    decided = 0
    prompt = "a approve, r reject, e edit, s approve all from this source, q quit [skip]: "
    for n, fact in enumerate(pending, 1):
        if fact["status"] != "pending":  # approved by an earlier "s"
            continue
        console.say(_describe(fact, n, len(pending)))
        answer = console.ask(prompt, "").lower()
        if answer == "q":
            break
        if answer == "a":
            fact["status"] = "approved"
        elif answer == "r":
            fact["status"] = "rejected"
        elif answer == "e":
            text = console.ask("  New text (Enter keeps it): ", "")
            if text:
                fact["text"], fact["edited"] = clean(text, 400), True
            fact["status"] = "approved"
        elif answer == "s":
            for other in pending:
                if other["source_id"] == fact["source_id"] and other["status"] == "pending":
                    other["status"] = "approved"
                    decided += 1
            continue
        else:
            continue
        decided += 1
    finish_review(console, facts)
    return decided


def list_facts(console: Console, status: str | None) -> int:
    facts = [f for f in store.load_facts()["facts"] if status in (None, f["status"])]
    if not facts:
        console.say("No facts yet. Run `sourcer init` or `sourcer facts scan`.")
        return 0
    for fact in facts:
        where = printable(f"{fact['source_type']} {fact['source_ref']}")
        console.say(f"[{fact['status']}] {fact.get('kind')}, {fact.get('group') or 'other'}: "
                    f"{fact['text']}  ({where})")  # fmt: skip
    counts = Counter(f["status"] for f in facts)
    console.say(", ".join(f"{counts[s]} {s}" for s in store.STATUSES if counts[s]))
    return 0


def print_log(console: Console) -> int:
    entries = store.read_audit()
    if not entries:
        console.say("No outbound calls recorded.")
        return 0
    for e in entries:
        where = ", ".join(e.get("sources") or [])
        console.say(f"{e.get('ts')}  {e.get('destination')}  {e.get('action')}  "
                    f"{e.get('chars', 0):,} chars  {where}")  # fmt: skip
    return 0


# ---------------------------------------------------------------- skills


def _change_weight(console: Console, action: str, name: str, weight: int | None) -> int:
    facts = store.load_facts()["facts"]
    try:
        if action == "set":
            entry = weights.set_override(facts, name, int(weight or 0))
            console.say(printable(
                f"{entry['skill']} is now {entry['weight']}, kept on every recompute. "
                f"`sourcer skills reset {entry['skill']}` undoes it."
            ))  # fmt: skip
            return 0
        if weights.reset_override(facts, name):
            console.say(printable(f"{name} is back to its computed weight."))
            return 0
    except ValueError as exc:
        console.say(f"{exc}.")
        return 1
    console.say(printable(f"{name} has no weight set by you."))
    return 1


def skills_cmd(
    console: Console, action: str | None, name: str = "", weight: int | None = None,
    force: bool = False,
) -> int:  # fmt: skip
    """List the weights with reasons, set or reset an override, or recompute."""
    if action in ("set", "reset"):
        return _change_weight(console, action, name, weight)
    if action == "compute":
        done = weights.recompute(store.load_facts()["facts"], force=force)
        if not done.written:
            console.say(f"{done.note}.")
            return 1
    data = weights.load_vector()
    if weights.hand_written(data):
        console.say(f"Skill weights: {weights.HAND_WRITTEN}.")
        return 0
    entries = data["skills"]
    if not entries:
        console.say("No skill weights yet. Approve facts with `sourcer facts review`.")
        return 0
    width = min(28, max(len(str(e["skill"])) for e in entries))
    for e in entries:
        mine = " (yours)" if e.get("source") == weights.OVERRIDE else ""
        line = f"{e['weight']:>3}  {e['skill']!s:<{width}}  {e.get('reason', '')}{mine}"
        console.say(printable(line))
    console.say(
        "Change one with `sourcer skills set NAME WEIGHT`; undo with `sourcer skills reset NAME`."
    )
    return 0


# ---------------------------------------------------------------- forget


def forget(console: Console, source: str | None, everything: bool) -> int:  # noqa: PLR0911
    sources = store.load_sources()
    facts = store.load_facts()
    if everything:
        paths = [config.FACTS_PATH, config.SOURCES_PATH, config.ONBOARD_PREVIEW_PATH,
                 config.ONBOARD_LOG_PATH]  # fmt: skip
        if not console.confirm(
            "Delete every onboarding file (facts, sources, preview, call log, computed skill "
            f"weights) and the onboarding part of {show(config.EVIDENCE_PATH)}? [y/N] "
        ):
            console.say("Nothing deleted.")
            return 0
        for path in paths:
            path.unlink(missing_ok=True)
        store.write_evidence([])
        if config.SKILLS_PATH.exists() and not weights.hand_written(weights.load_vector()):
            store.write_private(config.SKILLS_PATH, json.dumps(STARTER_SKILLS, indent=2) + "\n")
        console.say("Deleted. Your profile.json and .env were kept.")
        return 0
    if not source:
        for s in sources["sources"]:
            n = sum(1 for f in facts["facts"] if f["source_id"] == s["id"])
            name = printable(s.get("label") or s["value"])
            console.say(f"  {s['id']}  {s['type']} {name}  ({n} facts)")
        console.say(
            "Remove one with `sourcer forget --source ID`, or all with `sourcer forget --all`."
        )
        return 0
    match = [s for s in sources["sources"] if source in (s["id"], s.get("label"), s["value"])]
    match = match or [s for s in sources["sources"] if s["type"] == source]
    if len(match) > 1:
        console.say(f"You have {len(match)} {source} sources; `sourcer forget` lists their ids.")
        return 1
    if not match:
        console.say(f"No source {source!r}. `sourcer forget` lists them.")
        return 1
    target = match[0]
    n = sum(1 for f in facts["facts"] if f["source_id"] == target["id"])
    name = f"{target['type']} {target.get('label') or target['value']}"
    if not console.confirm(f"Remove {name} and its {n} facts? [y/N] "):
        console.say("Nothing removed.")
        return 0
    sources["sources"] = [s for s in sources["sources"] if s["id"] != target["id"]]
    facts["facts"] = [f for f in facts["facts"] if f["source_id"] != target["id"]]
    prefix = target["id"] + "/"
    facts["scanned"] = {k: v for k, v in facts["scanned"].items() if not k.startswith(prefix)}
    store.save_sources(sources)
    store.save_facts(facts)
    store.write_evidence(facts["facts"])
    console.say(f"Removed {name} and {n} facts.")
    recompute_weights(console, facts["facts"])
    return 0


# ---------------------------------------------------------------- init


STARTER_SKILLS = {
    "meta": {
        "source": "Starter file written by sourcer init. With evidence scoring on, fit is judged "
        "from your approved facts in the evidence file; this list is not used for it."
    },
    "skills": [],
}
CLOSING = "To delete everything Sourcer saved, run `sourcer forget --all`."


def privacy_screen(console: Console) -> bool:
    console.say(PRIVACY_SHORT)
    while True:
        answer = console.ask("> ", "").lower()
        if answer == "details":
            console.say(privacy_details())
            console.say("Press Enter to start, or q to stop.")
            continue
        return answer not in ("q", "quit", "n", "no")


def show_value(console: Console, facts: list[dict[str, Any]]) -> None:
    """A few of the facts just found, so the next question has a reason."""
    if not facts:
        return
    console.say("A few of them:")
    for fact in facts[:3]:
        console.say(f"  {fact['text']}")


def converse(console: Console, args: argparse.Namespace, backend: str) -> int:
    """The interactive setup: one source at a time, each scanned as it is added."""
    data = store.load_sources()
    found: list[dict[str, Any]] = []
    first = not args.add

    def scan_one(spec: dict[str, Any]) -> Outcome:
        nonlocal first
        outcome = _scan(console, backend, only={spec["id"]}, dry_run=args.dry_run, first=first)
        first = False
        found.extend(outcome.facts)
        show_value(console, outcome.facts)
        return outcome

    if not args.add:
        resume = console.ask(
            "Let's start with your resume. What is its path (PDF, DOCX, TXT or MD)? Enter skips: "
        )
        spec = resume and add_one(console, data, "resume", str(Path(resume).expanduser().resolve()))
        if spec:
            console.say(f"Got it, {spec['label']}. {revoke_hint(data, spec)}")
            scan_one(spec)
        user = console.ask("Want me to look at your public GitHub too? Your username, or Enter: ")
        spec = user and add_one(console, data, "github", user.strip().lstrip("@"))
        if spec:
            console.say(f"Got it, {spec['label']}. {revoke_hint(data, spec)}")
            got = scan_one(spec).collected.get(spec["id"])
            if got is not None and got.read:
                console.say(f"That was {plural(len(got.read), 'public repo')}.")
    console.say(
        f"I found {plural(len(found), 'fact')} so far. Adding a project folder usually finds more."
        if found
        else "A project folder on this computer helps me find your projects."
    )
    prompt = "Want me to look at a project folder? Its path, or Enter to skip: "
    while folder := console.ask(prompt):
        where = str(Path(folder).expanduser().resolve())
        spec = add_one(console, data, "folder", where, list(args.exclude or []))
        if spec:
            console.say(f"Got it. {revoke_hint(data, spec)}")
            scan_one(spec)
        prompt = "Another project folder? Its path, or Enter to move on: "
    more = console.ask(
        "Any links? A portfolio, blog or feed, Kaggle, Hugging Face, Devpost, Google Scholar or "
        "Credly page, or a LinkedIn data export zip. Comma separated, or Enter: "
    )
    for entry in (e for e in more.split(",") if e.strip()):
        kind = classify(entry)
        if isinstance(kind, str):
            console.say(f"  skipped: {kind}")
            continue
        spec = add_one(console, data, *kind)
        if spec is None:
            continue
        console.say(f"Got it, {spec['label']}. {revoke_hint(data, spec)}")
        if spec["type"] in links.LINK_TYPES:
            console.say(links.describe(spec["type"], spec["value"]))
        scan_one(spec)
    return 0


def setup_profile(console: Console, args: argparse.Namespace, backend: Any) -> None:
    """The starter profile when there is none, and the settings that turn on
    evidence scoring with the chosen backend."""
    settings = {"SOURCER_EVIDENCE_SCORING": "1"}
    if backend is not None:
        settings["SOURCER_LLM"] = backend.name
    if config.load_profile() is None:
        profile, metros = build_profile(console, args)
        store.write_private(config.PROFILE_PATH, json.dumps(profile, indent=2) + "\n")
        settings["SOURCER_METROS"] = metros
        console.say(f"Saved your profile to {show(config.PROFILE_PATH)}.")
    else:
        console.say(f"Using your existing {show(config.PROFILE_PATH)}.")
    if not config.SKILLS_PATH.exists():
        store.write_private(config.SKILLS_PATH, json.dumps(STARTER_SKILLS, indent=2) + "\n")
    changed = set_env(settings)
    if changed:
        console.say(f"Settings saved to .env: {', '.join(changed)}.")


def init(console: Console, args: argparse.Namespace) -> int:
    if not args.add and not privacy_screen(console):
        console.say("Stopped. Nothing was read or saved.")
        return 0
    backend, problem = backends.detect(args.backend)
    if backend is None and not args.dry_run:
        console.say(problem)
        return 1
    if backend is not None:
        console.say(f"I'll use {backend.label}.")
    choice = backend.name if backend is not None else args.backend
    flagged = flag_sources(args)
    if flagged or console.yes or not console.interactive:
        data = store.load_sources()
        for kind, value in flagged:
            spec = add_one(console, data, kind, value, list(args.exclude or []))
            if spec:
                console.say(f"Added {spec['type']} {spec['label']}.")
        if not args.add:
            setup_profile(console, args, backend)
        if args.dry_run or console.confirm("Scan your sources now? [Y/n] ", default=True):
            code = scan(console, choice, dry_run=args.dry_run, first=not args.add)
            if code:
                return code
    else:
        converse(console, args, choice)
        if not args.add:
            setup_profile(console, args, backend)
        facts = store.load_facts()
        if any(f["status"] == "pending" for f in facts["facts"]):
            offer_review(console, facts)
    console.say("Next: `sourcer` builds your job list from the facts you approved.")
    console.say(CLOSING)
    return 0


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="sourcer", description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--yes", action="store_true", help="accept every prompt (no questions)")
    common.add_argument("--backend", default="auto", choices=["auto", *backends.BACKENDS])
    common.add_argument("--cost-cap", type=float, default=None, metavar="USD")
    common.add_argument(
        "--dry-run",
        action="store_true",
        help="write the excerpts that would be sent to the preview file; no model calls",
    )

    init_p = sub.add_parser("init", parents=[common], help="set up sources, profile and settings")
    init_p.add_argument("--add", action="store_true", help="only add sources to an existing setup")
    init_p.add_argument("--resume", action="append", metavar="PATH")
    init_p.add_argument("--github", action="append", metavar="USER")
    init_p.add_argument("--folder", action="append", metavar="PATH")
    init_p.add_argument(
        "--exclude", action="append", metavar="GLOB", help="skip matching subfolders"
    )
    init_p.add_argument("--link", action="append", metavar="URL")
    init_p.add_argument("--linkedin-export", action="append", metavar="ZIP")
    init_p.add_argument(
        "--publications", action="append", metavar="FILE", help="a TXT or MD publications list"
    )
    init_p.add_argument("--grad", default="", metavar="YYYY-MM", help="graduation month")
    init_p.add_argument("--degree", default="")
    init_p.add_argument("--graduated", action="store_true", help="already graduated")
    init_p.add_argument("--max-years", type=int, default=2, help="most years of experience asked")
    init_p.add_argument(
        "--work-auth", default="", choices=["", "citizen", "resident", "sponsorship"]
    )
    init_p.add_argument(
        "--metros", default="", help=f"comma separated: {', '.join(config.METRO_TERMS)}"
    )

    facts_p = sub.add_parser("facts", help="scan, review and list candidate facts")
    facts_sub = facts_p.add_subparsers(dest="action", required=True)
    facts_sub.add_parser("scan", parents=[common], help="read sources and extract new facts")
    facts_sub.add_parser("review", help="approve, reject or edit pending facts")
    list_p = facts_sub.add_parser("list", help="list facts")
    list_p.add_argument("--status", choices=store.STATUSES)
    facts_sub.add_parser("log", help="every outbound call onboarding made")

    forget_p = sub.add_parser("forget", help="remove a source's facts or all onboarding data")
    forget_p.add_argument(
        "--source", metavar="ID", help="a source id or name from `sourcer forget`"
    )
    forget_p.add_argument("--all", action="store_true", dest="everything")
    forget_p.add_argument("--yes", action="store_true")

    sync_p = sub.add_parser(
        "sync", parents=[common], help="recheck every source and queue new or changed facts"
    )
    sync_p.add_argument(
        "--quiet", action="store_true", help="for scheduled runs: no prompts, one line of output"
    )

    skills_p = sub.add_parser("skills", help="skill weights with reasons, and your overrides")
    skills_sub = skills_p.add_subparsers(dest="action")
    set_p = skills_sub.add_parser("set", help="keep your own weight for a skill")
    set_p.add_argument("name")
    set_p.add_argument("weight", type=int, choices=range(1, 11), metavar="WEIGHT")
    reset_p = skills_sub.add_parser("reset", help="go back to the computed weight")
    reset_p.add_argument("name")
    compute_p = skills_sub.add_parser("compute", help="recompute the weights now")
    compute_p.add_argument(
        "--force", action="store_true", help="replace a hand written skills_vector.json"
    )
    return ap


def default_backend() -> str:
    """SOURCER_LLM when it is set (init writes it), else auto detection."""
    return config.LLM_BACKEND if os.environ.get("SOURCER_LLM") else "auto"


def main(argv: list[str] | None = None, console: Console | None = None) -> int:  # noqa: PLR0911
    args = _parser().parse_args(argv)
    log.configure()
    console = console or Console(yes=getattr(args, "yes", False))
    if getattr(args, "cost_cap", None) is not None:
        config.COST_CAP_USD = args.cost_cap
    if args.command == "init":
        return init(console, args)
    if args.command == "forget":
        return forget(console, args.source, args.everything)
    if args.command == "sync":
        from .sync import sync  # noqa: PLC0415 (sync builds on this module)

        choice = args.backend if args.backend != "auto" else default_backend()
        return sync(console, choice, quiet=args.quiet, dry_run=args.dry_run)
    if args.command == "skills":
        return skills_cmd(console, args.action, getattr(args, "name", ""),
                          getattr(args, "weight", None), getattr(args, "force", False))  # fmt: skip
    if args.action == "scan":
        choice = args.backend if args.backend != "auto" else default_backend()
        return scan(console, choice, dry_run=args.dry_run)
    if args.action == "review":
        review(console, store.load_facts())
        return 0
    if args.action == "list":
        return list_facts(console, args.status)
    return print_log(console)
