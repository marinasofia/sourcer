"""Polite fetching for link sources: robots.txt first, a User-Agent that
names Sourcer, page and byte caps, a timeout, and no redirect to another host.

Every request (robots.txt included) goes through ``net.get``, which allows
only public HTTPS addresses, and is written to the onboarding call log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from .. import __version__, config, net
from .store import audit

PRODUCT = "Sourcer"
USER_AGENT = f"{PRODUCT}/{__version__} (+https://github.com/marinasofia/sourcer)"
MAX_BYTES = 2_000_000  # one page or API reply
MAX_REDIRECTS = 3
ROBOTS_REDIRECTS = 5  # RFC 9309: follow at least five
MAX_RULES = 1000  # per robots.txt group
MAX_RULE_CHARS = 512
NOISE_TAGS = ("script", "style", "noscript", "template", "svg", "nav", "footer", "header",
              "form", "iframe", "button")  # fmt: skip


class Blocked(Exception):
    """A fetch Sourcer would not make or finish; the message is for the candidate."""


# ---------------------------------------------------------------- robots.txt


@dataclass
class Robots:
    """The rules of one robots.txt for Sourcer (RFC 9309): the group naming
    Sourcer, else the ``*`` group; the longest matching rule wins and allow
    wins a tie. ``deny_all`` when robots.txt could not be read (5xx)."""

    rules: list[tuple[bool, str]] = field(default_factory=list)  # (allow, pattern)
    deny_all: bool = False

    @classmethod
    def parse(cls, text: str) -> Robots:
        groups: dict[str, list[tuple[bool, str]]] = {}
        agents: list[str] = []
        in_rules = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if in_rules:
                    agents, in_rules = [], False
                agents.append(value.lower())
                for agent in agents:
                    groups.setdefault(agent, [])
            elif key in ("allow", "disallow") and agents:
                in_rules = True
                if value and len(value) <= MAX_RULE_CHARS:  # an empty Disallow allows all
                    for agent in agents:
                        if len(groups[agent]) < MAX_RULES:
                            groups[agent].append((key == "allow", value))
        mine = groups.get(PRODUCT.lower())
        return cls(mine if mine is not None else groups.get("*", []))

    def allows(self, url: str) -> bool:
        if self.deny_all:
            return False
        parsed = urlparse(url)
        path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
        best: tuple[int, bool] | None = None
        for allow, pattern in self.rules:
            if rule_matches(pattern, path):
                rank = (len(pattern), allow)
                best = rank if best is None or rank > best else best
        return True if best is None else best[1]


def rule_matches(pattern: str, path: str) -> bool:
    """Does a robots.txt rule match the start of ``path``? ``*`` is any run of
    characters and a final ``$`` anchors the end. A greedy two pointer match
    with no regex, so a hostile rule cannot make it backtrack for long."""
    if pattern.endswith("$"):
        pattern = pattern[:-1]
    else:
        pattern += "*"  # a rule matches a prefix of the path
    p = s = 0
    star, mark = -1, 0
    while s < len(path):
        if p < len(pattern) and pattern[p] == "*":
            star, mark = p, s
            p += 1
        elif p < len(pattern) and pattern[p] == path[s]:
            p += 1
            s += 1
        elif star >= 0:
            p, mark = star + 1, mark + 1
            s = mark
        else:
            return False
    return all(c == "*" for c in pattern[p:])


# ---------------------------------------------------------------- fetching


class Fetcher:
    """Fetches for one source: robots.txt checked once per host, every call logged."""

    def __init__(self, source_id: str) -> None:
        self.source_id = source_id
        self.robots: dict[str, Robots] = {}
        self.last_url = ""  # where the last get() ended, after same host redirects

    def _raw(self, url: str, accept: str) -> requests.Response:
        """One GET, logged whether it succeeds or fails (0 characters then)."""
        size = 0
        try:
            resp = net.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": accept},
                timeout=config.REQUEST_TIMEOUT,
                allow_redirects=False,
                max_bytes=MAX_BYTES + 1,
            )
            size = len(resp.content or b"")
        finally:
            parsed = urlparse(url)
            audit(str(parsed.hostname), f"GET {parsed.path or '/'}", [self.source_id], size)
        return resp

    def _robots(self, url: str) -> Robots:
        host = str(urlparse(url).hostname)
        if host not in self.robots:
            self.robots[host] = self._read_robots(f"https://{host}/robots.txt")
        return self.robots[host]

    def _read_robots(self, url: str) -> Robots:
        """robots.txt by RFC 9309: redirects followed (up to five, any public
        HTTPS host), 4xx means no rules, 5xx, 429 or no answer means read nothing."""
        for _ in range(ROBOTS_REDIRECTS + 1):
            try:
                resp = self._raw(url, "text/plain")
            except requests.RequestException:
                return Robots(deny_all=True)
            location = resp.headers.get("Location")
            if 300 <= resp.status_code < 400 and location:
                url = urljoin(url, location)
                continue
            if resp.status_code >= 500 or resp.status_code == 429:
                return Robots(deny_all=True)
            if resp.status_code >= 300:
                return Robots()  # missing: no rules
            return Robots.parse(resp.text[:MAX_BYTES])
        return Robots(deny_all=True)

    def get(self, url: str, accept: str = "text/html,application/xhtml+xml") -> requests.Response:
        """GET ``url`` after robots.txt allows it. Redirects are followed only
        on the same host. Blocked with a reason the candidate can act on."""
        host = urlparse(url).hostname
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            robots = self._robots(current)
            if robots.deny_all:
                raise Blocked(
                    f"robots.txt on {host} could not be read, so Sourcer reads nothing there"
                )
            if not robots.allows(current):
                raise Blocked(f"robots.txt on {host} asks tools not to read {current}")
            try:
                resp = self._raw(current, accept)
            except requests.RequestException as exc:
                raise Blocked(f"could not read {current} ({type(exc).__name__})") from exc
            if 300 <= resp.status_code < 400 and resp.headers.get("Location"):
                target = urljoin(current, resp.headers["Location"])
                if urlparse(target).hostname != host:
                    raise Blocked(
                        f"{current} moves to {urlparse(target).hostname}, another site; "
                        "add that link instead"
                    )
                current = target
                continue
            if resp.status_code >= 300:  # a 3xx with no Location, or 4xx and 5xx
                raise Blocked(f"{host} answered {resp.status_code} for {current}")
            if len(resp.content or b"") > MAX_BYTES:
                raise Blocked(f"{current} is over {MAX_BYTES:,} bytes, not read")
            self.last_url = current
            return resp
        raise Blocked(f"{current} redirects too many times")

    def json(self, url: str) -> Any:
        try:
            return self.get(url, accept="application/json").json()
        except ValueError as exc:
            raise Blocked(f"{urlparse(url).hostname} did not answer with JSON") from exc


# ---------------------------------------------------------------- page text


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def meta(doc: BeautifulSoup, *names: str) -> str:
    """The first non empty meta content among ``names`` (name or property)."""
    for name in names:
        tag = doc.find("meta", attrs={"name": name}) or doc.find("meta", attrs={"property": name})
        content = tag.get("content") if isinstance(tag, Tag) else None
        if isinstance(content, str) and content.strip():
            return " ".join(content.split())
    return ""


def title(doc: BeautifulSoup) -> str:
    return " ".join(doc.title.get_text().split()) if doc.title else ""


def main_text(doc: BeautifulSoup, limit: int) -> str:
    """Readable text of the page's main part, without menus, scripts or forms."""
    for tag in doc.find_all(NOISE_TAGS):
        tag.decompose()
    root = doc.find("main") or doc.find("article") or doc.body or doc
    lines = [" ".join(line.split()) for line in root.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line)[:limit]


def same_host_links(doc: BeautifulSoup, base: str, skip: tuple[str, ...]) -> list[str]:
    """Distinct HTTPS links on the same host as ``base``, in page order."""
    host = urlparse(base).hostname
    out: list[str] = []
    for a in doc.find_all("a", href=True):
        href = a.get("href")
        if not isinstance(href, str):
            continue
        url = urljoin(base, href).split("#", 1)[0]
        parsed = urlparse(url)
        if (
            parsed.scheme == "https"
            and parsed.hostname == host
            and not parsed.path.lower().endswith(skip)
            and url.rstrip("/") != base.rstrip("/")
            and url not in out
        ):
            out.append(url)
    return out
