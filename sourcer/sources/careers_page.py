"""Read-only browser fallback for careers pages and Workday sites."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import os
import re
import threading
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from .. import net
from ..net import validate_public_https_url
from ._common import Posting, clean_html

CAREER_PATHS = ("careers", "jobs", "join")
JOB_PATH_RE = re.compile(
    r"/(?:job|jobs|career|careers|positions?|openings?)/(?:[^/?#]+/)*[^/?#]+"
    r"|/(?:careers?|jobs?)-(?:positions?-)?[\w-]{6,}",  # /careers-positions-data-analyst
    re.I,
)
# Hosted job boards with no API Sourcer reads: the browser opens them instead
HOSTED_BOARD_RE = re.compile(
    r"https://(?:jobs\.gem\.com/[\w-]+|www\.ycombinator\.com/companies/[\w-]+/jobs"
    r"|apply\.workable\.com/[\w-]+|[\w-]+\.teamtailor\.com/jobs|ats\.rippling\.com/[\w-]+/jobs"
    r"|[\w-]+\.bamboohr\.com/careers|[\w-]+\.recruitee\.com|wellfound\.com/company/[\w-]+/jobs)/?",
    re.I,
)
# A word nearly every job title has; a list of only categories or cities has none
ROLE_WORD_RE = re.compile(
    r"\b(engineer|developer|scientist|analyst|manager|lead|head|director|designer|associate|"
    r"specialist|intern|representative|executive|technician|architect|researcher|consultant|"
    r"coordinator|officer|strategist|recruiter|counsel|staff|founding|operator|writer|advisor|"
    r"administrator|accountant|marketer|partner|creator|product|sales|account|support|"
    r"operations|chief|vp|president)",
    re.I,
)
DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
# link text that leads to jobs but is not one ("See open roles", "Browse jobs")
# matched against the text with punctuation and spacing folded to single
# spaces (nav_title), so no two parts compete for the same characters
NAV_TITLE_RE = re.compile(
    r"(?:(?:see|view|browse|explore|search|find|read|learn|apply|join|check out|show) )?"
    r"(?:(?:all|our|the|current) )?(?:(?:open|available) )?"
    r"(?:jobs?|roles?|positions?|openings?|opportunities|careers?|more|now|team|us)"
    r"(?: (?:and )?apply)?",
    re.I,
)
NAV_TITLE_MAX = 60  # longer link text is not a "See open roles" link


def nav_title(title: str) -> bool:
    """Link text that leads to jobs but is not one."""
    if len(title) > NAV_TITLE_MAX * 4:
        return False
    folded = re.sub(r"[\W_]+", " ", title).strip()
    return len(folded) <= NAV_TITLE_MAX and bool(NAV_TITLE_RE.fullmatch(folded))


ROLES_LINK_RE = re.compile(
    r"\b(open (?:roles|positions|jobs)|all (?:jobs|roles|positions|openings)|job openings|"
    r"(?:see|view|browse|explore|search) (?:all |our |open )?(?:jobs|roles|positions|openings)|"
    r"current openings|career opportunities)\b",
    re.I,
)
# A job board Sourcer reads through its API, linked or embedded on a careers page
BOARD_LINKS = (
    (
        "greenhouse",
        re.compile(
            r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?(?P<slug>[\w-]+)",
            re.I,
        ),
    ),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/(?P<slug>[\w.%-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/(?P<slug>[\w.-]+)", re.I)),
    (
        "workday",
        re.compile(
            r"(?<![\w-])(?P<slug>[\w-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[\w-]+)"
        ),
    ),
)
BOARD_SLUG_NOISE = frozenset({"embed", "api", "v1", "jobs", "js", "static", "assets"})


def _jsonld_postings(soup: BeautifulSoup, page_url: str) -> list[Posting]:
    out = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or "null")
        except json.JSONDecodeError:
            continue
        values = payload if isinstance(payload, list) else [payload]
        if isinstance(payload, dict) and isinstance(payload.get("@graph"), list):
            values.extend(payload["@graph"])
        for value in values:
            if not isinstance(value, dict) or value.get("@type") != "JobPosting":
                continue
            location = _location_text(
                value.get("jobLocation") or value.get("applicantLocationRequirements") or ""
            )
            out.append(
                {
                    "title": str(value.get("title") or "").strip(),
                    "location": location,
                    "url": urljoin(page_url, str(value.get("url") or page_url)),
                    "description": clean_html(str(value.get("description") or "")),
                    "employment_type": str(value.get("employmentType") or "")
                    .lower()
                    .replace("_", " "),
                    "posted_at": str(value.get("datePosted") or "")[:10] or None,
                }
            )
    return out


def _location_text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(filter(None, (_location_text(item) for item in value)))
    if not isinstance(value, dict):
        return str(value)
    address = value.get("address", value)
    if not isinstance(address, dict):
        return str(address)
    fields = ("addressLocality", "addressRegion", "addressCountry", "name")
    return ", ".join(str(address[field]) for field in fields if address.get(field))


def _card_heading(anchor: Any, levels: int = 4) -> str:
    """The heading of the card a link sits in, a few levels up at most. A
    level holding several links is a list, not a card: its heading names
    the list, so the search stops there."""
    node = anchor.parent
    for _ in range(levels):
        if node is None or len(node.find_all("a", href=True)) > 2:
            return ""
        heading = node.find(["h1", "h2", "h3", "h4", "h5"])
        if heading:
            return str(heading.get_text(" ", strip=True))
        node = node.parent
    return ""


def parse_rendered_page(html: str, page_url: str) -> list[Posting]:
    """Extract JobPosting data first, then conservative job-card links."""
    soup = BeautifulSoup(html, "html.parser")
    structured = _jsonld_postings(soup, page_url)
    if structured:
        return structured
    out = []
    seen = set()
    board = HOSTED_BOARD_RE.match(page_url)
    for anchor in soup.find_all("a", href=True):
        href = urljoin(page_url, str(anchor.get("href")))
        # on a hosted board every deeper link on it is a job
        on_board = bool(board) and href.startswith(page_url.rstrip("/") + "/")
        if href in seen or not (on_board or JOB_PATH_RE.search(href)):
            continue
        title = anchor.get_text(" ", strip=True)
        container = anchor.find_parent(["article", "li"]) or anchor.parent
        if nav_title(title):  # "View position" on a job card: the card's heading names it
            title = _card_heading(anchor)
        if len(title) < 4 or href.split("#")[0] == page_url:
            continue
        seen.add(href)
        text = container.get_text(" ", strip=True) if container else title
        date_match = DATE_RE.search(text)
        out.append(
            {
                "title": title,
                "location": text.replace(title, "", 1).strip()[:200],
                "url": href,
                "description": text[:8000],
                "posted_at": date_match.group(1) if date_match else None,
            }
        )
    return out


# Resource types a job list never needs: skipping them makes each page load
# a fraction of the bytes and time.
SKIP_TYPES = frozenset({"image", "media", "font", "stylesheet", "manifest", "other"})
PAGES_AT_ONCE = max(1, int(os.environ.get("SOURCER_BROWSER_PAGES", "3")))
PAGE_TIMEOUT_MS = 30_000
SANDBOX = os.environ.get("SOURCER_BROWSER_SANDBOX", "1") != "0"
SETTLE_MS = 6_000  # after the first paint, how long a script driven list may take to fill in


class _Browser:
    """One headless Chromium for the whole run, shared by every fetch thread.

    Playwright objects belong to the thread that made them, so the browser
    lives on its own thread with its own event loop; fetch threads hand it a
    URL and wait for the HTML. It starts on the first page that needs it and
    closes when the process exits. At most PAGES_AT_ONCE pages are open.
    Every request the page makes is checked: GET only, public https only.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._public: dict[str, bool] = {}

    def _allowed(self, url: str) -> bool:
        host = urlsplit(url).hostname or ""
        if url.startswith("https://") and host in self._public:
            return self._public[host]
        try:
            validate_public_https_url(url)
            ok = True
        except ValueError:
            ok = False
        if url.startswith("https://"):
            self._public[host] = ok
        return ok

    def _start(self) -> None:
        with self._lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(
                    target=self._run, args=(self._loop,), name="sourcer-browser", daemon=True
                ).start()
        self._ready.wait(60)  # every caller waits, not only the one that started it
        if self._error is not None:
            raise ValueError(f"browser could not start: {self._error}") from None

    def _run(self, loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._open())
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        loop.run_forever()

    async def _open(self) -> None:
        from playwright.async_api import async_playwright  # noqa: PLC0415

        self._pw = await async_playwright().start()
        # the browser reads pages from anyone: it gets no secrets from this
        # process's environment (tokens, API keys), only what it needs to run
        # Chromium's own sandbox stays on, so a hostile page that breaks the
        # renderer still cannot reach this process or its files; a Linux
        # runner needs unprivileged user namespaces allowed for it
        self._chromium = await self._pw.chromium.launch(
            headless=True, env=_browser_env(), chromium_sandbox=SANDBOX
        )
        self._context = await self._chromium.new_context(
            accept_downloads=False, service_workers="block", java_script_enabled=True
        )
        await self._context.route("**/*", self._guard)
        # WebSockets bypass route(); a job list never needs one
        await self._context.route_web_socket("**/*", self._close_socket)
        self._slots = asyncio.Semaphore(PAGES_AT_ONCE)

    @staticmethod
    async def _close_socket(ws: Any) -> None:
        await ws.close()

    async def _guard(self, route: Any) -> None:
        request = route.request
        if (
            request.method != "GET"
            or request.resource_type in SKIP_TYPES
            or not self._allowed(request.url)
        ):
            await route.abort()
            return
        # the page's request is made by net.get, not by Chromium: the same
        # pinned public address check and size cap as every other fetch, so a
        # host whose DNS answer changes after the check cannot reach this machine
        try:
            reply = await asyncio.to_thread(
                net.get,
                request.url,
                headers=_forwarded(request.headers),
                timeout=PAGE_TIMEOUT_MS / 1000,
                allow_redirects=False,  # Chromium follows, and each hop comes back here
            )
        except requests.RequestException:
            await route.abort()
            return
        await route.fulfill(
            status=reply.status_code, headers=_replied(reply.headers), body=reply.content
        )

    async def _page_html(self, url: str) -> str:
        async with self._slots:
            page = await self._context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                # a script driven list fills in after the first paint; a page
                # that never goes quiet is read as it is
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("networkidle", timeout=SETTLE_MS)
                return str(await page.content())[: net.MAX_BODY_BYTES]  # scripts can grow it
            finally:
                await page.close()

    def html(self, url: str) -> str:
        self._start()
        if self._loop is None:
            raise ValueError("browser is not running")
        future = asyncio.run_coroutine_threadsafe(self._page_html(url), self._loop)
        try:
            return future.result(timeout=(PAGE_TIMEOUT_MS + SETTLE_MS) / 1000 + 30)
        except Exception:
            future.cancel()
            raise ValueError("careers page could not be rendered") from None

    def close(self) -> None:
        if self._loop is None or self._error is not None or not self._loop.is_running():
            return  # never started, failed to start, or already closed

        async def shut() -> None:
            await self._context.close()
            await self._chromium.close()
            await self._pw.stop()

        with contextlib.suppress(Exception):  # the process is exiting anyway
            asyncio.run_coroutine_threadsafe(shut(), self._loop).result(timeout=20)
        self._loop.call_soon_threadsafe(self._loop.stop)


BROWSER_ENV_KEYS = ("PATH", "HOME", "TMPDIR", "LANG", "PLAYWRIGHT_BROWSERS_PATH", "DISPLAY")


FORWARDED = frozenset({"accept", "accept-language", "user-agent", "referer"})
# the body arrives decoded and whole, so these no longer describe it
DROPPED = frozenset({"content-encoding", "content-length", "transfer-encoding", "connection"})


def _forwarded(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() in FORWARDED}


def _replied(headers: Any) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in DROPPED}


def _browser_env() -> dict[str, str]:
    """The few variables Chromium needs, and nothing else from this process."""
    return {k: os.environ[k] for k in BROWSER_ENV_KEYS if k in os.environ}


_BROWSER: _Browser | None = None
_BROWSER_LOCK = threading.Lock()


def _browser() -> _Browser:
    global _BROWSER  # noqa: PLW0603  one browser per process, started on first use
    with _BROWSER_LOCK:
        if _BROWSER is None:
            _BROWSER = _Browser()
            atexit.register(_BROWSER.close)
        return _BROWSER


def _render(url: str) -> str:
    if os.environ.get("SOURCER_BROWSER", "0") != "1":
        raise ValueError("browser fallback is disabled; set SOURCER_BROWSER=1 to opt in")
    try:
        import playwright.async_api  # noqa: F401, PLC0415
    except ImportError as exc:
        raise ValueError(
            "browser collection requires: pip install -e '.[browser]' "
            "and playwright install chromium"
        ) from exc
    return _browser().html(validate_public_https_url(url))


def board_on_page(html: str) -> tuple[str, str] | None:
    """(ats, slug) when the page links or embeds a job board Sourcer reads
    directly; that board's own API is faster and complete."""
    for ats, pattern in BOARD_LINKS:
        m = pattern.search(html)
        if m and m.group("slug").lower() not in BOARD_SLUG_NOISE:
            return ats, m.group("slug") if ats != "workday" else "/".join(
                m.group("slug", "wd", "site")
            )
    return None


def _from_board(board: tuple[str, str]) -> list[Posting]:
    from . import (  # noqa: PLC0415  same layer, only when a page names one
        ashby,
        greenhouse,
        lever,
        workday,
    )

    readers = {
        "greenhouse": greenhouse.fetch_postings,
        "lever": lever.fetch_postings,
        "ashby": ashby.fetch_postings,
        "workday": workday.fetch_postings,
    }
    return readers[board[0]]({"slug": board[1]})


def _roles_link(html: str, page_url: str) -> str | None:
    """Where the full list of open roles is: a hosted board the page links or
    embeds, else the page's own "open roles" link."""
    hosted = HOSTED_BOARD_RE.search(html)
    if hosted:
        return hosted.group(0)
    soup = BeautifulSoup(html, "html.parser")
    host = urlsplit(page_url).hostname or ""
    for anchor in soup.find_all("a", href=True):
        href = urljoin(page_url, str(anchor.get("href"))).split("#")[0]
        text = anchor.get_text(" ", strip=True)
        same_site = (urlsplit(href).hostname or "").endswith(host.removeprefix("www."))
        if (
            href != page_url
            and href.startswith("https://")
            and same_site
            and ROLES_LINK_RE.search(text)
        ):
            return href
    return None


def _read(url: str, depth: int = 0) -> list[Posting]:
    """One careers page: its job board if it names one, its own listings,
    or one step to its "open roles" page when the first has none."""
    html = _render(url)
    board = board_on_page(html)
    if board:
        with contextlib.suppress(Exception):  # a board that will not answer falls back to the page
            return _from_board(board)
    postings = parse_rendered_page(html, url)
    if depth or any(ROLE_WORD_RE.search(p["title"]) for p in postings):
        return postings
    # nothing, or only categories and cities: one step to the real list
    deeper = _roles_link(html, url)
    return (_read(deeper, depth + 1) or postings) if deeper else postings


def fetch_postings(company: dict[str, Any]) -> list[Posting]:
    """Render configured pages and extract listings without interacting with forms."""
    configured = company.get("url")
    domain = company.get("domain")
    if configured:
        candidates = [configured]
    elif domain:
        found = str(company.get("slug") or "").strip("/")  # the path the probe found jobs on
        paths = [found] + [p for p in CAREER_PATHS if p != found] if found else list(CAREER_PATHS)
        candidates = [f"https://{domain}/{path}" for path in paths]
    else:
        return []
    for url in candidates:
        if not url:
            continue
        postings = _read(str(url))
        if postings:
            return postings
    return []
