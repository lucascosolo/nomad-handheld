"""A stopwatch that nothing has to tick.

The naive stopwatch is a task incrementing a counter, and on this device that
task dies with the process, stalls when the governor is busy, and drifts
against wall time either way. So there is no task: a stopwatch is stored as
`(started_at, accumulated_seconds, running)` and *read* by arithmetic against
the current time. Start writes a timestamp, stop folds the elapsed span into
the accumulator and clears it, and elapsed is `accumulated + (now - started_at)`
whenever it is running. That is the whole mechanism, and it is why closing the
screen, suspending the process, or rebooting the Pi costs nothing — the state
was never in memory to lose.

Laps are stored as elapsed offsets rather than absolute times for the same
reason: an offset stays true across a reboot and across a clock correction,
where a wall-clock lap list quietly becomes fiction the first time NTP steps
the clock.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from nomad.core.config import UtilitiesConfig
from nomad.core.logging import get_logger
from nomad.storage.db import Database
from nomad.utilities.errors import UtilityError

logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def format_duration(seconds: float) -> str:
    """`1h 04m 09.2s`, dropping the units that are zero from the left.

    Here rather than in a tool because three call sites need it and a duration
    rendered three different ways on one small screen reads as three different
    numbers.
    """
    seconds = max(0.0, seconds)
    hours, rest = divmod(int(seconds), 3600)
    minutes, whole = divmod(rest, 60)
    fraction = seconds - int(seconds)
    if hours:
        return f"{hours}h {minutes:02d}m {whole:02d}s"
    if minutes:
        return f"{minutes}m {whole:02d}s"
    return f"{whole + fraction:.1f}s"


class Stopwatch(BaseModel):
    """One stopwatch, as stored. `elapsed_seconds` is computed, never stored."""

    id: str
    label: str
    running: bool
    started_at: datetime | None = None
    accumulated_seconds: float = 0.0
    laps: list[float] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    stopped_at: datetime | None = None

    def elapsed_seconds(self, now: datetime) -> float:
        """Total run time as of `now`. Pure — this is what makes it durable."""
        if not self.running or self.started_at is None:
            return self.accumulated_seconds
        return self.accumulated_seconds + max(
            0.0, (now - self.started_at).total_seconds()
        )


def _row_to_stopwatch(row: dict[str, Any]) -> Stopwatch:
    return Stopwatch(
        id=row["id"],
        label=row["label"],
        running=bool(row["running"]),
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        accumulated_seconds=float(row["accumulated_seconds"]),
        laps=[float(v) for v in json.loads(row["laps_json"])],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        stopped_at=datetime.fromisoformat(row["stopped_at"]) if row["stopped_at"] else None,
    )


class StopwatchStore:
    """Durable stopwatches over a `Database`."""

    def __init__(
        self,
        db: Database,
        *,
        config: UtilitiesConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._config = config or UtilitiesConfig()
        self._clock = clock or _now

    def now(self) -> datetime:
        """The store's clock. Elapsed time is arithmetic against this."""
        return self._clock()

    async def start(self, label: str = "stopwatch") -> Stopwatch:
        clean = " ".join(label.split()) or "stopwatch"
        if await self._count() >= max(1, self._config.max_stopwatches):
            raise UtilityError(
                f"there are already {self._config.max_stopwatches} stopwatches; "
                "reset one before starting another",
                {"max_stopwatches": self._config.max_stopwatches},
            )
        now = self._clock()
        watch = Stopwatch(
            id=uuid.uuid4().hex,
            label=clean,
            running=True,
            started_at=now,
            created_at=now,
            updated_at=now,
        )
        await self._db.execute(
            "INSERT INTO stopwatches (id, label, running, started_at, accumulated_seconds, "
            "laps_json, created_at, updated_at, stopped_at) "
            "VALUES (?, ?, 1, ?, 0.0, '[]', ?, ?, NULL)",
            (watch.id, watch.label, now.isoformat(), now.isoformat(), now.isoformat()),
        )
        logger.info("Started a stopwatch", extra={"stopwatch_id": watch.id})
        return watch

    async def get(self, stopwatch_id: str) -> Stopwatch | None:
        row = await self._db.fetch_one(
            "SELECT * FROM stopwatches WHERE id = ?", (stopwatch_id,)
        )
        return _row_to_stopwatch(row) if row is not None else None

    async def list_all(self) -> list[Stopwatch]:
        rows = await self._db.fetch_all(
            "SELECT * FROM stopwatches ORDER BY running DESC, created_at DESC"
        )
        return [_row_to_stopwatch(row) for row in rows]

    async def stop(self, stopwatch_id: str) -> Stopwatch:
        """Fold the running span into the accumulator. Idempotent by state."""
        watch = await self._require(stopwatch_id)
        now = self._clock()
        if not watch.running:
            return watch
        total = watch.elapsed_seconds(now)
        await self._db.execute(
            "UPDATE stopwatches SET running = 0, started_at = NULL, "
            "accumulated_seconds = ?, updated_at = ?, stopped_at = ? WHERE id = ?",
            (total, now.isoformat(), now.isoformat(), stopwatch_id),
        )
        return watch.model_copy(
            update={
                "running": False,
                "started_at": None,
                "accumulated_seconds": total,
                "updated_at": now,
                "stopped_at": now,
            }
        )

    async def resume(self, stopwatch_id: str) -> Stopwatch:
        watch = await self._require(stopwatch_id)
        if watch.running:
            return watch
        now = self._clock()
        await self._db.execute(
            "UPDATE stopwatches SET running = 1, started_at = ?, updated_at = ?, "
            "stopped_at = NULL WHERE id = ?",
            (now.isoformat(), now.isoformat(), stopwatch_id),
        )
        return watch.model_copy(
            update={"running": True, "started_at": now, "updated_at": now, "stopped_at": None}
        )

    async def lap(self, stopwatch_id: str) -> tuple[Stopwatch, float]:
        """Record the current elapsed offset. Returns the watch and the split."""
        watch = await self._require(stopwatch_id)
        now = self._clock()
        elapsed = watch.elapsed_seconds(now)
        previous = watch.laps[-1] if watch.laps else 0.0
        laps = [*watch.laps, elapsed]
        await self._db.execute(
            "UPDATE stopwatches SET laps_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(laps), now.isoformat(), stopwatch_id),
        )
        return watch.model_copy(update={"laps": laps, "updated_at": now}), elapsed - previous

    async def reset(self, stopwatch_id: str) -> bool:
        """Delete it outright. A stopwatch has no audit value once cleared."""
        if await self.get(stopwatch_id) is None:
            return False
        await self._db.execute("DELETE FROM stopwatches WHERE id = ?", (stopwatch_id,))
        return True

    async def _require(self, stopwatch_id: str) -> Stopwatch:
        watch = await self.get(stopwatch_id)
        if watch is None:
            raise UtilityError(
                f"no stopwatch with id {stopwatch_id}", {"stopwatch_id": stopwatch_id}
            )
        return watch

    async def _count(self) -> int:
        row = await self._db.fetch_one("SELECT COUNT(*) AS n FROM stopwatches")
        return int(row["n"]) if row else 0
