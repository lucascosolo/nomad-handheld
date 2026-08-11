"""One Nomad per device, and therefore one Claude session per device (D45).

Nothing stopped a second `python -m nomad` from starting beside the first. Both
would open the same SQLite file, both would draw to the same panel, and — the
expensive one — **both would connect their own Claude Code session**, against
the same subscription, each with its own permission broker and its own idea of
what the operator had approved.

That was survivable while every turn began with a person typing. It stops being
survivable the moment the device starts turns on a schedule: a second instance
is a second unattended loop nobody knows is running, spending tokens and opening
worktrees on the same tree as the first.

The lock is an exclusive `flock` on a file in the data directory, held for the
life of the process and released by the kernel when it exits — which is the
property a PID file does not have. A crashed instance leaves a stale PID file
and a lock nobody holds; the next start succeeds, correctly, with no cleanup
step and no "is that PID still alive" heuristic to get wrong.

**What it does not cover, stated plainly:** an operator who SSHes in and runs
`claude` by hand is a second CLI session, and nothing here can prevent that. It
is also not the failure this guards — that session is a person's own hands on
the device. This bounds what *Nomad* starts on its own.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from nomad.core.errors import LifecycleError
from nomad.core.logging import get_logger

logger = get_logger(__name__)

#: Lives beside the database, because they are locked together in the
#: literal sense: the reason to refuse a second instance is that it would open
#: this one's storage and this one's backend session.
LOCK_FILENAME = "nomad.lock"


class InstanceLock:
    """Refuses to start when another Nomad already holds this data directory."""

    name = "instance_lock"

    def __init__(self, data_dir: str | Path, *, filename: str = LOCK_FILENAME) -> None:
        self._path = Path(data_dir).expanduser() / filename
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._fd is not None

    async def start(self) -> None:
        """Take the lock, or refuse the boot.

        Refusing is the whole point, so this raises rather than logging and
        carrying on: a second instance that starts anyway with a warning in a
        log nobody is reading is the same two sessions, minus the error
        message.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = _read_pid(fd)
            os.close(fd)
            raise LifecycleError(
                "Another Nomad is already running on this data directory",
                {"lock": str(self._path), "holder_pid": holder, "error": str(exc)},
            ) from exc
        # Truncate then write: the previous holder's PID is not ours, and a
        # shorter PID would otherwise leave its tail behind.
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        self._fd = fd
        logger.info("Instance lock held", extra={"lock": str(self._path), "pid": os.getpid()})

    async def stop(self) -> None:
        """Release it. Safe before `start()` and safe twice."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_pid(fd: int) -> int | None:
    """Whose lock it is, for the error message. Never load-bearing."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return int(os.read(fd, 32).decode().strip() or 0) or None
    except (OSError, ValueError):  # pragma: no cover - diagnostics only
        return None
