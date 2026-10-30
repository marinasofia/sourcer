"""Link collectors on fake HTTP and made up pages: robots.txt, the
User-Agent, page and byte caps, same host redirects, redaction and the log."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
import requests

from sourcer import config, net
from sourcer.onboard import cli, collect, extract, links, store, web
from sourcer.onboard.items import source_spec
from tests.test_onboard_cli import Script
from tests.test_onboard_collectors import FAKE_EMAIL, FAKE_KEY, FakeResponse

ROBOTS_OK = FakeResponse(200, "User-agent: *\nDisallow: /private\n")


class Web:
    """A fake internet: URL (with query) to response; robots.txt allows all
    unless set. Records every URL and its headers."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []
        monkeypatch.setattr(web.net, "get", self.get)

    def get(self, url: str, **kw: Any) -> FakeResponse:
        self.calls.append((url, kw.get("headers") or {}))
        assert kw.get("allow_redirects") is False and kw.get("timeout")
        hit = self.routes.get(url)
        if hit is None and url.endswith("/robots.txt"):
            return FakeResponse(404, "")
        if isinstance(hit, Exception):
            raise hit
        return hit if hit is not None else FakeResponse(404, "missing")

    @property
    def urls(self) -> list[str]:
        return [u for u, _ in self.calls]


def html(body: str, head: str = "") -> FakeResponse:
    return FakeResponse(200, f"<html><head>{head}</head><body>{body}</body></html>")


def run(kind: str, value: str) -> Any:
    return collect.COLLECTORS[kind](source_spec(kind, value))


# ---------------------------------------------------------------- detection


def test_detect_names_each_kind() -> None:
    assert links.detect("https://example.org/") == ("page", "https://example.org/")
    assert links.detect("https://blog.example.org/feed.xml")[0] == "feed"
    assert links.detect("https://blog.example.org/rss/")[0] == "feed"
    assert links.detect("https://www.kaggle.com/octoexample")[0] == "kaggle"
    assert links.detect("https://huggingface.co/octo-example")[0] == "huggingface"
    assert links.detect("https://devpost.com/octo-example")[0] == "devpost"
    assert links.detect("https://scholar.google.co.uk/citations?user=AbC123xyz")[0] == "scholar"
    assert links.detect("https://www.credly.com/users/octo-example")[0] == "credly"
    assert links.detect("https://github.com/octo-example") == ("github", "octo-example")
    assert "data export" in str(links.detect("https://www.linkedin.com/in/someone"))
    assert "https://" in str(links.detect("http://example.org"))
    assert "robots.txt is respected" in links.describe("huggingface", "https://huggingface.co/x")
    assert links.describe("other", "https://example.org").startswith("I'll read this link.")


# ---------------------------------------------------------------- robots.txt


def test_robots_rules_follow_rfc_9309() -> None:
    text = (
        "User-agent: *\nDisallow: /citations?\nAllow: /citations?user=\n"
        "Disallow: /citations?user=*@\nDisallow: /*.pdf$\n\n"
        "User-agent: OtherBot\nDisallow: /\n"
    )
    robots = web.Robots.parse(text)
    assert robots.allows("https://h/citations?user=abc&hl=en")
    assert not robots.allows("https://h/citations?view_op=x")
    assert not robots.allows("https://h/citations?user=a@b")
    assert not robots.allows("https://h/cv.pdf") and robots.allows("https://h/cv.pdf.html")
    assert robots.allows("https://h/")
    mine = web.Robots.parse("User-agent: *\nDisallow:\n\nUser-agent: sourcer\nUser-agent: x\n"
                            "Disallow: /drafts\nAllow: /drafts/public\n")  # fmt: skip
    assert not mine.allows("https://h/drafts/a") and mine.allows("https://h/drafts/public/a")
    tie = web.Robots.parse("User-agent: *\nDisallow: /a\nAllow: /a\n# comment only\nnoise\n")
    assert tie.allows("https://h/a")
    assert not web.Robots(deny_all=True).allows("https://h/")


def test_robots_unreachable_means_read_nothing(
    onboard_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Web(monkeypatch, {"https://example.org/robots.txt": FakeResponse(503, "")})
    got = run("page", "https://example.org/")
    assert "robots.txt on example.org could not be read" in got.notes[0]
    assert fake.urls == ["https://example.org/robots.txt"] and not got.items
    fake.routes["https://example.org/robots.txt"] = requests.ConnectionError("down")
    assert not run("page", "https://example.org/").items


# ---------------------------------------------------------------- pages


SITE = {
    "https://example.org/robots.txt": ROBOTS_OK,
    "https://example.org/": html(
        '<nav><a href="/projects">Projects</a><a href="/private/x">Private</a>'
        '<a href="https://other.example.com/">Elsewhere</a><a href="/cv.pdf">CV</a>'
        '<a href="/about#me">About</a><a href="/about">About again</a>'
        '<a href="/moved">Moved</a><a href="/away">Away</a></nav>'
        f"<main><h1>Alex Example</h1><p>Data projects in Python.</p><p>{FAKE_EMAIL}</p></main>"
        "<script>var secret = 1</script><footer>footer text</footer>",
        "<title>Alex Example</title>",
    ),
    "https://example.org/projects": html("<article>Built a churn model with SQL.</article>"),
    "https://example.org/about": html(""),
    "https://example.org/moved": FakeResponse(301, "", {"Location": "/projects2"}),
    "https://example.org/projects2": html("<p>Shipped a dashboard to 30 users.</p>"),
    "https://example.org/away": FakeResponse(302, "", {"Location": "https://elsewhere.example/"}),
}


def test_page_reads_main_text_on_the_same_host(
    onboard_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "ONBOARD_LINK_MAX_PAGES", 8)
    fake = Web(monkeypatch, dict(SITE))
    got = run("page", "https://example.org/")
    assert got.read == ["https://example.org/", "https://example.org/projects",
                        "https://example.org/projects2"]  # fmt: skip
    first = got.items[0].text
    assert first.startswith("Page: Alex Example\nURL: https://example.org/")
    assert "Data projects in Python." in first and FAKE_EMAIL not in first
    assert "secret" not in first and "footer text" not in first and "Projects" not in first
    assert "robots.txt on example.org asks tools not to read https://example.org/private/x" in (
        got.notes
    )
    assert any("moves to elsewhere.example, another site" in n for n in got.notes)
    assert "https://other.example.com/" not in fake.urls and "https://example.org/cv.pdf" not in (
        fake.urls
    )
    assert fake.urls.count("https://example.org/robots.txt") == 1
    assert all(h["User-Agent"] == web.USER_AGENT for _, h in fake.calls)
    assert web.USER_AGENT.startswith("Sourcer/")
    assert web.USER_AGENT.endswith("(+https://github.com/marinasofia/sourcer)")
    log = store.read_audit()
    assert {e["destination"] for e in log} == {"example.org"}
    assert all(set(e) == {"ts", "destination", "action", "sources", "chars"} for e in log)


def test_page_cap_and_failures(onboard_paths: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ONBOARD_LINK_MAX_PAGES", 2)
    Web(monkeypatch, dict(SITE))
    got = run("page", "https://example.org/")
    assert len(got.read) == 2
    big = FakeResponse(200, "x" * (web.MAX_BYTES + 1))
    Web(monkeypatch, {"https://example.org/robots.txt": ROBOTS_OK, "https://example.org/": big})
    assert "over 2,000,000 bytes" in run("page", "https://example.org/").notes[0]
    Web(monkeypatch, {"https://example.org/": FakeResponse(500, "")})
    assert "answered 500" in run("page", "https://example.org/").notes[0]
    loop = FakeResponse(301, "", {"Location": "/"})
    Web(monkeypatch, {"https://example.org/": loop})
    assert "redirects too many times" in run("page", "https://example.org/").notes[0]
    Web(monkeypatch, {"https://example.org/": requests.Timeout("slow")})
    assert (
        "could not read https://example.org/ (Timeout)"
        in run("page", "https://example.org/").notes[0]
    )


RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Notes</title>
<item><title>Forecasting with SQL</title><link>https://blog.example.org/sql</link>
<pubDate>Tue, 01 Sep 2026 10:00:00 GMT</pubDate>
<description>&lt;p&gt;How I built a &lt;b&gt;forecast&lt;/b&gt; in SQL.&lt;/p&gt;
</description></item>
<item><title>No link</title><description>Plain.</description></item>
</channel></rss>"""
ATOM = """<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Agents in Python</title><link href="https://blog.example.org/agents"/>
<updated>2026-10-01T00:00:00Z</updated><summary>Tool use with LangGraph.</summary></entry>
</feed>"""


def test_feeds_rss_atom_and_refusals(onboard_paths: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = "https://blog.example.org"
    Web(
        monkeypatch,
        {
            f"{base}/feed.xml": FakeResponse(200, RSS),
            f"{base}/atom.xml": FakeResponse(200, ATOM),
            f"{base}/": FakeResponse(200, ATOM),
            f"{base}/dtd.xml": FakeResponse(
                200, '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss/>'
            ),
            f"{base}/bad.xml": FakeResponse(200, "<rss><item>"),
            f"{base}/empty.xml": FakeResponse(200, "<rss><channel/></rss>"),
        },
    )
    got = run("feed", f"{base}/feed.xml")
    assert [i.location for i in got.items] == [f"{base}/sql", f"{base}/feed.xml#1"]
    assert got.items[0].text == (
        "Post: Forecasting with SQL\nDate: Tue, 01 Sep 2026 10:00:00 GMT\n"
        "Summary: How I built a forecast in SQL."
    )
    atom = run("feed", f"{base}/atom.xml")
    assert atom.items[0].location == f"{base}/agents" and "LangGraph" in atom.items[0].text
    assert run("page", f"{base}/").items[0].source_type == "feed"  # a page that is a feed
    assert "declares a DTD" in run("feed", f"{base}/dtd.xml").notes[0]
    assert "not a feed Sourcer can read" in run("feed", f"{base}/bad.xml").notes[0]
    assert run("feed", f"{base}/empty.xml").notes == ["no posts found in the feed"]


# ---------------------------------------------------------------- Kaggle


def test_kaggle_reads_page_meta_only(onboard_paths: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def page(t: str, d: str) -> FakeResponse:
        return html("", f'<meta property="og:title" content="{t}">'
                        f'<meta name="description" content="{d}">')  # fmt: skip

    k = "https://www.kaggle.com"
    Web(
        monkeypatch,
        {
            f"{k}/octoexample": page("Alex Example", "Data scientist"),
            f"{k}/code/octoexample/churn": page("Churn EDA", "XGBoost baseline"),
            f"{k}/code/octoexample/js": page("Kaggle: Your Home for Data Science", ""),
        },
    )
    prof = run("kaggle", f"{k}/octoexample")
    assert prof.items[0].text == "Kaggle profile: Alex Example\nAbout: Data scientist"
    assert "add each notebook or dataset link" in prof.notes[0]
    nb = run("kaggle", f"{k}/code/octoexample/churn")
    assert nb.items[0].text.startswith("Kaggle notebook: Churn EDA") and not nb.notes
    assert "without JavaScript" in run("kaggle", f"{k}/code/octoexample/js").notes[0]


# ---------------------------------------------------------------- Hugging Face


def test_huggingface_public_api(onboard_paths: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = "https://huggingface.co/api"
    model = {"id": "octo-example/churn-bert", "pipeline_tag": "text-classification",
             "library_name": "transformers", "tags": ["pytorch", "region:us", "license:mit"],
             "likes": 2, "downloads": 40, "createdAt": "2026-02-03T00:00:00Z"}  # fmt: skip
    space = {"id": "octo-example/demo", "sdk": "gradio", "tags": [], "createdAt": "2026-05-01"}
    fake = Web(monkeypatch, {
        f"{api}/models?author=octo-example&limit=25": FakeResponse(200, [model, "junk"]),
        f"{api}/spaces?author=octo-example&limit=25": FakeResponse(200, [space, {"id": "../x"}]),
        f"{api}/datasets?author=octo-example&limit=25": FakeResponse(200, {"error": "x"}),
    })  # fmt: skip
    got = run("huggingface", "https://huggingface.co/octo-example")
    assert got.read == ["octo-example/churn-bert", "octo-example/demo"]
    assert got.items[0].location == "https://huggingface.co/octo-example/churn-bert"
    assert got.items[1].location == "https://huggingface.co/spaces/octo-example/demo"
    text = got.items[0].text
    assert "Task: text-classification" in text and "Tags: pytorch\n" in text
    assert "Library or SDK: gradio" in got.items[1].text
    assert fake.urls[0] == "https://huggingface.co/robots.txt"
    empty = Web(monkeypatch, {f"{api}/{k}?author=nobody&limit=25": FakeResponse(200, [])
                              for k in ("models", "spaces", "datasets")})  # fmt: skip
    assert (
        "no public models" in run("huggingface", "https://huggingface.co/spaces/nobody").notes[-1]
    )
    assert empty.urls  # the API was asked
    assert run("huggingface", "https://huggingface.co/").notes == [
        "no Hugging Face username in this link"
    ]

    class NotJson(FakeResponse):
        def json(self) -> Any:
            raise ValueError("not json")

    Web(monkeypatch, {f"{api}/models?author=bad&limit=25": NotJson(200, "<html>")})
    assert "did not answer with JSON" in run("huggingface", "https://huggingface.co/bad").notes[0]


# ---------------------------------------------------------------- Devpost


def project_page(name: str) -> FakeResponse:
    return html(
        f'<h1 id="app-title">{name}</h1><div id="app-details-left"><p>We built {name} with '
        'Flask.</p></div><div id="built-with"><a href="/b/flask">flask</a><a>python</a></div>',
        f'<meta property="og:description" content="{name} tagline">',
    )


def test_devpost_profile_and_project(onboard_paths: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = "https://devpost.com"
    profile = html(
        f'<a class="link-to-software" href="{d}/software/alpha">A</a>'
        f'<a class="link-to-software" href="{d}/software/alpha">A again</a>'
        f'<a class="link-to-software" href="{d}/software/beta">B</a>'
        '<a class="link-to-software" href="https://evil.example/software/x">X</a>'
    )
    Web(monkeypatch, {f"{d}/octo-example": profile, f"{d}/software/alpha": project_page("Alpha"),
                      f"{d}/empty": html("")})  # fmt: skip
    got = run("devpost", f"{d}/octo-example")
    assert got.read == ["Alpha"] and "answered 404" in got.notes[0]
    assert got.items[0].text.startswith("Devpost project: Alpha\nTagline: Alpha tagline\n"
                                        "Built with: flask, python")  # fmt: skip
    assert run("devpost", f"{d}/software/alpha").read == ["Alpha"]
    assert run("devpost", f"{d}/empty").notes == ["no projects found on this Devpost profile"]


# ---------------------------------------------------------------- Scholar

SCHOLAR = html(
    '<div id="gsc_prf_in">A. Example</div><div id="gsc_prf_int"><a>NLP</a><a>Agents</a></div>'
    '<table><tr class="gsc_a_tr"><td><a class="gsc_a_at">Tool use in small agents</a>'
    '<div class="gs_gray">A Example, B Example</div><div class="gs_gray">Workshop on Agents'
    '</div></td><td><span class="gsc_a_h">2026</span></td></tr></table>'
)


def test_scholar_profile_or_an_honest_refusal(
    onboard_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://scholar.google.com/citations?user=AbC123xyz&hl=en"
    robots = FakeResponse(200, "User-agent: *\nDisallow: /citations?\nAllow: /citations?user=\n")
    Web(monkeypatch, {"https://scholar.google.com/robots.txt": robots, url: SCHOLAR})
    got = run("scholar", "https://scholar.google.de/citations?user=AbC123xyz&cstart=20")
    assert got.items[0].text == (
        "Google Scholar profile: A. Example\nInterests: NLP, Agents\nPublications:\n"
        "- Tool use in small agents (Workshop on Agents; 2026)"
    )
    Web(monkeypatch, {"https://scholar.google.com/robots.txt": robots,
                      url: FakeResponse(429, "")})  # fmt: skip
    refused = run("scholar", url)
    assert "answered 429" in refused.notes[0] and "--publications FILE" in refused.notes[0]
    Web(monkeypatch, {url: html("<p>Please show you're not a robot</p>")})
    assert run("scholar", url).notes == [links.SCHOLAR_REFUSED]
    assert "no Scholar user id" in run("scholar", "https://scholar.google.com/").notes[0]


# ---------------------------------------------------------------- Credly


def test_credly_badges(onboard_paths: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    badge = {"id": "0f3c-badge", "issued_at_date": "2026-04-01",
             "issuer": {"summary": "issued by Example Cloud"},
             "badge_template": {"name": "Cloud Practitioner", "description": "Cloud basics.",
                                "skills": [{"name": "AWS"}, "junk"]}}  # fmt: skip
    api = "https://www.credly.com/users/octo-example/badges.json"
    Web(monkeypatch, {api: FakeResponse(200, {"data": [badge, {"badge_template": "x"}]})})
    got = run("credly", "https://www.credly.com/users/octo-example/badges")
    assert got.items[0].location == "https://www.credly.com/badges/0f3c-badge"
    assert "Credly badge: Cloud Practitioner\nIssued by: issued by Example Cloud" in (
        got.items[0].text
    )
    assert "Skills: AWS" in got.items[0].text
    Web(monkeypatch, {api: FakeResponse(200, {"data": []})})
    assert run("credly", "https://www.credly.com/users/octo-example").notes == [
        "no public badges for octo-example"
    ]
    assert "credly.com/users/NAME" in run("credly", "https://www.credly.com/org/x").notes[0]


# ---------------------------------------------------------------- publications and CLI


def test_publications_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pubs = tmp_path / "pubs.md"
    pubs.write_text(f"Tool use in small agents. Workshop 2026.\n\nContact {FAKE_EMAIL}\n")
    got = run("publications", str(pubs))
    assert got.read == ["pubs.md"] and FAKE_EMAIL not in got.items[0].text
    assert "TXT or MD" in run("publications", str(tmp_path / "pubs.pdf")).notes[0]
    monkeypatch.setattr(config, "ONBOARD_MAX_FILE_BYTES", 5)
    assert "over 5 bytes" in run("publications", str(pubs)).notes[0]


def test_legacy_link_entries_are_read_by_kind(
    onboard_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Web(monkeypatch, {"https://example.org/": html("<p>Hello from a portfolio.</p>")})
    got = collect.COLLECTORS["link"](source_spec("link", "https://example.org/"))
    assert got.items[0].source_type == "page"
    gh = collect.COLLECTORS["link"](source_spec("link", "https://github.com/octo-example"))
    assert "--github octo-example" in gh.notes[0]


def test_init_add_links_by_kind(onboard_paths: Path, tmp_path: Path) -> None:
    pubs = tmp_path / "pubs.txt"
    pubs.write_text("A paper.")
    argv = [
        "init",
        "--add",
        "--link",
        "https://huggingface.co/octo-example",
        "--link",
        "https://www.linkedin.com/in/someone",
        "--link",
        "https://example.org/",
        "--publications",
        str(pubs),
        "--publications",
        str(tmp_path / "missing.txt"),
    ]
    script = Script()
    assert cli.main(argv, console=script.console(interactive=False)) == 0
    kinds = [s["type"] for s in store.load_sources()["sources"]]
    assert kinds == ["huggingface", "page", "publications"]
    assert "data export" in script.text and "publications list not found" in script.text
    assert cli.check_source("feed", "https://example.org/") == (
        "that is a page link, not feed: https://example.org/"
    )
    assert cli.check_source("page", "http://example.org/").startswith("links start with")
    assert json.loads(config.SOURCES_PATH.read_text())["sources"][0]["label"] == (
        "https://huggingface.co/octo-example"
    )


# ---------------------------------------------------------------- hardening


def test_dtd_refused_past_comments_and_in_utf16(
    onboard_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = "https://blog.example.org"
    dtd = '<!DOCTYPE r [<!ENTITY a "aaaaaaaaaa">]><rss><item><title>&a;&a;</title></item></rss>'
    long_comment = '<?xml version="1.0"?><!--' + "x" * 30000 + "-->" + dtd
    utf16 = ('<?xml version="1.0" encoding="UTF-16"?>' + dtd).encode("utf-16")
    Web(monkeypatch, {f"{base}/a.xml": FakeResponse(200, long_comment),
                      f"{base}/b.xml": FakeResponse(200, "")})  # fmt: skip
    assert "declares a DTD" in run("feed", f"{base}/a.xml").notes[0]
    got = collect.Collected()
    with pytest.raises(web.Blocked, match="declares a DTD"):
        links._read_feed(source_spec("feed", f"{base}/b.xml"), got, utf16, f"{base}/b.xml")
    assert not got.items


def test_robots_wildcards_match_without_backtracking() -> None:
    hostile = "/" + "*a" * 40 + "$"
    robots = web.Robots.parse(f"User-agent: *\nDisallow: {hostile}\n")
    started = time.monotonic()
    assert robots.allows("https://h/" + "a" * 2000 + "b")
    assert time.monotonic() - started < 1.0
    assert web.rule_matches("/a*c$", "/abbc") and not web.rule_matches("/a*c$", "/abcd")
    assert web.rule_matches("/a*c", "/abcd") and web.rule_matches("/", "/x")
    assert not web.rule_matches("/x", "/")
    many = "User-agent: *\n" + "Disallow: /d\n" * 1500 + "Disallow: /" + "y" * 600 + "\n"
    assert len(web.Robots.parse(many).rules) == web.MAX_RULES


def test_robots_redirects_failures_and_3xx_without_location(
    onboard_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moved = FakeResponse(301, "", {"Location": "https://www.example.org/robots.txt"})
    fake = Web(monkeypatch, {
        "https://example.org/robots.txt": moved,
        "https://www.example.org/robots.txt": FakeResponse(200, "User-agent: *\nDisallow: /\n"),
    })  # fmt: skip
    assert "asks tools not to read" in run("page", "https://example.org/").notes[0]
    assert fake.urls == ["https://example.org/robots.txt", "https://www.example.org/robots.txt"]
    Web(monkeypatch, {"https://example.org/robots.txt": FakeResponse(429, "")})
    assert "could not be read" in run("page", "https://example.org/").notes[0]
    loop = FakeResponse(302, "", {"Location": "/robots.txt"})
    Web(monkeypatch, {"https://example.org/robots.txt": loop})
    assert "could not be read" in run("page", "https://example.org/").notes[0]
    Web(monkeypatch, {"https://example.org/": FakeResponse(302, "<p>not a page</p>")})
    assert "answered 302" in run("page", "https://example.org/").notes[0]
    # a request that fails is still in the call log
    config.ONBOARD_LOG_PATH.unlink(missing_ok=True)
    Web(monkeypatch, {"https://example.org/": requests.ConnectionError("reset")})
    run("page", "https://example.org/")
    log = store.read_audit()
    assert [(e["action"], e["chars"]) for e in log] == [("GET /robots.txt", 0), ("GET /", 0)]


def test_remote_fields_are_capped_and_bidi_marks_removed(
    onboard_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Web(
        monkeypatch,
        {
            "https://example.org/": html(
                "<p>" + "word " * 5000 + "</p>", "<title>" + "T" * 900 + "</title>"
            )
        },
    )
    item = run("page", "https://example.org/").items[0]
    assert len(item.text) <= links.ITEM_CHARS and "T" * 301 not in item.text
    marks = "".join(map(chr, (0x202E, 0x2066, 0x2028)))
    assert extract.printable(f"ok{marks[0]}gnirts{marks[1]}x{marks[2]}y") == "okgnirtsxy"


def test_net_get_takes_a_lower_byte_cap() -> None:
    class Body:
        url = "https://example.org/"

        def iter_content(self, size: int) -> Any:
            yield b"x" * 10
            yield b"x" * 10

        def close(self) -> None:
            pass

    with pytest.raises(ValueError, match="over 15 bytes"):
        net._capped(Body(), 15)  # type: ignore[arg-type]
    assert net._capped(Body(), 100)._content == b"x" * 20  # type: ignore[arg-type,attr-defined]


def test_robots_near_match_rules_cannot_stall(
    onboard_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    near = "*" + "a" * 509 + "b"
    robots = web.Robots.parse("User-agent: *\n" + f"Disallow: {near}\n" * 1000)
    started = time.monotonic()
    with pytest.raises(web.Blocked, match="too long"):
        robots.allows("https://h/" + "a" * 8000)
    with pytest.raises(web.Blocked, match="too costly"):
        robots.allows("https://h/" + "a" * 2000)
    assert time.monotonic() - started < 1.0
    assert robots.allows("https://h/short")
    rules = FakeResponse(200, "User-agent: *\n" + f"Disallow: {near}\n" * 1000)
    Web(monkeypatch, {"https://example.org/robots.txt": rules})
    got = run("page", "https://example.org/" + "a" * 2000)
    assert "too costly to check" in got.notes[0] and not got.items


def test_a_secret_across_the_cut_is_masked_whole(
    onboard_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = FAKE_KEY
    pad = "x" * (links.PAGE_CHARS - 12)
    Web(monkeypatch, {"https://example.org/": html(f"<p>{pad} {key} tail</p>")})
    text = run("page", "https://example.org/").items[0].text
    assert key[:8] not in text and "sk-ant" not in text
    feed = f"<rss><item><title>t</title><description>{'y' * 590} {key}</description></item></rss>"
    Web(monkeypatch, {"https://example.org/f.xml": FakeResponse(200, feed)})
    assert "sk-ant" not in run("feed", "https://example.org/f.xml").items[0].text
