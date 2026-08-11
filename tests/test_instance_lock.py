"""One Nomad per data directory (D45).

Nothing stopped a second `python -m nomad` from starting beside the first: same
database, same panel, and — the expensive one — a second Claude Code session
against the same subscription. Harmless enough while every turn began with a
person typing; not harmless at all once the device starts turns on a schedule,
because the second unattended loop is one nobody knows is running.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nomad.app import NomadApp
from nomad.core.errors import LifecycleError
from nomad.core.instance import InstanceLock
from nomad.core.lifecycle import ComponentState


async def test_the_lock_is_taken_and_released(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path)
    await lock.start()
    try:
        assert lock.held
        assert lock.path.exists()
        assert lock.path.read_text().strip() == str(os.getpid())
    finally:
        await lock.stop()
    assert not lock.held


async def test_a_second_lock_on_the_same_directory_is_refused(tmp_path: Path) -> None:
    first = InstanceLock(tmp_path)
    await first.start()
    try:
        with pytest.raises(LifecycleError) as caught:
            await InstanceLock(tmp_path).start()
        # The error has to say who is holding it, or the operator's next move
        # is guesswork on a device with no keyboard.
        assert caught.value.details["holder_pid"] == os.getpid()
    finally:
        await first.stop()


async def test_releasing_it_lets_the_next_instance_start(tmp_path: Path) -> None:
    first = InstanceLock(tmp_path)
    await first.start()
    await first.stop()
    second = InstanceLock(tmp_path)
    await second.start()
    try:
        assert second.held
    finally:
        await second.stop()


async def test_separate_data_directories_do_not_collide(tmp_path: Path) -> None:
    """The lock is on the data directory, not on the machine: a test run and a
    device on the same Pi are two Nomads and that is fine."""
    a, b = InstanceLock(tmp_path / "a"), InstanceLock(tmp_path / "b")
    await a.start()
    await b.start()
    try:
        assert a.held and b.held
    finally:
        await a.stop()
        await b.stop()


async def test_stop_is_safe_before_start_and_twice(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path)
    await lock.stop()
    await lock.start()
    await lock.stop()
    await lock.stop()


def _config(tmp_path: Path) -> object:
    from nomad.core.config import NomadConfig

    return NomadConfig.model_validate(
        {
            "storage": {"path": str(tmp_path / "nomad.db")},
            "workspace": {"root": str(tmp_path / "workspace")},
            "view": {"enabled": True, "port": 0, "remote": False},
            "core": {"data_dir": str(tmp_path / "var")},
        }
    )


async def test_a_second_app_on_the_same_device_refuses_to_boot(tmp_path: Path) -> None:
    """The join. A refused boot must also leave nothing running behind it —
    `ComponentRegistry` rolls back, and the lock is first so there is nothing
    to roll back from."""
    first = NomadApp(_config(tmp_path))  # type: ignore[arg-type]
    await first.start()
    second = NomadApp(_config(tmp_path))  # type: ignore[arg-type]
    try:
        with pytest.raises(LifecycleError):
            await second.start()
        assert second.states()["agent_session"] is ComponentState.NEW
        assert second.state is ComponentState.FAILED
        # The first is untouched by the second's failure.
        assert first.states()["agent_session"] is ComponentState.STARTED
    finally:
        await second.stop()
        await first.stop()


async def test_the_lock_is_the_first_component_and_so_the_last_released(
    tmp_path: Path,
) -> None:
    app = NomadApp(_config(tmp_path))  # type: ignore[arg-type]
    order = [c.name for c in app._ordered_components()]
    assert order[0] == "instance_lock"
