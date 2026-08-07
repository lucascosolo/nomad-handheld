from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from nomad.core.config import NomadConfig, load_config
from nomad.core.events import EventBus
from nomad.storage.db import Database
from nomad.storage.migrations import migrate

MINIMAL_TOML = """
[core]
name = "nomad-test"
data_dir = "var"

[storage]
path = "var/nomad.db"
"""


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / "var"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


@pytest.fixture
def tmp_config_path(tmp_path: Path) -> Path:
    config_path = tmp_path / "nomad.toml"
    config_path.write_text(MINIMAL_TOML)
    return config_path


@pytest.fixture
def config(tmp_config_path: Path) -> NomadConfig:
    return load_config(tmp_config_path, env={})


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "nomad.db")
    await database.start()
    await migrate(database)
    try:
        yield database
    finally:
        await database.stop()


@pytest.fixture
async def event_bus() -> AsyncIterator[EventBus]:
    bus = EventBus()
    await bus.start()
    try:
        yield bus
    finally:
        await bus.stop()
