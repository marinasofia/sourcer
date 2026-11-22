"""Company websites for companies discovered without one.

The board finder reads a company's own careers page first when it knows the
website, so a website is worth finding before the probe runs. A website comes
from data discovery already holds when it can (an apply link on the company's
own careers host), otherwise from one profile page that names it: the Built In
company page, or the Simplify company page. A job board host, a directory, or
a social network is never stored as a website, and a stored domain is never
replaced.
"""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .. import db
from ..net import SafeSession
from ..probe import RateLimiter, Requester, Response

log = logging.getLogger(__name__)

# a company with no website found is tried again after RETRY_DAYS, at most MAX_ATTEMPTS times
RETRY_DAYS = 30
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT_SECONDS = 10
# companies looked up at once; each host still gets one request a second
WORKERS = 4
USER_AGENT = {"User-Agent": "Sourcer website finder/0.1 (+https://github.com/marinasofia/sourcer)"}
SIMPLIFY_PAGE = "https://simplify.jobs/c/{slug}"

# hosts that serve many companies: job boards and applicant tracking systems
BOARD_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "myworkdaysite.com",
    "workday.com", "workable.com", "smartrecruiters.com", "rippling.com", "icims.com",
    "jobvite.com", "oraclecloud.com", "taleo.net", "successfactors.com", "successfactors.eu",
    "sapsf.com", "ultipro.com", "ukg.com", "adp.com", "paylocity.com", "paycomonline.net",
    "bamboohr.com", "breezy.hr", "applytojob.com", "jazzhr.com", "recruitee.com",
    "teamtailor.com", "personio.de", "personio.com", "eightfold.ai", "phenompeople.com",
    "dayforcehcm.com", "avature.net", "brassring.com", "kenexa.com", "csod.com",
    "pinpointhq.com", "gem.com", "dover.com", "trinethire.com", "hire.withgoogle.com",
    "careers-page.com", "comeet.com", "workatastartup.com", "wellfound.com", "angel.co",
    "ycombinator.com", "simplify.jobs", "builtin.com", "getro.com", "indeed.com",
    "glassdoor.com", "ziprecruiter.com", "joinhandshake.com", "monster.com", "dice.com",
    "otta.com", "welcometothejungle.com", "notion.site", "google.com", "goo.gl", "bit.ly",
    "t.co", "lnkd.in",
)  # fmt: skip
SOCIAL_HOSTS = (
    "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com", "youtube.com",
    "youtu.be", "tiktok.com", "github.com", "medium.com", "crunchbase.com", "pitchbook.com",
    "threads.net", "reddit.com", "discord.gg", "discord.com", "apps.apple.com",
    "play.google.com", "wikipedia.org", "bsky.app",
)  # fmt: skip
# Built In runs one site per city (builtinnyc.com, builtinchicago.org, builtin.com)
BUILTIN_HOST_RE = re.compile(r"^(?:www\.)?builtin[a-z]*\.(?:com|org)$")
# leading labels of a careers host: jobs.example.com is example.com's
CAREER_LABELS = {"jobs", "job", "careers", "career", "apply", "join", "work", "talent", "hiring"}
SECOND_LEVEL = {"com", "co", "org", "net", "gov", "ac", "edu"}
SUFFIX_WORDS = {"inc", "corp", "llc", "ltd", "co", "the", "company", "corporation", "group"}


def _host(url: str) -> str | None:
    parsed = urlparse(url if "://" in url else f"//{url}")
    try:
        host = parsed.hostname
    except ValueError:
        return None
    return host.lower().rstrip(".").removeprefix("www.") if host else None


def _under(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes)


def blocked(host: str) -> bool:
    """A host that is not one company's website."""
    return (
        _under(host, BOARD_HOSTS)
        or _under(host, SOCIAL_HOSTS)
        or BUILTIN_HOST_RE.match(host) is not None
    )


def website_domain(url: str | None) -> str | None:
    """The domain a website link names, or None for a board, a directory, a
    social network, an address, or anything that is not a public name."""
    host = _host(url or "")
    if not host or "." not in host or blocked(host):
        return None
    labels = host.split(".")
    if not re.fullmatch(r"[a-z]{2,}", labels[-1]):  # an IP address or a bare label
        return None
    if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) for label in labels):
        return None
    return host


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower().replace("&", "and"))


def names_match(name: str, domain: str) -> bool:
    """The domain's name part and the company name share their core: "tiktok"
    in lifeattiktok.com, "l3harris" in L3Harris Technologies."""
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in SUFFIX_WORDS]
    core = "".join(words)
    labels = domain.split(".")[:-1]
    while len(labels) > 1 and labels[-1] in SECOND_LEVEL:  # example.com.au
        labels.pop()
    label = _compact(labels[-1]) if labels else ""
    if len(core) >= 3 and core in label:
        return True
    return len(label) >= 4 and label in _compact(name)


def apply_url_domain(url: str, name: str) -> str | None:
    """The company website an apply link implies, with no request: the link's
    host when it is the company's own careers host (jobs.example.com gives
    example.com) and the host carries the company's name."""
    host = website_domain(url)
    if host is None:
        return None
    labels = host.split(".")
    while len(labels) > 2 and labels[0] in CAREER_LABELS:
        labels.pop(0)
    domain = ".".join(labels)
    return domain if names_match(name, domain) else None


def _first_website(links: list[str]) -> str | None:
    return next((domain for link in links if (domain := website_domain(link))), None)


def builtin_website(html: str) -> str | None:
    """The "View Website" link of a Built In company page."""
    soup = BeautifulSoup(html, "html.parser")
    links = [
        str(anchor.get("href"))
        for anchor in soup.find_all("a", href=True)
        if "utm_source=builtin" in str(anchor.get("href")).lower()
        or "view website" in anchor.get_text(" ", strip=True).lower()
    ]
    return _first_website(links)


def _organization(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Organization":
            return data
    return {}


def simplify_website(html: str, name: str | None = None) -> str | None:
    """The website a Simplify company page names: its structured data first,
    then its website link. With ``name``, the page must be that company's."""
    soup = BeautifulSoup(html, "html.parser")
    org = _organization(soup)
    if name is not None and _compact(str(org.get("name") or "")) != _compact(name):
        return None
    same_as = org.get("sameAs") or []
    links = [str(link) for link in same_as] if isinstance(same_as, list) else [str(same_as)]
    links += [
        str(anchor.get("href"))
        for anchor in soup.find_all("a", href=True)
        if "utm_source=simplify" in str(anchor.get("href")).lower()
    ]
    return _first_website(links)


def builtin_company_link(html: str, page_url: str, name: str) -> str | None:
    """On a Built In job page, the link to the company's own profile. The page
    also links similar companies, so the profile slug must be the name's."""
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        url = urljoin(page_url, str(anchor.get("href")))
        match = re.fullmatch(r"/company/([\w-]+)/?", urlparse(url).path)
        if match and _compact(match.group(1)) == _compact(name):
            return url
    return None


def simplify_slug(name: str) -> str:
    """The page name Simplify most often gives a company: words joined by hyphens."""
    return "-".join(re.findall(r"[A-Za-z0-9]+", name))


@dataclass(frozen=True)
class WebsiteResult:
    name: str
    domain: str | None
    found_at: str | None  # the link or page the domain came from
    requests: int


def _source_urls(row: dict[str, Any]) -> list[str]:
    try:
        signals = json.loads(row.get("quality_signals") or "{}")
    except (TypeError, json.JSONDecodeError):
        return []
    urls = signals.get("source_urls") if isinstance(signals, dict) else None
    return [str(url) for url in urls or [] if url]


def _get(requester: Requester, limiter: RateLimiter, url: str) -> Response | None:
    limiter.wait(_host(url) or "")
    try:
        response = requester.get(
            url, timeout=REQUEST_TIMEOUT_SECONDS, headers=USER_AGENT, allow_redirects=True
        )
    except (requests.RequestException, ValueError):
        return None
    return response if response.status_code == 200 else None


def find_website(row: dict[str, Any], requester: Requester, limiter: RateLimiter) -> WebsiteResult:
    """The cheapest source first: stored apply links (no request), then the
    Built In profile, then the Simplify profile, then a Built In job page's
    link to the profile (two requests)."""
    name = str(row["name"])
    urls = _source_urls(row)
    for url in urls:
        if domain := apply_url_domain(url, name):
            return WebsiteResult(name, domain, url, 0)
    sent = 0
    builtin = [url for url in urls if BUILTIN_HOST_RE.match(urlparse(url).hostname or "")]
    profiles = [url for url in builtin if re.match(r"/company/[\w-]+/?$", urlparse(url).path)]
    for url in profiles[:1]:
        sent += 1
        response = _get(requester, limiter, url)
        if response is not None and (domain := builtin_website(response.text)):
            return WebsiteResult(name, domain, url, sent)
    if "simplify" in str(row.get("source") or "") and (slug := simplify_slug(name)):
        url = SIMPLIFY_PAGE.format(slug=slug)
        sent += 1
        response = _get(requester, limiter, url)
        if response is not None and (domain := simplify_website(response.text, name)):
            return WebsiteResult(name, domain, url, sent)
    jobs = [url for url in builtin if urlparse(url).path.startswith("/job/")]
    if not profiles and jobs:
        sent += 1
        response = _get(requester, limiter, jobs[0])
        profile = builtin_company_link(response.text, jobs[0], name) if response else None
        if profile is not None:
            sent += 1
            page = _get(requester, limiter, profile)
            if page is not None and (domain := builtin_website(page.text)):
                return WebsiteResult(name, domain, profile, sent)
    return WebsiteResult(name, None, None, sent)


def attempts(row: dict[str, Any]) -> int:
    return int(row.get("domain_attempts") or 0)


def due(row: dict[str, Any], now: datetime) -> bool:
    """A company with no website is due when never looked up, or when its last
    lookup is RETRY_DAYS old and it has had fewer than MAX_ATTEMPTS."""
    if row.get("domain"):
        return False
    count = attempts(row)
    if count == 0:
        return True
    try:
        last = datetime.fromisoformat(str(row.get("domain_checked_at") or ""))
    except ValueError:
        return count < MAX_ATTEMPTS
    last = last if last.tzinfo else last.replace(tzinfo=UTC)
    return count < MAX_ATTEMPTS and now - last >= timedelta(days=RETRY_DAYS)


def backfill_domains(
    conn: db.Connection,
    limit: int = 25,
    *,
    minutes: float | None = None,
    workers: int = WORKERS,
    now: datetime | None = None,
    clock: Any = time.monotonic,
    requester: Requester | None = None,
    limiter: RateLimiter | None = None,
) -> list[WebsiteResult]:
    """Look up websites for up to ``limit`` due companies, never looked up
    first. With ``minutes``, no new company starts after that budget. Every
    lookup is recorded, found or not, so a company that names no website is
    not fetched again until its retry is due."""
    if limit < 0:
        raise ValueError("backfill limit cannot be negative")
    moment = now or datetime.now(UTC)
    rows = sorted(
        (row for row in db.list_companies(conn) if due(row, moment)),
        key=lambda row: (attempts(row), str(row.get("domain_checked_at") or "")),
    )[:limit]
    deadline = clock() + minutes * 60 if minutes else None
    client = requester if requester is not None else cast(Requester, SafeSession())
    throttle = limiter or RateLimiter()

    def one(row: dict[str, Any]) -> WebsiteResult | None:
        if deadline is not None and clock() >= deadline:
            return None
        try:
            return find_website(row, client, throttle)
        except Exception as e:
            log.warning(
                "website lookup failed for %s: %s: %s", row.get("name"), type(e).__name__, e
            )
            return None

    results = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for result in pool.map(one, rows):
            if result is None:
                continue
            db.save_company_domain(conn, result.name, result.domain)
            results.append(result)
    return results
