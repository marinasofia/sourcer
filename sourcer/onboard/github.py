"""GitHub collector: a user's public repositories through the REST API.

Reads, per repository: name, description, topics, languages, created and
last pushed dates, stars, fork and archive flags, and the start of the
README. Unauthenticated by default (60 requests an hour); ``GITHUB_TOKEN``
raises the limit and is sent only to api.github.com. Forks are skipped.
On a rate limit the collector stops and keeps what it already read.
"""

from __future__ import annotations

import os
import re
from typing import Any

import requests

from .. import config, net
from ..log import get_logger
from .items import Collected, SourceItem, seen
from .privacy import redact
from .store import audit

log = get_logger(__name__)

API = "https://api.github.com"
HOST = "api.github.com"
USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
README_CHARS = 2500


class RateLimited(Exception):
    """GitHub refused for the rate limit; ``reset`` is its epoch reset time."""

    def __init__(self, reset: str | None) -> None:
        super().__init__(f"GitHub rate limit reached (resets at epoch {reset or 'unknown'})")
        self.reset = reset


def valid_username(name: str) -> bool:
    return bool(USERNAME_RE.match(name or ""))


def _headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {
        "Accept": accept,
        "User-Agent": "sourcer-onboard",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get(path: str, source_id: str, accept: str | None = None) -> requests.Response | None:
    """GET one API path. None on 404; RateLimited on a rate limit refusal."""
    resp = net.get(
        API + path,
        headers=_headers(accept) if accept else _headers(),
        timeout=config.REQUEST_TIMEOUT,
        allow_redirects=False,
    )
    audit(HOST, f"GET {path.split('?', maxsplit=1)[0]}", [source_id], len(resp.content or b""))
    if resp.status_code in (403, 429) and (
        resp.headers.get("X-RateLimit-Remaining") == "0" or resp.status_code == 429
    ):
        raise RateLimited(resp.headers.get("X-RateLimit-Reset"))
    if resp.status_code == 404 or 300 <= resp.status_code < 400:
        return None  # missing, or moved (redirects are not followed)
    resp.raise_for_status()
    return resp


def list_repos(user: str, source_id: str, limit: int) -> list[dict[str, Any]]:
    """The user's own public repositories, most recently pushed first."""
    resp = _get(f"/users/{user}/repos?type=owner&sort=pushed&per_page=100", source_id)
    if resp is None:
        raise LookupError(f"GitHub user not found: {user}")
    repos = [r for r in resp.json() if isinstance(r, dict) and not r.get("fork")]
    return [r for r in repos if REPO_RE.match(str(r.get("name") or ""))][:limit]


def _languages(user: str, name: str, source_id: str) -> str:
    resp = _get(f"/repos/{user}/{name}/languages", source_id)
    data = resp.json() if resp is not None else {}
    total = sum(v for v in data.values() if isinstance(v, int)) or 1
    return ", ".join(
        f"{lang} {round(100 * size / total)}%"
        for lang, size in sorted(data.items(), key=lambda kv: -kv[1])
        if isinstance(size, int)
    )


def _readme(user: str, name: str, source_id: str) -> str:
    resp = _get(f"/repos/{user}/{name}/readme", source_id, accept="application/vnd.github.raw")
    return "" if resp is None else resp.text[:README_CHARS]


def repo_excerpt(repo: dict[str, Any], languages: str, readme: str) -> str:
    lines = [
        f"Repository: {repo.get('name')}",
        f"Description: {repo.get('description') or ''}",
        f"Topics: {', '.join(repo.get('topics') or [])}",
        f"Languages: {languages or repo.get('language') or ''}",
        f"Created: {str(repo.get('created_at') or '')[:10]}",
        f"Last pushed: {str(repo.get('pushed_at') or '')[:10]}",
        f"Stars: {repo.get('stargazers_count') or 0}",
    ]
    if repo.get("archived"):
        lines.append("Archived: yes")
    if repo.get("homepage"):
        lines.append(f"Homepage: {repo['homepage']}")
    if readme:
        lines += ["", "README (start):", readme]
    return redact("\n".join(lines))


def collect(source: dict[str, Any]) -> Collected:
    user = str(source["value"]).strip().lstrip("@")
    out = Collected()
    if not valid_username(user):
        out.notes.append(f"not a GitHub username: {user!r}")
        return out
    try:
        repos = list_repos(user, source["id"], config.ONBOARD_GITHUB_MAX_REPOS)
        same = 0
        for repo in repos:
            name = repo["name"]
            url = str(repo.get("html_url") or f"https://github.com/{user}/{name}")
            pushed = str(repo.get("pushed_at") or "")
            if pushed and seen(source, url) == pushed:
                same += 1  # nothing pushed since it was read: README and languages are the same
                continue
            excerpt = repo_excerpt(
                repo, _languages(user, name, source["id"]), _readme(user, name, source["id"])
            )
            out.items.append(SourceItem(source["id"], "github", url, excerpt))
            out.read.append(name)
            if pushed:
                out.stamps[url] = pushed
        if same:
            out.notes.append(f"{same} repos unchanged since the last scan")
    except RateLimited as exc:
        out.notes.append(f"{exc}; kept {len(out.items)} repos. Set GITHUB_TOKEN for more.")
    except (LookupError, requests.RequestException, ValueError) as exc:
        log.warning("github read failed for %s: %s", user, type(exc).__name__)
        out.notes.append(f"GitHub not read: {exc}")
    return out
