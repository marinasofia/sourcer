"""Link sources: one collector per kind of link, registered in ``COLLECTORS``.

``detect`` names the kind from the URL. Every collector fetches through
``web.Fetcher`` (robots.txt, User-Agent, caps, same host redirects, the call
log) and redacts every excerpt. What each reads:

page          the page's main text and up to a few more pages on the same host
feed          an RSS or Atom feed: titles, dates and summaries
kaggle        title and description of each Kaggle page you add (Kaggle shows
              notebooks and datasets only with JavaScript, so a profile page
              gives little: add each notebook or dataset link)
huggingface   public models, spaces and datasets of a user, from the public API
devpost       the projects on a public Devpost profile and their pages
scholar       one Google Scholar profile page; when Google refuses, a pasted
              publications list (``publications``) is the way
credly        the public badges of a Credly user (badges.json)
publications  a TXT or MD file of publications, pasted by hand
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import config
from . import web
from .items import Collected, SourceItem
from .privacy import redact
from .resume import split_parts

LINK_TYPES = ("page", "feed", "kaggle", "huggingface", "devpost", "scholar", "credly")
PAGE_CHARS = 4000
MAX_ENTRIES = 25
SUMMARY_CHARS = 600
NOT_PAGES = (".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".mp4", ".mp3",
             ".css", ".js", ".xml", ".json", ".ico")  # fmt: skip
FEED_PATH_RE = re.compile(r"(\.(xml|rss|atom)|/(feed|rss|atom)(\.xml)?)/?$", re.I)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
HF_ID_RE = re.compile(r"^[A-Za-z0-9][\w.-]{0,95}/[A-Za-z0-9][\w.-]{0,95}$")
SCHOLAR_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
SCHOLAR_HOST_RE = re.compile(r"^scholar\.google\.[a-z.]{2,12}$")
LINKEDIN = (
    "LinkedIn is read only from the data export you download yourself "
    "(LinkedIn Settings, Data privacy, Get a copy of your data); add the zip"
)


def _host(url: str) -> str:
    return str(urlparse(url).hostname or "").lower().removeprefix("www.")


def _segments(url: str) -> list[str]:
    return [s for s in urlparse(url).path.split("/") if s]


HOST_KINDS = {"huggingface.co": "huggingface", "kaggle.com": "kaggle", "devpost.com": "devpost",
              "credly.com": "credly"}  # fmt: skip


def detect(url: str) -> tuple[str, str] | str:
    """(source type, value) for a link, or why it cannot be added."""
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return f"links start with https://: {url}"
    host, parts = _host(url), _segments(url)
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return LINKEDIN
    if host == "github.com" and len(parts) == 1:
        return "github", parts[0]
    kind = HOST_KINDS.get(host) or ("scholar" if SCHOLAR_HOST_RE.match(host) else None)
    return kind or ("feed" if FEED_PATH_RE.search(parsed.path) else "page"), url


def describe(kind: str, url: str) -> str:
    """One line on what Sourcer will read for a link, before it reads it."""
    host = _host(url)
    return {
        "page": f"I'll read this page and up to {config.ONBOARD_LINK_MAX_PAGES - 1} more on "
        f"{host}.",
        "feed": f"I'll read the titles, dates and summaries in this feed on {host}.",
        "kaggle": "I'll read the title and description of this Kaggle page.",
        "huggingface": "I'll read your public Hugging Face models, spaces and datasets.",
        "devpost": "I'll read the projects on your public Devpost profile.",
        "scholar": "I'll read your Google Scholar profile page once.",
        "credly": "I'll read your public Credly badges.",
    }.get(kind, "I'll read this link.") + " robots.txt is respected."  # fmt: skip


def _item(source: dict[str, Any], kind: str, where: str, text: str, part: int = 0) -> SourceItem:
    return SourceItem(source["id"], kind, where, redact(text), part)


def _guard(collect: Callable[[dict[str, Any], Collected], None]) -> Callable[..., Collected]:
    """Run a collector; a Blocked fetch becomes a note and keeps what was read."""

    def run(source: dict[str, Any]) -> Collected:
        out = Collected()
        try:
            collect(source, out)
        except web.Blocked as exc:
            out.notes.append(str(exc))
        return out

    run.__doc__ = collect.__doc__
    return run


# ---------------------------------------------------------------- page and feed


def _is_feed(body: str) -> bool:
    head = body.lstrip()[:500].lower()
    if head.startswith("<?xml"):
        return "<rss" in head or "<feed" in head
    return head.startswith(("<rss", "<feed"))


def _page(source: dict[str, Any], out: Collected) -> None:
    """A web page, then a few more pages on the same host it links to."""
    fetch = web.Fetcher(source["id"])
    start = str(source["value"])
    resp = fetch.get(start)
    if _is_feed(resp.text):
        _read_feed(source, out, resp.content, start)
        return
    queue = [start]
    first = True
    fetched = 1
    while queue:
        url = queue.pop(0)
        if not first:
            if fetched >= config.ONBOARD_LINK_MAX_PAGES:
                break
            fetched += 1
            try:
                resp = fetch.get(url)
            except web.Blocked as exc:
                out.notes.append(str(exc))
                continue
        url = fetch.last_url or url  # a same host redirect lands elsewhere
        doc = web.soup(resp.text)
        if first:
            queue = web.same_host_links(doc, start, NOT_PAGES)
            first = False
        text = web.main_text(doc, PAGE_CHARS)
        if not text or url in out.read:
            continue
        head = web.title(doc)
        out.items.append(_item(source, "page", url, f"Page: {head}\nURL: {url}\n\n{text}"))
        out.read.append(url)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child(entry: ET.Element, *names: str) -> str:
    for el in entry:
        if _local(el.tag) in names:
            if _local(el.tag) == "link" and el.get("href"):
                return str(el.get("href"))
            return "".join(el.itertext()).strip()
    return ""


def _read_feed(source: dict[str, Any], out: Collected, body: bytes, url: str) -> None:
    if re.search(rb"<!\s*(doctype|entity)", body[:20000], re.I):
        raise web.Blocked(f"{url} declares a DTD; Sourcer does not read those feeds")
    try:
        root = ET.fromstring(body)  # noqa: S314 (no DTD, so no entity expansion)
    except ET.ParseError as exc:
        raise web.Blocked(f"{url} is not a feed Sourcer can read") from exc
    entries = [el for el in root.iter() if _local(el.tag) in ("item", "entry")]
    for n, entry in enumerate(entries[:MAX_ENTRIES]):
        head = _child(entry, "title")
        when = _child(entry, "pubdate", "published", "updated", "date")
        summary = web.soup(_child(entry, "description", "summary", "content", "encoded"))
        text = " ".join(summary.get_text(" ").split())[:SUMMARY_CHARS]
        link = _child(entry, "link")
        where = link if link.startswith("https://") else f"{url}#{n}"
        out.items.append(
            _item(source, "feed", where, f"Post: {head}\nDate: {when}\nSummary: {text}")
        )
        out.read.append(head or where)
    if not entries:
        out.notes.append("no posts found in the feed")


def _feed(source: dict[str, Any], out: Collected) -> None:
    """An RSS or Atom feed: titles, dates and summaries of the latest posts."""
    url = str(source["value"])
    resp = web.Fetcher(source["id"]).get(url, "application/rss+xml,application/atom+xml,*/*")
    _read_feed(source, out, resp.content, url)


# ---------------------------------------------------------------- Kaggle

GENERIC_KAGGLE = "kaggle: your home for data science"


def _kaggle(source: dict[str, Any], out: Collected) -> None:
    """Title and description of one Kaggle page (profile, notebook or dataset)."""
    url = str(source["value"])
    doc = web.soup(web.Fetcher(source["id"]).get(url).text)
    head = web.meta(doc, "og:title") or web.title(doc)
    about = web.meta(doc, "og:description", "description")
    if not head or head.lower() == GENERIC_KAGGLE:
        out.notes.append("Kaggle showed no content for this page without JavaScript")
        return
    parts = _segments(url)
    kind = {"code": "notebook", "datasets": "dataset", "competitions": "competition"}.get(
        parts[0] if parts else "", "profile" if len(parts) == 1 else "page"
    )
    out.items.append(_item(source, "kaggle", url, f"Kaggle {kind}: {head}\nAbout: {about}"))
    out.read.append(url)
    if kind == "profile":
        out.notes.append(
            "Kaggle lists notebooks and datasets only with JavaScript; "
            "add each notebook or dataset link to read it"
        )


# ---------------------------------------------------------------- Hugging Face

HF_API = "https://huggingface.co/api"
HF_NOISE = ("region:", "endpoints_compatible", "autotrain_compatible", "license:", "arxiv:")


def hf_user(url: str) -> str | None:
    parts = _segments(url)
    if parts[:1] in (["models"], ["datasets"], ["spaces"]):
        parts = parts[1:]
    return parts[0] if parts and NAME_RE.match(parts[0]) else None


def _huggingface(source: dict[str, Any], out: Collected) -> None:
    """Public models, spaces and datasets of one user, from the public API."""
    user = hf_user(str(source["value"]))
    if user is None:
        out.notes.append("no Hugging Face username in this link")
        return
    fetch = web.Fetcher(source["id"])
    for kind in ("models", "spaces", "datasets"):
        rows = fetch.json(f"{HF_API}/{kind}?author={user}&limit={MAX_ENTRIES}")
        for row in rows if isinstance(rows, list) else []:
            rid = str(row.get("id", "")) if isinstance(row, dict) else ""
            if not HF_ID_RE.match(rid) or ".." in rid:
                continue
            tags = [str(t) for t in row.get("tags") or [] if not str(t).startswith(HF_NOISE)]
            lines = [
                f"Hugging Face {kind[:-1]}: {rid}",
                f"Task: {row.get('pipeline_tag') or ''}",
                f"Library or SDK: {row.get('library_name') or row.get('sdk') or ''}",
                f"Tags: {', '.join(tags[:15])}",
                f"Likes: {row.get('likes') or 0}, downloads: {row.get('downloads') or 0}",
                f"Created: {str(row.get('createdAt') or '')[:10]}",
            ]
            prefix = "" if kind == "models" else f"{kind}/"
            where = f"https://huggingface.co/{prefix}{rid}"
            out.items.append(_item(source, "huggingface", where, "\n".join(lines)))
            out.read.append(rid)
    if not out.items:
        out.notes.append(f"no public models, spaces or datasets for {user}")


# ---------------------------------------------------------------- Devpost


def _devpost_project(source: dict[str, Any], out: Collected, fetch: web.Fetcher, url: str) -> None:
    doc = web.soup(fetch.get(url).text)
    name = doc.select_one("#app-title")
    head = name.get_text(" ", strip=True) if name else web.title(doc)
    tools = [a.get_text(strip=True) for a in doc.select("#built-with a")]
    details = doc.select_one("#app-details-left")
    story = " ".join(details.get_text(" ").split())[:PAGE_CHARS] if details else ""
    tagline = web.meta(doc, "og:description", "description")
    text = f"Devpost project: {head}\nTagline: {tagline}\nBuilt with: {', '.join(tools)}\n\n{story}"
    out.items.append(_item(source, "devpost", url, text))
    out.read.append(head or url)


def _devpost(source: dict[str, Any], out: Collected) -> None:
    """The projects on a public Devpost profile, or one project page."""
    url = str(source["value"])
    fetch = web.Fetcher(source["id"])
    if _segments(url)[:1] == ["software"]:
        _devpost_project(source, out, fetch, url)
        return
    doc = web.soup(fetch.get(url).text)
    projects = [
        str(a["href"]) for a in doc.select("a.link-to-software[href]")
        if str(a["href"]).startswith("https://devpost.com/software/")
    ]  # fmt: skip
    for project in list(dict.fromkeys(projects))[: config.ONBOARD_LINK_MAX_PAGES]:
        try:
            _devpost_project(source, out, fetch, project)
        except web.Blocked as exc:
            out.notes.append(str(exc))
    if not projects:
        out.notes.append("no projects found on this Devpost profile")


# ---------------------------------------------------------------- Google Scholar

SCHOLAR_REFUSED = (
    "Google Scholar did not return the profile (it often refuses automated reading). "
    "Save your publications as a text file and add it with "
    "`sourcer init --add --publications FILE`"
)


def _scholar(source: dict[str, Any], out: Collected) -> None:
    """One Google Scholar profile page: name, interests and publications."""
    user = (parse_qs(urlparse(str(source["value"])).query).get("user") or [""])[0]
    if not SCHOLAR_ID_RE.match(user):
        out.notes.append("no Scholar user id in this link (it looks like ?user=...)")
        return
    url = f"https://scholar.google.com/citations?user={user}&hl=en"
    try:
        doc = web.soup(web.Fetcher(source["id"]).get(url).text)
    except web.Blocked as exc:
        out.notes.append(f"{exc}. {SCHOLAR_REFUSED}")
        return
    rows = doc.select("tr.gsc_a_tr")
    if not rows:
        out.notes.append(SCHOLAR_REFUSED)
        return
    name = doc.select_one("#gsc_prf_in")
    interests = [a.get_text(strip=True) for a in doc.select("#gsc_prf_int a")]
    lines = [f"Google Scholar profile: {name.get_text(strip=True) if name else ''}",
             f"Interests: {', '.join(interests)}", "Publications:"]  # fmt: skip
    for row in rows[:40]:
        head = row.select_one("a.gsc_a_at")
        gray = [g.get_text(" ", strip=True) for g in row.select("div.gs_gray")]
        year = row.select_one("span.gsc_a_h")
        venue = gray[1] if len(gray) > 1 else ""
        lines.append(f"- {head.get_text(strip=True) if head else ''} ({venue}; "
                     f"{year.get_text(strip=True) if year else ''})")  # fmt: skip
    out.items.append(_item(source, "scholar", url, "\n".join(lines)))
    out.read.append(f"{len(rows[:40])} publications")


# ---------------------------------------------------------------- Credly


def credly_user(url: str) -> str | None:
    parts = _segments(url)
    if len(parts) >= 2 and parts[0] == "users" and NAME_RE.match(parts[1]):
        return parts[1]
    return None


def _credly(source: dict[str, Any], out: Collected) -> None:
    """The public badges of one Credly user."""
    user = credly_user(str(source["value"]))
    if user is None:
        out.notes.append("Credly links look like https://www.credly.com/users/NAME")
        return
    data = web.Fetcher(source["id"]).json(f"https://www.credly.com/users/{user}/badges.json")
    badges = data.get("data") if isinstance(data, dict) else None
    for badge in (badges if isinstance(badges, list) else [])[:MAX_ENTRIES]:
        if not isinstance(badge, dict) or not isinstance(badge.get("badge_template"), dict):
            continue
        tpl = badge["badge_template"]
        skills = [str(s.get("name")) for s in tpl.get("skills") or [] if isinstance(s, dict)]
        issuer = badge.get("issuer") if isinstance(badge.get("issuer"), dict) else {}
        lines = [
            f"Credly badge: {tpl.get('name') or ''}",
            f"Issued by: {(issuer or {}).get('summary') or ''}",
            f"Issued: {badge.get('issued_at_date') or ''}",
            f"Skills: {', '.join(skills)}",
            f"About: {str(tpl.get('description') or '')[:SUMMARY_CHARS]}",
        ]
        bid = str(badge.get("id") or "")
        where = f"https://www.credly.com/badges/{bid}" if NAME_RE.match(bid) else source["value"]
        out.items.append(_item(source, "credly", str(where), "\n".join(lines)))
        out.read.append(str(tpl.get("name") or bid))
    if not out.items:
        out.notes.append(f"no public badges for {user}")


# ---------------------------------------------------------------- publications

PUBLICATION_SUFFIXES = (".txt", ".md")


def publications(source: dict[str, Any]) -> Collected:
    """A publications list the candidate saved as a text file."""
    path = Path(source["value"]).expanduser()
    out = Collected()
    try:
        if path.suffix.lower() not in PUBLICATION_SUFFIXES:
            raise ValueError(f"the publications list must be a TXT or MD file: {path.name}")
        if path.stat().st_size > config.ONBOARD_MAX_FILE_BYTES:
            raise ValueError(f"{path.name} is over {config.ONBOARD_MAX_FILE_BYTES:,} bytes")
        text = redact(path.read_text(encoding="utf-8", errors="replace")).strip()
    except (OSError, ValueError) as exc:
        out.notes.append(f"publications not read: {exc}")
        return out
    out.read.append(path.name)
    for n, part in enumerate(split_parts(text, min(PAGE_CHARS, config.ONBOARD_BATCH_CHARS))):
        out.items.append(SourceItem(source["id"], "publications", path.name, part, n))
    return out


COLLECTORS: dict[str, Callable[[dict[str, Any]], Collected]] = {
    "page": _guard(_page),
    "feed": _guard(_feed),
    "kaggle": _guard(_kaggle),
    "huggingface": _guard(_huggingface),
    "devpost": _guard(_devpost),
    "scholar": _guard(_scholar),
    "credly": _guard(_credly),
    "publications": publications,
}


def legacy_link(source: dict[str, Any]) -> Collected:
    """A link saved before link collectors existed: read it as its detected kind."""
    kind = detect(str(source["value"]))
    if isinstance(kind, str):
        return Collected(notes=[kind])
    if kind[0] not in COLLECTORS:  # a GitHub profile link
        return Collected(notes=[f"add it with `sourcer init --add --github {kind[1]}`"])
    return COLLECTORS[kind[0]]({**source, "value": kind[1]})
