"""SQLite database component: WAL mode, single dedicated worker thread (D7).

All SQL runs on one dedicated thread via a `ThreadPoolExecutor(max_workers=1)`
so the `sqlite3.Connection` is never touched from more than one thread at a
time (D1: never block the event loop; sqlite3 connections are not safe to
share across threads without serialization).
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

from nomad.core.errors import StorageError
from nomad.core.lifecycle import ComponentState
from nomad.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class Database:
    """Component wrapping a single sqlite3 connection on a dedicated thread."""

    name = "database"

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._state = ComponentState.NEW
        self._in_transaction = False

    @property
    def state(self) -> ComponentState:
        return self._state

    async def start(self) -> None:
        self._state = ComponentState.STARTING
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nomad-db")
        try:
            await self._run(self._open_connection)
        except Exception as exc:  # noqa: BLE001
            self._state = ComponentState.FAILED
            raise StorageError(f"Failed to open database at {self._path}") from exc
        self._state = ComponentState.STARTED

    async def stop(self) -> None:
        self._state = ComponentState.STOPPING
        if self._conn is not None:
            try:
                await self._run(self._conn.close)
            except Exception as exc:  # noqa: BLE001
                logger.error("Error closing database", extra={"error": str(exc)})
        if self._executor is not None:
            self._executor.shutdown(wait=True)
        self._conn = None
        self._state = ComponentState.STOPPED

    def _open_connection(self) -> None:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        self._conn = conn

    async def _run(self, fn: Callable[[], T]) -> T:
        if self._executor is None:
            raise StorageError("Database is not started")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn)

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StorageError("Database is not started")
        return self._conn

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run an INSERT/UPDATE/DELETE (or DDL). Returns lastrowid."""

        def _do() -> int:
            conn = self._require_conn()
            cur = conn.execute(sql, params)
            if not self._in_transaction:
                conn.commit()
            return cur.lastrowid or 0

        try:
            return await self._run(_do)
        except sqlite3.Error as exc:
            raise StorageError(f"execute failed: {exc}", {"sql": sql}) from exc

    async def executemany(self, sql: str, seq_of_params: Sequence[Sequence[Any]]) -> None:
        def _do() -> None:
            conn = self._require_conn()
            conn.executemany(sql, seq_of_params)
            if not self._in_transaction:
                conn.commit()

        try:
            await self._run(_do)
        except sqlite3.Error as exc:
            raise StorageError(f"executemany failed: {exc}", {"sql": sql}) from exc

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        def _do() -> dict[str, Any] | None:
            conn = self._require_conn()
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row is not None else None

        try:
            return await self._run(_do)
        except sqlite3.Error as exc:
            raise StorageError(f"fetch_one failed: {exc}", {"sql": sql}) from exc

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        def _do() -> list[dict[str, Any]]:
            conn = self._require_conn()
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

        try:
            return await self._run(_do)
        except sqlite3.Error as exc:
            raise StorageError(f"fetch_all failed: {exc}", {"sql": sql}) from exc

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Async context manager running a block of statements as one transaction.

        All statements issued via `execute`/`executemany` while inside this
        block run against the same worker thread and commit/rollback together.
        """

        def _begin() -> None:
            self._require_conn().execute("BEGIN")

        def _commit() -> None:
            self._require_conn().commit()

        def _rollback() -> None:
            self._require_conn().rollback()

        await self._run(_begin)
        self._in_transaction = True
        try:
            yield
        except Exception:
            await self._run(_rollback)
            raise
        else:
            await self._run(_commit)
        finally:
            self._in_transaction = False
