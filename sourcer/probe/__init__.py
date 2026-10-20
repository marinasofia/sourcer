"""Discover a company's applicant tracking system and careers page.

The probe checks one URL per second, follows a fixed platform order, and stops
at the first HTTP 200 page that names the company and lists at least one job.
When no guessed slug works, the company's own careers page is read for a link
to a hosted board (Greenhouse, Lever, Ashby, Workday and the rest): a link on
the company's own site is the strongest proof the board is theirs.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .. import db
from ..net import SafeSession

REQUEST_INTERVAL_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 20
USER_AGENT = {"User-Agent": "Sourcer ATS probe/0.2 (+https://github.com/marinasofia/sourcer)"}
SUFFIXES = (
    "incorporated",
    "corporation",
    "company",
    "technologies",
    "technology",
    "labs",
    "inc",
    "corp",
    "ai",
    "hq",
    "co",
)


@dataclass(frozen=True)
class Platform:
    ats_type: str
    url_template: str


PLATFORMS = (
    Platform("ashby", "https://jobs.ashbyhq.com/{slug}"),
    Platform("greenhouse", "https://boards.greenhouse.io/{slug}"),
    Platform("lever", "https://jobs.lever.co/{slug}"),
    Platform("workable", "https://apply.workable.com/{slug}"),
    Platform("smartrecruiters", "https://careers.smartrecruiters.com/{slug}"),
    Platform("rippling", "https://ats.rippling.com/{slug}"),
)
VERIFICATION_URLS = {
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json&limit=1",
    "workable": "https://apply.workable.com/{slug}/llms.txt",
}
GENERIC_PATHS = ("careers", "jobs", "join")
# where a hosted board's address names its slug; Workday needs tenant, pod and site
LINKED_BOARDS = (
    (
        "greenhouse",
        re.compile(
            r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([\w-]+)"
        ),
    ),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([\w-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([\w.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([\w-]+)")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)")),
    ("rippling", re.compile(r"ats\.rippling\.com/([\w-]+)")),
    (
        "workday",
        re.compile(
            r"(?<![a-z0-9-])([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)"
        ),
    ),
    ("eightfold", re.compile(r"(?<![a-z0-9-])([a-z0-9-]+)\.eightfold\.ai")),
    (
        "oracle",
        re.compile(
            r"(?<![a-z0-9-])([a-z0-9-]+\.fa(?:\.[a-z0-9]+)?\.oraclecloud\.com)"
            r"(?:/hcmUI/CandidateExperience/[a-z-]+/sites/([\w-]+))?"
        ),
    ),
)
NOT_SLUGS = {"embed", "js", "v1", "api", "jobs", "job", "careers", "static", "assets", "wday"}
KNOWN_HOSTS = {urlparse(platform.url_template.format(slug="x")).hostname for platform in PLATFORMS}


class Response(Protocol):
    status_code: int
    text: str
    url: str


class Requester(Protocol):
    def get(self, url: str, **kwargs: Any) -> Response: ...


@dataclass(frozen=True)
class ProbeTarget:
    name: str
    domains: tuple[str, ...]
    slugs: tuple[str, ...]
    # the domain came from the company's record, not guessed from its name
    known_site: bool = False


@dataclass(frozen=True)
class ProbeResult:
    name: str
    domain: str | None
    ats_type: str | None
    ats_slug: str | None
    hit_url: str | None
    urls_tried: tuple[str, ...]

    @property
    def status(self) -> str:
        return "resolved" if self.ats_type else "unresolved"


class RateLimiter:
    def __init__(
        self,
        interval: float = REQUEST_INTERVAL_SECONDS,
        clock: Any = time.monotonic,
        sleep: Any = time.sleep,
    ) -> None:
        self.interval = interval
        self.clock = clock
        self.sleep = sleep
        self.last_request: float | None = None

    def wait(self) -> None:
        now = self.clock()
        if self.last_request is not None:
            remaining = self.interval - (now - self.last_request)
            if remaining > 0:
                self.sleep(remaining)
                now = self.clock()
        self.last_request = now


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower().replace("&", "and"))


def _slug(value: str) -> str:
    """Normalize an existing board slug while preserving meaningful hyphens."""
    normalized = re.sub(r"[^a-z0-9-]", "", value.lower().replace("&", "and"))
    return re.sub(r"-+", "-", normalized).strip("-")


def _without_suffix(words: list[str]) -> list[str]:
    trimmed = list(words)
    while trimmed and _compact(trimmed[-1]) in SUFFIXES:
        trimmed.pop()
    return trimmed


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _input_parts(value: str) -> tuple[str, str | None, str | None]:
    parsed = urlparse(value if "://" in value else f"//{value}")
    if parsed.hostname and ("://" in value or "." in value):
        host = parsed.hostname.lower().removeprefix("www.")
        path_slug = next((part for part in parsed.path.split("/") if part), None)
        if host in KNOWN_HOSTS and path_slug:
            return path_slug, None, path_slug
        label = host.split(".")[0]
        return label, host, None
    return value.strip(), None, None


def build_target(value: str, known_domain: str | None = None) -> ProbeTarget:
    """Derive ordered slug and domain candidates from a company name or URL."""
    name, input_domain, path_slug = _input_parts(value)
    words = re.findall(r"[a-z0-9]+", name.lower().replace("&", " and "))
    stripped = _without_suffix(words)
    slugs = [_slug(path_slug or ""), _compact("".join(words)), _compact("".join(stripped))]
    domain = input_domain or known_domain
    domains: tuple[str, ...]
    if domain:
        host = urlparse(domain if "://" in domain else f"//{domain}").hostname or domain
        host = host.lower().removeprefix("www.")
        label_words = re.findall(r"[a-z0-9]+", host.split(".")[0])
        slugs.extend(
            (_compact("".join(label_words)), _compact("".join(_without_suffix(label_words))))
        )
        domains = (host,)
    else:
        domains = (f"{slug}.com",) if (slug := next((item for item in slugs if item), "")) else ()
    if path_slug is None:
        bases = list(_unique(slugs))
        slugs.extend(
            f"{base}{suffix}"
            for base in bases
            for suffix in ("inc", "labs", "ai", "hq")
            if not base.endswith(suffix)
        )
    return ProbeTarget(
        name=name.strip() or value,
        domains=_unique(list(domains)),
        slugs=_unique(slugs),
        known_site=bool(domain),
    )


def _listing_links(response: Response) -> list[str]:
    soup = BeautifulSoup(response.text, "html.parser")
    return [
        urljoin(response.url, str(anchor.get("href"))) for anchor in soup.find_all("a", href=True)
    ]


def has_job_listing(response: Response, ats_type: str) -> bool:
    """Require HTTP 200 and a platform-shaped job link or JobPosting data."""
    if response.status_code != 200:
        return False
    if re.search(r'"@type"\s*:\s*"jobposting"', response.text, re.I):
        return True
    links = [urlparse(link) for link in _listing_links(response)]

    def host_is(link: Any, host: str) -> bool:
        return (link.hostname or "").lower() == host

    predicates = {
        "ashby": lambda link: (
            host_is(link, "jobs.ashbyhq.com")
            and len([part for part in link.path.split("/") if part]) >= 2
        ),
        "greenhouse": lambda link: (
            (link.hostname or "").lower().endswith("greenhouse.io")
            and bool(re.search(r"/jobs?/[^/]+", link.path.lower()))
        ),
        "lever": lambda link: (
            host_is(link, "jobs.lever.co")
            and len([part for part in link.path.split("/") if part]) >= 2
        ),
        "workable": lambda link: (
            host_is(link, "apply.workable.com")
            and bool(re.search(r"/(?:j|jobs)/[^/]+", link.path.lower()))
        ),
        "smartrecruiters": lambda link: (
            host_is(link, "jobs.smartrecruiters.com")
            and len([part for part in link.path.split("/") if part]) >= 2
        ),
        "rippling": lambda link: (
            host_is(link, "ats.rippling.com")
            and bool(re.search(r"/jobs?/[^/]+", link.path.lower()))
        ),
        "careers_page": lambda link: bool(
            re.search(r"/(?:careers|jobs|join)/[^/]+", link.path.lower())
        ),
    }
    return any(predicates[ats_type](link) for link in links)


_LEGAL_SUFFIX_RE = re.compile(rf"[,\s]+(?:{'|'.join(SUFFIXES)}|llc|ltd)\.?$", re.I)


def board_owned_by(response: Response, target: ProbeTarget, *, own_domain: bool = False) -> bool:
    """The page names the company, so a guessed slug did not land on another
    company's board. Evidence: the company's name on the page ("Jobs at Rent
    the Runway", or the board's data on script rendered boards), or, for a
    hosted board, a link or redirect to the company's domain. A careers page
    always links its own domain, so there only the name counts."""
    if response.status_code != 200:
        return False
    names = {target.name.strip(), _LEGAL_SUFFIX_RE.sub("", target.name.strip())} - {""}
    for name in names:
        # as a word, not inside a link or address ("https://testco.com")
        if re.search(rf"(?<![\w/.@]){re.escape(name)}(?!\w|\.[a-z])", response.text, re.I):
            return True
    if own_domain:
        return False
    final_host = (urlparse(response.url).hostname or "").lower()
    return any(domain in response.text or final_host.endswith(domain) for domain in target.domains)


def _verification_has_openings(response: Response, ats_type: str) -> bool:
    if response.status_code != 200:
        return False
    if ats_type == "workable":
        match = re.search(r"\b(\d+)\s+current openings\b", response.text, re.I)
        return match is not None and int(match.group(1)) > 0
    try:
        payload = json.loads(response.text)
    except (TypeError, json.JSONDecodeError):
        return False
    if ats_type in {"ashby", "greenhouse"}:
        return isinstance(payload, dict) and bool(payload.get("jobs"))
    if ats_type == "lever":
        return isinstance(payload, list) and bool(payload)
    return False


def _request(requester: Requester, limiter: RateLimiter, url: str) -> Response | None:
    limiter.wait()
    try:
        return requester.get(
            url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers=USER_AGENT,
            allow_redirects=True,
        )
    except (requests.RequestException, ValueError):
        return None


def linked_boards(text: str) -> list[tuple[str, str]]:
    """Hosted boards a page links to, as (ats type, slug), in order, once each."""
    found: list[tuple[str, str]] = []
    for ats_type, pattern in LINKED_BOARDS:
        for match in pattern.finditer(text):
            slug = "/".join(part for part in match.groups() if part)
            if ats_type == "oracle" and match.group(2) is None:
                slug += "/CX_1"  # the career site most tenants use
            if match.group(1).lower() in NOT_SLUGS or (ats_type, slug) in found:
                continue
            found.append((ats_type, slug))
    return found


def _board_reads(ats_type: str, slug: str, name: str) -> bool:
    """The adapter can read the board: the slug is real, not a page fragment."""
    from ..sources import fetch_postings  # noqa: PLC0415  (sources is heavy, probe is not)

    try:
        fetch_postings({"company": name, "ats": ats_type, "slug": slug})
    except (requests.RequestException, ValueError, KeyError):
        return False
    return True


def _probe_site(
    target: ProbeTarget,
    client: Requester,
    throttle: RateLimiter,
    tried: list[str],
    read_board: Any,
) -> ProbeResult | None:
    """The company's own site: a hosted board it links to, else a careers page."""
    for domain in target.domains:
        for path in (*GENERIC_PATHS, ""):
            url = f"https://{domain}/{path}"
            tried.append(url)
            response = _request(client, throttle, url)
            if response is None or response.status_code != 200:
                continue
            for ats_type, found in linked_boards(f"{response.url} {response.text}"):
                # an Eightfold site is searched by the domain of the company it serves
                slug = f"{found}/{domain}" if ats_type == "eightfold" else found
                if read_board(ats_type, slug, target.name):
                    return ProbeResult(target.name, domain, ats_type, slug, url, tuple(tried))
            if (
                path
                and has_job_listing(response, "careers_page")
                and board_owned_by(response, target, own_domain=True)
            ):
                return ProbeResult(target.name, domain, "careers_page", path, url, tuple(tried))
    return None


def probe_target(
    target: ProbeTarget,
    requester: Requester | None = None,
    limiter: RateLimiter | None = None,
    board_reader: Any = None,
) -> ProbeResult:
    """The company's own site first when its domain is known, else hosted boards in
    fixed order, then the site; stop at the first verified listing."""
    client = requester if requester is not None else cast(Requester, SafeSession())
    throttle = limiter or RateLimiter()
    read_board = board_reader or _board_reads
    tried: list[str] = []
    # a known site usually links its board: a few requests instead of dozens of guesses
    if target.known_site:
        found = _probe_site(target, client, throttle, tried, read_board)
        if found is not None:
            return found
    for platform in PLATFORMS:
        for slug in target.slugs:
            url = platform.url_template.format(slug=slug)
            tried.append(url)
            response = _request(client, throttle, url)
            if response is not None and not board_owned_by(response, target):
                continue  # not a board, or a namesake's board
            if response is not None and has_job_listing(response, platform.ats_type):
                return ProbeResult(
                    target.name,
                    target.domains[0] if target.domains else None,
                    platform.ats_type,
                    slug,
                    url,
                    tuple(tried),
                )
            if response is not None and response.status_code == 200:
                verification_template = VERIFICATION_URLS.get(platform.ats_type)
                if verification_template is None:
                    continue
                verification_url = verification_template.format(slug=slug)
                tried.append(verification_url)
                verification = _request(client, throttle, verification_url)
                if verification is not None and _verification_has_openings(
                    verification, platform.ats_type
                ):
                    return ProbeResult(
                        target.name,
                        target.domains[0] if target.domains else None,
                        platform.ats_type,
                        slug,
                        url,
                        tuple(tried),
                    )
    if not target.known_site:
        found = _probe_site(target, client, throttle, tried, read_board)
        if found is not None:
            return found
    return ProbeResult(
        target.name,
        target.domains[0] if target.domains else None,
        None,
        None,
        None,
        tuple(tried),
    )


def probe_pending(conn: db.Connection, limit: int = 25) -> list[ProbeResult]:
    if limit < 0:
        raise ValueError("probe limit cannot be negative")
    rows = sorted(
        (row for row in db.list_companies(conn) if not row.get("ats_type")),
        key=lambda row: row.get("probed_at") or "",
    )
    limiter = RateLimiter()
    results = []
    for row in rows[:limit]:
        result = probe_target(build_target(row["name"], row.get("domain")), limiter=limiter)
        db.save_company_probe(conn, result.__dict__)
        results.append(result)
    return results


def _save(conn: db.Connection, result: ProbeResult) -> None:
    db.save_company_probe(conn, result.__dict__)


def _print_results(results: list[ProbeResult]) -> None:
    for result in results:
        found = f"{result.ats_type}:{result.ats_slug}" if result.ats_type else "unresolved"
        print(
            f"{result.name}\t{found}\t{result.hit_url or ''}\t{len(result.urls_tried)} URLs tried"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sourcer probe", description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("company", nargs="?", help="company name or careers URL")
    group.add_argument("--all", action="store_true", help="probe every company with no ATS type")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    conn = db.connect()
    limiter = RateLimiter()
    if args.all:
        rows = [row for row in db.list_companies(conn) if not row.get("ats_type")]
        targets = [build_target(row["name"], row.get("domain")) for row in rows]
    else:
        targets = [build_target(args.company)]
    results = [probe_target(target, limiter=limiter) for target in targets]
    for result in results:
        _save(conn, result)
    _print_results(results)
    return 0 if all(result.ats_type for result in results) else 1
