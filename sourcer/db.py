"""Storage facade. ``SOURCER_BACKEND`` selects the implementation at import time.

Every caller imports this module and nothing else. The two backends expose the
same functions, enforced by the ``Backend`` protocol below: mypy rejects a
backend module whose signatures drift.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from . import snowdb, sqlitedb

STATUSES = sqlitedb.STATUSES
APPLICATION_STATUSES = sqlitedb.APPLICATION_STATUSES
APPLICATION_EVENTS = sqlitedb.APPLICATION_EVENTS
posting_hash = sqlitedb.posting_hash
now_iso = sqlitedb.now_iso

BACKENDS = ("sqlite", "snowflake")
BACKEND = os.environ.get("SOURCER_BACKEND", "sqlite")


class Connection(Protocol):
    """The slice of the DB-API surface the package relies on.

    ``execute`` takes qmark parameters and returns rows addressable by column
    name; ``sqlite3.Row`` and ``snowdb.Conn`` both satisfy it.
    """

    def execute(self, sql: str, params: Sequence[Any] = (), /) -> Any: ...

    def executemany(self, sql: str, seq_of_params: Sequence[Sequence[Any]], /) -> Any: ...

    def commit(self) -> None: ...

    def close(self) -> None: ...


class Backend(Protocol):
    def connect(self, path: Path | None = None) -> Any: ...

    def seen_hashes(self, conn: Any) -> set[str]: ...

    def seen_identities(self, conn: Any) -> dict[str, str]: ...

    def insert_posting(self, conn: Any, p: dict[str, Any]) -> None: ...

    def save_description(self, conn: Any, identity: str, text: str) -> None: ...

    def insert_run_log(
        self, conn: Any, stats: dict[str, Any], run_ts: str | None = None
    ) -> None: ...

    def set_status(
        self, conn: Any, posting_id: int, status: str, note: str | None = None
    ) -> int: ...

    def record_feedback(self, conn: Any, posting_id: int, value: int) -> int: ...

    def list_feedback(self, conn: Any) -> list[dict[str, Any]]: ...

    def list_digest_postings(self, conn: Any, threshold: int) -> list[dict[str, Any]]: ...

    def mark_telegram_notified(self, conn: Any, posting_ids: Sequence[int]) -> None: ...

    def weekly_metrics(self, conn: Any) -> dict[str, Any]: ...

    def find_scored_twin(
        self, conn: Any, company: str, title: str, current: Any = None
    ) -> Any | None: ...

    def upsert_companies(self, conn: Any, companies: Sequence[dict[str, Any]]) -> int: ...

    def list_companies(self, conn: Any) -> list[dict[str, Any]]: ...

    def save_company_probe(self, conn: Any, result: dict[str, Any]) -> None: ...

    def save_discovered_companies(self, conn: Any, candidates: Sequence[dict[str, Any]]) -> int: ...

    def insert_application(self, conn: Any, app: dict[str, Any]) -> int: ...

    def add_application_event(
        self, conn: Any, application_id: int, kind: str, detail: str | None = None
    ) -> None: ...

    def set_application_status(
        self, conn: Any, application_id: int, status: str, detail: str | None = None
    ) -> int: ...

    def list_applications(self, conn: Any) -> list[dict[str, Any]]: ...

    def list_application_events(self, conn: Any, application_id: int) -> list[dict[str, Any]]: ...


def _select(name: str) -> Backend:
    if name == "sqlite":
        return sqlitedb
    if name == "snowflake":
        return snowdb
    raise ValueError(f"SOURCER_BACKEND must be one of {BACKENDS}, got {name!r}")


_impl: Backend = _select(BACKEND)

connect = _impl.connect
seen_hashes = _impl.seen_hashes
seen_identities = _impl.seen_identities
insert_posting = _impl.insert_posting
save_description = _impl.save_description
insert_run_log = _impl.insert_run_log
set_status = _impl.set_status
find_scored_twin = _impl.find_scored_twin
upsert_companies = _impl.upsert_companies
list_companies = _impl.list_companies
save_company_probe = _impl.save_company_probe
save_discovered_companies = _impl.save_discovered_companies
record_feedback = _impl.record_feedback
list_feedback = _impl.list_feedback
list_digest_postings = _impl.list_digest_postings
mark_telegram_notified = _impl.mark_telegram_notified
weekly_metrics = _impl.weekly_metrics
insert_application = _impl.insert_application
add_application_event = _impl.add_application_event
set_application_status = _impl.set_application_status
list_applications = _impl.list_applications
list_application_events = _impl.list_application_events
