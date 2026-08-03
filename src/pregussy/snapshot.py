"""Where a running store puts itself so a restart doesn't erase the party.

The app keeps every event in memory behind a single lock and writes the whole thing
out after each mutation. That shape is deliberate — a room full of phones is a tiny
amount of data — so persistence only ever needs one job: hand back the same blob
after a restart. Hence a blob interface rather than a relational schema.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

Payload = dict[str, Any]


class Snapshot(Protocol):
    def load(self) -> Payload: ...
    def save(self, payload: Payload) -> None: ...


class NullSnapshot:
    """Remembers nothing. For tests that don't care about restarts."""

    def load(self) -> Payload:
        return {}

    def save(self, payload: Payload) -> None:
        return None


class FileSnapshot:
    """A JSON file next to the process. Fine for local dev and a real disk mount."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> Payload:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("snapshot_load_failed, starting empty: %s", exc)
            return {}

    def save(self, payload: Payload) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:  # pragma: no cover - disk trouble shouldn't stop a party
            logger.warning("snapshot_write_failed: %s", exc)


class PostgresSnapshot:
    """The whole store as one JSONB row.

    This is what makes a free hosting tier usable: those spin a service down when it
    goes quiet and wipe its filesystem on every deploy, either of which would drop
    every board mid-party. Reloading one row on boot brings the room back.
    """

    TABLE = "pregussy_snapshot"

    def __init__(self, dsn: str) -> None:
        self.dsn = normalise_dsn(dsn)
        self._lock = threading.Lock()
        self._conn: Any = None
        self._ready = False
        with self._lock:
            self._ensure_table()

    def _ensure_table(self) -> bool:
        """Create the table if we can. A dead database must not stop the app booting —
        Render deletes a free Postgres after 30 days, and losing durability on the
        night of a party is far better than the site refusing to come up at all."""
        if self._ready:
            return True
        try:
            self._execute(
                f"CREATE TABLE IF NOT EXISTS {self.TABLE} ("
                "  id smallint PRIMARY KEY,"
                "  events jsonb NOT NULL,"
                "  updated_at timestamptz NOT NULL DEFAULT now())"
            )
            self._ready = True
        except RuntimeError as exc:
            logger.warning("snapshot_unavailable, running in memory only: %s", exc)
        return self._ready

    # -- connection ---------------------------------------------------------

    def _connect(self) -> Any:
        import psycopg

        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, autocommit=True, connect_timeout=10)
        return self._conn

    def _drop(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:  # noqa: BLE001 - closing a broken socket is best-effort
            pass
        self._conn = None

    def _execute(self, sql: str, params: tuple[Any, ...] | None = None) -> Any:
        """Run a statement, retrying once — idle Postgres connections get reaped."""
        last: Exception | None = None
        for _ in range(2):
            try:
                return self._connect().execute(sql, params)
            except Exception as exc:  # noqa: BLE001 - any driver error is worth a retry
                last = exc
                self._drop()
        raise RuntimeError(f"postgres statement failed: {last}") from last

    # -- snapshot -----------------------------------------------------------

    def load(self) -> Payload:
        with self._lock:
            if not self._ensure_table():
                return {}
            try:
                row = self._execute(f"SELECT events FROM {self.TABLE} WHERE id = 1").fetchone()
            except RuntimeError as exc:
                logger.warning("snapshot_load_failed, starting empty: %s", exc)
                return {}
        return row[0] if row else {}

    def save(self, payload: Payload) -> None:
        from psycopg.types.json import Jsonb

        with self._lock:
            if not self._ensure_table():
                return
            try:
                self._execute(
                    f"INSERT INTO {self.TABLE} (id, events, updated_at) VALUES (1, %s, now()) "
                    "ON CONFLICT (id) DO UPDATE SET events = EXCLUDED.events, updated_at = now()",
                    (Jsonb(payload),),
                )
            except RuntimeError as exc:
                # A failed write must not break the tap that triggered it; the next
                # mutation writes the whole state again anyway.
                logger.warning("snapshot_write_failed: %s", exc)
                self._ready = False


def normalise_dsn(dsn: str) -> str:
    """Hosting providers hand out `postgres://`; psycopg wants `postgresql://`."""
    if dsn.startswith("postgres://"):
        return "postgresql://" + dsn[len("postgres://") :]
    return dsn


def from_environment() -> Snapshot:
    """Postgres when `DATABASE_URL` is set, otherwise a local JSON file."""
    dsn = os.getenv("DATABASE_URL", "").strip()
    if dsn:
        logger.info("snapshot_backend", extra={"backend": "postgres"})
        return PostgresSnapshot(dsn)
    path = Path(os.getenv("PREGUSSY_DATA", "data/events.json"))
    logger.info("snapshot_backend", extra={"backend": "file", "path": str(path)})
    return FileSnapshot(path)
