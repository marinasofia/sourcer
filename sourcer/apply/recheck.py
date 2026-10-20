"""Recheck one posting against the company's own board right before it is sent.

A posting can sit on the board for weeks after it was collected, and
community lists report their own dates, not the company's. Before a match is
texted, this asks the company's job board directly:

  still listed?        gone or unlisted means it is closed
  when was it posted?  the board's own first published date, never an
                       "updated" date (an edit is not a new posting)
  does its program end in the past?
                       "Duration: June to August 2026" read in October

A repost gets a new id and a new posted date on these boards, so it passes
as new. No model calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from .. import config, net
from .verify import (
    CLOSED_RE,
    GONE_STATUSES,
    HTTP_TIMEOUT,
    UA,
    WORKDAY_PULLED,
    WORKDAY_RE,
    workday_board_answers,
)

# the program term check has one home in config; kept here for callers that import it
MONTHS, TERM_RE, TERM_END, program_ended = (
    config.MONTHS,
    config.TERM_RE,
    config.TERM_END,
    config.program_ended,
)


@dataclass
class Official:
    """What the company's own board says. ``listed`` None means unknown."""

    listed: bool | None
    posted: str | None = None  # ISO date
    via: str = "page"


def _get(url: str, **kw: Any) -> requests.Response | None:
    try:
        return net.get(url, timeout=HTTP_TIMEOUT, headers=UA, **kw)
    except requests.RequestException:
        return None


def _get_public(url: str) -> requests.Response | None:
    """GET a page on an arbitrary host; net.get checks every redirect hop and
    pins each connection to the address it checked."""
    return _get(url)


def _day(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):  # Lever: milliseconds
        return datetime.fromtimestamp(value / 1000, UTC).date().isoformat()
    return str(value)[:10] or None


def _greenhouse(parts: Any, segs: list[str]) -> Official | None:
    slug, job = None, None
    if segs[:1] == ["embed"]:
        q = parse_qs(parts.query)
        slug, job = q.get("for", [None])[0], q.get("token", [None])[0]
    elif len(segs) >= 3 and segs[1] == "jobs":
        slug, job = segs[0], segs[2]
    job = job or parse_qs(parts.query).get("gh_jid", [None])[0]
    if not (slug and job and job.isdigit()):
        return None
    r = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job}")
    if r is None:
        return Official(None, via="greenhouse")
    if r.status_code in GONE_STATUSES:
        return Official(False, via="greenhouse")
    if not r.ok:
        return Official(None, via="greenhouse")
    return Official(True, _day(r.json().get("first_published")), "greenhouse")


def _ashby(segs: list[str]) -> Official | None:
    if len(segs) < 2:
        return None
    slug, job = segs[0], segs[1]
    r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if r is None or not r.ok:
        return Official(None, via="ashby")
    hit = next((j for j in r.json().get("jobs", []) if j.get("id") == job), None)
    if not hit or hit.get("isListed") is False:
        return Official(False, via="ashby")
    return Official(True, _day(hit.get("publishedAt")), "ashby")


def _lever(segs: list[str]) -> Official | None:
    if len(segs) < 2:
        return None
    r = _get(f"https://api.lever.co/v0/postings/{segs[0]}/{segs[1]}")
    if r is None:
        return Official(None, via="lever")
    if r.status_code in GONE_STATUSES:
        return Official(False, via="lever")
    if not r.ok:
        return Official(None, via="lever")
    return Official(True, _day(r.json().get("createdAt")), "lever")


def _workday(url: str) -> Official | None:
    m = WORKDAY_RE.match(url)
    if not m:
        return None
    tenant, wd, site, slug = m.groups()
    r = _get(f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{slug}")
    if r is None:
        return Official(None, via="workday")
    if r.status_code in GONE_STATUSES or (
        r.status_code == WORKDAY_PULLED and workday_board_answers(tenant, wd, site)
    ):
        return Official(False, via="workday")
    info = r.json().get("jobPostingInfo") if r.ok else None
    if not info:
        return Official(None, via="workday")
    open_ = info.get("canApply", True) and info.get("posted", True)
    return Official(bool(open_), _day(info.get("startDate")), "workday")


def _smartrecruiters(segs: list[str]) -> Official | None:
    if len(segs) < 2:
        return None
    job = segs[1].split("-")[0]
    r = _get(f"https://api.smartrecruiters.com/v1/companies/{segs[0]}/postings/{job}")
    if r is None:
        return Official(None, via="smartrecruiters")
    if r.status_code in GONE_STATUSES:
        return Official(False, via="smartrecruiters")
    if not r.ok:
        return Official(None, via="smartrecruiters")
    return Official(True, _day(r.json().get("releasedDate")), "smartrecruiters")


def _eightfold(host: str, segs: list[str]) -> Official | None:
    """Closed roles drop out of the public details API ("Position not found")."""
    if segs[:2] != ["careers", "job"] or len(segs) < 3 or not segs[2].isdigit():
        return None
    r = _get(f"https://{host}/api/pcsx/position_details", params={"position_id": segs[2]})
    if r is None or not (r.ok or r.status_code in GONE_STATUSES):
        return Official(None, via="eightfold")
    reply = r.json()
    if r.status_code in GONE_STATUSES or not reply.get("data"):
        return Official(False, via="eightfold")
    ts = reply["data"].get("postedTs")
    return Official(True, _day(ts * 1000) if ts else None, "eightfold")


def _oracle(host: str, segs: list[str]) -> Official | None:
    """Oracle answers an empty list for a requisition that is gone, and gives
    the date it stops being posted."""
    if "sites" not in segs or "job" not in segs:
        return None
    site, job = segs[segs.index("sites") + 1], segs[segs.index("job") + 1 :][:1]
    if not job:
        return None
    r = _get(
        f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
        f'?onlyData=true&finder=ById;Id="{job[0]}",siteNumber={site}'
    )
    if r is None or not r.ok:
        return Official(None, via="oracle")
    items = r.json().get("items") or []
    if not items:
        return Official(False, via="oracle")
    ends = _day(items[0].get("ExternalPostedEndDate"))
    still = not ends or ends >= config.utc_now().date().isoformat()
    return Official(still, _day(items[0].get("ExternalPostedStartDate")), "oracle")


def official(url: str) -> Official:
    """Ask the board behind ``url`` whether the posting is listed and when it
    was first posted. Other hosts: the page itself (gone or closed text)."""
    parts = urlparse(url or "")
    host = (parts.hostname or "").lower()
    segs = [s for s in parts.path.split("/") if s]
    found = None
    if host.endswith("greenhouse.io"):
        found = _greenhouse(parts, segs)
    elif host == "jobs.ashbyhq.com":
        found = _ashby(segs)
    elif host == "jobs.lever.co":
        found = _lever(segs)
    elif host.endswith("myworkdayjobs.com"):
        found = _workday(url)
    elif host == "jobs.smartrecruiters.com":
        found = _smartrecruiters(segs)
    elif host.endswith(".eightfold.ai"):
        found = _eightfold(host, segs)
    elif host.endswith(".oraclecloud.com"):
        found = _oracle(host, segs)
    if found is not None:
        return found
    r = _get_public(url)  # any host: only public addresses, every redirect checked
    if r is None or not r.ok:
        return Official(False if r is not None and r.status_code in GONE_STATUSES else None)
    return Official(False if CLOSED_RE.search(r.text[:400_000]) else None)


def why_not_send(row: dict[str, Any], today: date | None = None) -> tuple[str | None, Official]:
    """(reason to hold the posting back, what the board said). Reason None
    means it is fine to send. A role the board still lists is open whatever
    its age (config.is_fresh); only its program dates can hold it back."""
    today = today or config.utc_now().date()
    board = official(row.get("url") or "")
    if board.listed is False:
        return "no longer listed on the company's board", board
    text = " ".join(str(row.get(k) or "")[:4000] for k in ("title", "start_date", "description"))
    ended = program_ended(text, today)
    if ended:
        return f"its program dates ended by {ended}", board
    return None, board
