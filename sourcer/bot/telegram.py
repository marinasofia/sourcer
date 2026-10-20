"""Allowlisted Telegram digest, feedback, and company URL intake."""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from .. import config, db
from ..discovery.models import CompanyCandidate
from ..log import get_logger
from ..net import SafeSession, validate_public_https_url
from ..preferences import adjusted_score
from ..probe import RateLimiter, build_target, probe_target

logger = get_logger(__name__)

URL_RE = re.compile(r"https://[^\s<>]+")
API_ROOT = "https://api.telegram.org"


class Response(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> Any: ...


class Transport(Protocol):
    def post(self, url: str, **kwargs: Any) -> Response: ...


@dataclass(frozen=True)
class Settings:
    token: str
    chat_ids: frozenset[int]
    user_ids: frozenset[int]
    dry_run: bool = True
    score_threshold: int = 75

    @classmethod
    def from_config(cls) -> Settings:
        return cls(
            config.TELEGRAM_TOKEN,
            config.TELEGRAM_ALLOWED_CHAT_IDS,
            config.TELEGRAM_ALLOWED_USER_IDS,
            config.DRY_RUN,
            config.TELEGRAM_SCORE_THRESHOLD,
        )

    def validate(self, *, network_required: bool = True) -> None:
        if not self.chat_ids or not self.user_ids:
            raise ValueError("Telegram requires nonempty chat and user allowlists")
        if len(self.chat_ids) != 1:
            raise ValueError("Personal Telegram mode requires exactly one chat")
        if network_required and not self.token:
            raise ValueError("SOURCER_TELEGRAM_TOKEN is required")


class TelegramClient:
    def __init__(self, settings: Settings, transport: Transport | None = None) -> None:
        self.settings = settings
        self.transport = transport or SafeSession()

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        self.settings.validate()
        if self.settings.dry_run:
            raise PermissionError("Telegram network calls are disabled in dry run")
        if method not in {"sendMessage", "getUpdates"}:
            raise PermissionError("Telegram method is not allowed")
        if method == "sendMessage" and payload.get("chat_id") not in self.settings.chat_ids:
            raise PermissionError("Telegram chat is not allowlisted")
        try:
            response = self.transport.post(
                f"{API_ROOT}/bot{self.settings.token}/{method}",
                json=payload,
                timeout=35,
                allow_redirects=False,
            )
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError):
            # Requests exceptions may include the bot token in the URL.
            raise RuntimeError(
                "Telegram request failed; check connectivity and credentials"
            ) from None
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise RuntimeError("Telegram API rejected the request")
        return body.get("result")

    def send(self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> bool:
        if chat_id not in self.settings.chat_ids:
            raise PermissionError("Telegram chat is not allowlisted")
        if self.settings.dry_run:
            return False
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4096]}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        self._call("sendMessage", payload)
        return True

    def updates(self, offset: int | None = None) -> list[dict[str, Any]]:
        if self.settings.dry_run:
            return []
        payload: dict[str, Any] = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload)
        return result if isinstance(result, list) else []


def _authorized(update: dict[str, Any], settings: Settings) -> tuple[int, int] | None:
    body = update.get("callback_query") or update.get("message") or {}
    sender = body.get("from") or {}
    message = body.get("message") if "callback_query" in update else body
    chat = (message or {}).get("chat") or {}
    try:
        pair = int(chat["id"]), int(sender["id"])
    except (KeyError, TypeError, ValueError):
        return None
    if pair[0] not in settings.chat_ids or pair[1] not in settings.user_ids:
        return None
    return pair


def _feedback_buttons(posting_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "👍", "callback_data": f"feedback:1:{posting_id}"},
                {"text": "👎", "callback_data": f"feedback:-1:{posting_id}"},
                {"text": "Shortlist", "callback_data": f"status:shortlist:{posting_id}"},
            ]
        ]
    }


def _posting_text(posting: dict[str, Any], score: int) -> str:
    gap = str(posting.get("rationale") or "No gap summary available.").splitlines()[0]
    return (
        f"{posting['company']}\n{posting['title']}\n"
        f"Score: {score}\n{posting.get('location') or 'Location unavailable'}\n"
        f"{gap[:500]}\n{posting['url']}"
    )


def send_morning_digest(
    conn: db.Connection, client: TelegramClient, chat_id: int
) -> list[dict[str, Any]]:
    """Send unnotified strong matches and mark only successfully sent rows."""
    client.settings.validate(network_required=not client.settings.dry_run)
    feedback = db.list_feedback(conn)
    rows = db.list_digest_postings(conn, max(0, client.settings.score_threshold - 5))
    ranked = []
    for row in rows:
        if not config.is_fresh(row.get("posted_at")):
            continue
        score = adjusted_score(row, feedback)
        if score >= client.settings.score_threshold:
            ranked.append((row, score))
    ranked.sort(key=lambda item: (str(item[0]["company"]).casefold(), -item[1]))
    preview = []
    for row, score in ranked:
        preview.append({**row, "adjusted_score": score})
        if client.send(chat_id, _posting_text(row, score), _feedback_buttons(int(row["id"]))):
            db.mark_telegram_notified(conn, [int(row["id"])])
    return preview


def weekly_report_text(metrics: dict[str, Any]) -> str:
    lines = [f"Weekly Sourcer report\nCompanies added: {metrics['companies_added']}"]
    for row in metrics["sources"]:
        seen = int(row.get("seen") or 0)
        applied = int(row.get("applied") or 0)
        responses = int(row.get("responses") or 0)
        rate = 0 if applied == 0 else round(100 * responses / applied)
        lines.append(
            f"{row.get('source') or 'unknown'}: {seen} seen, {applied} applied, "
            f"{responses} responses, {rate}% response rate"
        )
    return "\n".join(lines)


def send_weekly_report(conn: db.Connection, client: TelegramClient, chat_id: int) -> str:
    text = weekly_report_text(db.weekly_metrics(conn))
    client.send(chat_id, text)
    return text


def _add_company_url(conn: db.Connection, url: str) -> str:
    safe_url = validate_public_https_url(url)
    target = build_target(safe_url)
    candidate = CompanyCandidate(
        name=target.name,
        domain=target.domains[0] if target.domains else None,
        source="telegram",
        source_url=safe_url,
    )
    record = candidate.record()
    db.save_discovered_companies(conn, [record])
    result = probe_target(target, limiter=RateLimiter())
    db.save_company_probe(conn, result.__dict__)
    if result.ats_type:
        return f"Added {result.name}. Found {result.ats_type}: {result.hit_url}"
    return f"Added {result.name}, but no supported job board was found."


def handle_update(
    conn: db.Connection, client: TelegramClient, update: dict[str, Any]
) -> str | None:
    if client.settings.dry_run:
        return None
    identity = _authorized(update, client.settings)
    if identity is None:
        return None
    chat_id, _ = identity
    callback = update.get("callback_query") or {}
    data = str(callback.get("data") or "")
    match = re.fullmatch(r"feedback:(-?1):(\d+)", data)
    if match:
        db.record_feedback(conn, int(match.group(2)), int(match.group(1)))
        client.send(chat_id, "Preference saved.")
        return "feedback"
    match = re.fullmatch(r"status:(shortlist):(\d+)", data)
    if match:
        db.set_status(conn, int(match.group(2)), match.group(1))
        client.send(chat_id, "Added to shortlist. No application was submitted.")
        return "shortlist"
    message = update.get("message") or {}
    url_match = URL_RE.search(str(message.get("text") or ""))
    if url_match:
        reply = _add_company_url(conn, url_match.group(0).rstrip(".,)"))
        client.send(chat_id, reply)
        return "url"
    return None


def run() -> None:
    """Poll Telegram. Dry run exits without opening a network connection."""
    settings = Settings.from_config()
    settings.validate(network_required=not settings.dry_run)
    if settings.dry_run:
        return
    client = TelegramClient(settings)
    conn = db.connect()
    offset = None
    while True:
        try:
            updates = client.updates(offset)
            for update in updates:
                offset = max(offset or 0, int(update["update_id"]) + 1)
                try:
                    handle_update(conn, client, update)
                except Exception:  # one bad update is skipped, never replayed forever
                    logger.warning("Telegram update failed validation or processing")
        except (requests.RequestException, RuntimeError):
            logger.warning("Telegram polling unavailable; retrying in 10 seconds")
            time.sleep(10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sourcer telegram", description=__doc__)
    parser.add_argument("action", nargs="?", choices=("poll", "morning", "weekly"), default="poll")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.action == "poll":
            run()
        else:
            settings = Settings.from_config()
            settings.validate(network_required=not settings.dry_run)
            conn = db.connect()
            client = TelegramClient(settings)
            for chat_id in sorted(settings.chat_ids):
                if args.action == "morning":
                    preview = send_morning_digest(conn, client, chat_id)
                    print(f"Telegram digest: {len(preview)} posting(s)")
                else:
                    send_weekly_report(conn, client, chat_id)
                    print("Telegram weekly report ready")
    except (ValueError, PermissionError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
