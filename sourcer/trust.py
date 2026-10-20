"""How sure we are that a posting is real, from its link and text alone.

  python -m sourcer.trust            # tier counts and the held back rows

Three tiers:

  official    the link is the company's own job board or careers domain
  platform    a job platform that vets employers (Handshake, LinkedIn, YC,
              Wellfound, Built In). Real postings often live only there, so
              these are shown with a note, not dropped
  unverified  a red flag in the text, an agency posting, a board whose name
              does not match the company, or an unknown domain. Held back

No network and no model calls: this reads the URL, the company name, and
the stored description, so it can run on every row at send time.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs, urlparse

from . import config

OFFICIAL, PLATFORM, UNVERIFIED = "official", "platform", "unverified"


class Trust(NamedTuple):
    tier: str
    reason: str


# Hosted boards where the owner is named in the link. Enterprise systems are
# sold to companies under contract; the others offer free or self serve
# signup, which is where fake postings appear, so a name mismatch there
# counts as a red flag rather than an abbreviation.
_PATH_OWNER = {  # host suffix -> board name is the first path segment
    "greenhouse.io": True,
    "lever.co": True,
    "ashbyhq.com": True,
    "smartrecruiters.com": False,  # free tier; training mills post here
    "workable.com": False,
    "rippling.com": False,
    "jobvite.com": True,
    "dover.com": False,
    "careerpuck.com": True,
}
_SUBDOMAIN_OWNER = {  # host suffix -> board name is the leftmost label
    "myworkdayjobs.com": True,
    "icims.com": True,
    "eightfold.ai": True,
    "avature.net": True,
    "bamboohr.com": False,
    "applytojob.com": False,
    "breezy.hr": False,
    "pinpointhq.com": False,
}
# Enterprise systems whose links do not name the owner (tenant ids).
_OPAQUE_ATS = (
    "oraclecloud.com",
    "successfactors.com",
    "successfactors.eu",
    "taleo.net",
    "myworkdaysite.com",
    "ultipro.com",
    "paylocity.com",
    "adp.com",
    "brassring.com",
    "dayforcehcm.com",
)
PLATFORM_HOSTS = (
    "linkedin.com",
    "joinhandshake.com",
    "wellfound.com",
    "angel.co",
    "workatastartup.com",
    "ycombinator.com",
    "builtin.com",
    "builtinnyc.com",
    "simplify.jobs",
)

_NAME_STOP = {
    "the", "inc", "llc", "ltd", "corp", "corporation", "co", "company", "group",
    "holdings", "technologies", "technology", "labs", "lab", "ai", "and", "of",
    "systems", "solutions", "services", "global", "international", "us", "usa",
    "com", "io",
}  # fmt: skip

# Asks of the applicant only: fintech and retail postings mention payments
# and gift cards as the product, which is not a red flag.
FEE_RE = re.compile(
    r"\b(?:application|registration|training|processing|onboarding|placement)\s+fee"
    r"|\b(?:you|candidates?|applicants?)\s+(?:will\s+|must\s+|are\s+required\s+to\s+)?"
    r"(?:pay|send|deposit)\s+(?:a\s+|the\s+|an\s+)?(?:\w+\s+)?(?:fee|money|payment|deposit)"
    r"|\bpurchase\s+(?:your\s+own\s+)?(?:equipment|laptop|software)\s+(?:with|using)\s+(?:a|the)\s+check"
    r"|\bcheck\s+to\s+purchase",
    re.I,
)
CONTACT_RE = re.compile(
    r"[\w.+-]+@(?:gmail|yahoo|outlook|hotmail|aol|protonmail|proton)\.\w+"
    r"|\b(?:contact|message|text|reach|interview)\b[^.\n]{0,40}"
    r"\b(?:on|via|through)\s+(?:telegram|whatsapp|signal)\b",
    re.I,
)
AGENCY_RE = re.compile(
    r"\bon\s+behalf\s+of\s+(?:our|a|an)\s+client|\bour\s+client,?\s+(?:a|an)\b"
    r"|\bconfidential\s+(?:client|company)\b",
    re.I,
)
# Consultancies that place students with clients after paid training.
PLACEMENT_RE = re.compile(
    r"\b(?:c2c|corp[\s-]to[\s-]corp|h-?1b\s+transfer|bench\s+sales"
    r"|training\s+(?:and|&)\s+placement|placement\s+(?:assistance|program)"
    r"|market(?:ing)?\s+(?:your|the\s+candidate'?s?)\s+(?:profile|resume))\b",
    re.I,
)
# Companies the operator has ruled out, one name per line ("#" comments).
# Kept outside the public code: a private list, not a public accusation.
BLOCKLIST_PATH = Path(os.environ.get("SOURCER_BLOCKLIST", config.PROJECT_ROOT / "blocklist.txt"))


@lru_cache(maxsize=1)
def blocklist() -> frozenset[str]:
    try:
        lines = BLOCKLIST_PATH.read_text().splitlines()
    except OSError:
        return frozenset()
    names = (name_key(line.split("#", 1)[0])[0] for line in lines)
    return frozenset(n for n in names if n)


def name_key(company: str) -> tuple[str, list[str]]:
    """Compact name plus its meaningful words: "The Walt Disney Company" ->
    ("waltdisney", ["walt", "disney"])."""
    words = [w for w in re.findall(r"[a-z0-9]+", (company or "").lower()) if w not in _NAME_STOP]
    return "".join(words), words


def names_match(company: str, owner: str) -> bool:
    """Whether a board or domain name plausibly belongs to the company:
    full name, any distinctive word, or initials in order ("Northrop Grumman"
    ~ "ngc", "Johns Hopkins Applied Physics Laboratory" ~ "jhuapl")."""
    owner = re.sub(r"[^a-z0-9]", "", (owner or "").lower())
    key, words = name_key(company)
    if not owner or not key:
        return False
    if key in owner or (len(owner) >= 3 and owner in key):
        return True
    if any(len(w) >= 3 and w in owner for w in words):
        return True
    initials = "".join(w[0] for w in words)
    if len(initials) < 2 or owner[0] != initials[0]:
        return False
    rest = iter(owner)
    return all(ch in rest for ch in initials)  # initials appear in order


def _host_ends(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def board_owner(url: str) -> tuple[str, str, bool] | None:
    """(system, owner name, enterprise) for a hosted job board link, else None.
    Owner is "" when the link does not name it."""
    parts = urlparse(url or "")
    host = (parts.hostname or "").lower()
    segs = [s for s in parts.path.split("/") if s]
    for suffix, enterprise in _PATH_OWNER.items():
        if _host_ends(host, suffix):
            if suffix == "greenhouse.io" and segs[:1] == ["embed"]:
                owner = parse_qs(parts.query).get("for", [""])[0]
            elif suffix in ("dover.com", "careerpuck.com"):  # /apply/<owner>, /job-board/<owner>
                owner = segs[1] if len(segs) > 1 else ""
            else:
                owner = segs[0] if segs else ""
            return suffix, owner, enterprise
    for suffix, enterprise in _SUBDOMAIN_OWNER.items():
        if _host_ends(host, suffix) and host != suffix:
            label = host.split(".")[0]
            return suffix, re.sub(r"^(?:careers|jobs)-|-(?:careers|jobs)$", "", label), enterprise
    for suffix in _OPAQUE_ATS:
        if _host_ends(host, suffix):
            return suffix, "", True
    return None


def red_flag(company: str, description: str) -> str | None:
    text = description or ""
    if config.STAFFING_AGENCY_RE.search(company or ""):
        return "staffing agency"
    if name_key(company)[0] in blocklist():
        return "on your blocklist"
    for pattern, reason in (
        (FEE_RE, "asks for money"),
        (CONTACT_RE, "personal email or chat app contact"),
        (AGENCY_RE, "agency posting for an unnamed client"),
        (PLACEMENT_RE, "training and placement firm"),
    ):
        m = pattern.search(text)
        if m:
            return f"{reason}: {m.group(0)[:40]!r}"
    return None


def classify(  # noqa: PLR0911 (one return per rule reads clearest)
    company: str, url: str, description: str = "", domain: str | None = None
) -> Trust:
    """The trust tier for one posting. ``domain`` is the company's known
    website, when the companies table has it."""
    flag = red_flag(company, description)
    if flag:
        return Trust(UNVERIFIED, flag)
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return Trust(UNVERIFIED, "no link")
    board = board_owner(url)
    if board:
        system, owner, enterprise = board
        if not owner:
            return Trust(OFFICIAL, f"company system on {system}")
        if names_match(company, owner):
            return Trust(OFFICIAL, f"company board on {system}")
        if enterprise:
            return Trust(OFFICIAL, f"company board on {system} ({owner!r}, name abbreviated)")
        return Trust(UNVERIFIED, f"board {owner!r} on {system} does not match {company!r}")
    if any(_host_ends(host, p) for p in PLATFORM_HOSTS):
        return Trust(PLATFORM, f"only on {host.removeprefix('www.')}")
    if domain and _host_ends(host, domain.lower().removeprefix("www.")):
        return Trust(OFFICIAL, "company careers site")
    labels = [lab for lab in host.split(".")[:-1] if lab not in {"www", "careers", "jobs"}]
    if any(names_match(company, lab) for lab in labels):
        return Trust(OFFICIAL, "company careers site")
    return Trust(UNVERIFIED, f"link on {host}, not the company's domain")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--show", type=int, default=40, help="held back rows to print")
    args = ap.parse_args(argv)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    domains = {
        (n or "").lower(): d
        for n, d in conn.execute("SELECT name, domain FROM companies WHERE domain IS NOT NULL")
    }
    counts: dict[str, int] = {}
    held = []
    for company, url, desc in conn.execute("SELECT company, url, description FROM postings"):
        t = classify(company, url, desc or "", domains.get((company or "").lower()))
        counts[t.tier] = counts.get(t.tier, 0) + 1
        if t.tier == UNVERIFIED:
            held.append((company, t.reason))
    print(counts)
    for company, reason in held[: args.show]:
        print(f"held: {company}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
