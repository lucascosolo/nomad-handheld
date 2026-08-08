"""The composition root: the one place that knows the whole device.

Everything below this module takes its collaborators as constructor arguments
and never reaches for a global. That is what has kept the suite runnable with
no hardware — and it is also why, until now, nothing assembled the parts and
`python -m nomad` did not exist. 443 passing tests on a machine that had never
been switched on.

Two rules this file exists to honour:

* **Dependency order is explicit and one-directional.** Config, then storage,
  then the bus, then the tool and target surface, then the drivers, then the
  session. Nothing here reaches backwards.
* **A partial start is the normal case, not an exception.** A handheld boots
  with a display unplugged, a full SD card, a database mid-migration. So every
  component goes through `ComponentRegistry`, which rolls back what already
  started, and `stop()` is safe to call at any point including before `start()`
  and after a failed one.

`api` is deliberately absent: nothing imports it, and it does not exist yet.
When it does, it is registered here and nowhere else.
"""

from __future__ import annotations

from nomad.agent.session import AgentSession
from nomad.core.config import NomadConfig
from nomad.core.events import EventBus
from nomad.core.lifecycle import Component, ComponentRegistry, ComponentState
from nomad.core.logging import get_logger
from nomad.hardware.selection import create_battery_driver, create_display_driver
from nomad.input.mapper import InputMapper
from nomad.input.stream import InputStream
from nomad.mcp.server import build_hardware_tools
from nomad.memory.store import MemoryStore
from nomad.storage.db import Database
from nomad.storage.migrations import migrate
from nomad.storage.repositories.conversations import ConversationsRepository
from nomad.storage.repositories.grants import GrantsRepository
from nomad.targets.local import LocalTarget
from nomad.targets.registry import TargetRegistry
from nomad.tools.builtin import build_default_registry
from nomad.tools.workspace import Workspace
from nomad.view.renderer import TurnRenderer
from nomad.view.server import ScreenServer

logger = get_logger(__name__)

#: Display drivers with no glass of their own, and therefore the only ones for
#: which serving the screen to a browser means anything.
_HEADLESS_DRIVERS = frozenset({"mock", "headless"})


class _Migrator:
    """Runs migrations as a lifecycle step, so a failure rolls the DB back.

    A bare `await migrate(db)` between `start_all()` calls would leave a
    started database behind when the schema is the thing that is broken.
    """

    name = "migrations"

    def __init__(self, db: Database) -> None:
        self._db = db
        self.version = 0

    async def start(self) -> None:
        self.version = await migrate(self._db)
        logger.info("Database migrated", extra={"version": self.version})

    async def stop(self) -> None:
        return None


class NomadApp:
    """Every component of the device, assembled and started in order."""

    name = "nomad"

    def __init__(self, config: NomadConfig) -> None:
        self._config = config
        self._registry = ComponentRegistry()
        self._state = ComponentState.NEW

        # Nothing here does I/O or binds anything; constructing the app must
        # be free of side effects so a failed `start()` has nothing stranded.
        self.db = Database(config.storage.path)
        self.migrator = _Migrator(self.db)
        self.bus = EventBus()
        self.workspace = Workspace(
            config.workspace.root,
            follow_symlinks_outside_root=config.workspace.follow_symlinks_outside_root,
        )
        self.targets = TargetRegistry()
        self.targets.register(LocalTarget())
        self.tools = build_default_registry(config)
        self.conversations = ConversationsRepository(self.db)
        self.grants = GrantsRepository(self.db)
        self.memory = MemoryStore(self.db, config=config.memory)

        self.display = create_display_driver(config.display)
        self.battery = create_battery_driver(config.battery)
        # The same display object the renderer draws on is the one the model's
        # `display_*` tools reach. Two screens would be worse than none.
        self.hardware_tools = build_hardware_tools(
            display=self.display,  # type: ignore[arg-type]
            battery=self.battery,  # type: ignore[arg-type]
            store=self.memory if config.memory.enabled else None,
        )

        self.renderer = TurnRenderer(self.display, bus=self.bus)  # type: ignore[arg-type]
        self.view: ScreenServer | None = self._build_view()
        self.input = InputStream(InputMapper(config.input))
        self.session = AgentSession(
            config=config,
            bus=self.bus,
            conversations=self.conversations,
            grants=self.grants,
            targets=self.targets,
            tools=self.tools,
            workspace=self.workspace,
            memory=self.memory if config.memory.enabled else None,
            hardware_tools=self.hardware_tools,
        )

        for component in self._ordered_components():
            self._registry.register(component)

    def _build_view(self) -> ScreenServer | None:
        """The browser view, for a headless display and nothing else.

        An ESP32 has its own glass; serving a second copy of it over HTTP
        would be a network service nobody asked for.
        """
        view = self._config.view
        if not view.enabled or self._config.display.driver not in _HEADLESS_DRIVERS:
            return None
        display = self.display

        def screen_html() -> str:
            # Read live, from the HTTP thread. `HeadlessDisplay` rebinds one
            # frozen `Screen` attribute per draw, so there is nothing here to
            # tear: a reader sees the previous screen or the next one.
            return str(display.screen.html)  # type: ignore[attr-defined]

        return ScreenServer(
            screen_html,
            host=view.host,
            port=view.port,
            refresh_seconds=view.refresh_seconds,
            width=self._config.display.width,
            height=self._config.display.height,
        )

    def _ordered_components(self) -> list[Component]:
        """Start order. Stop order is exactly its reverse (`ComponentRegistry`)."""
        components: list[Component] = [self.db, self.migrator, self.bus]
        if self.view is not None:
            components.append(self.view)
        # The renderer subscribes, so it must be up before the session can
        # publish a turn — and it must still be up while the session stops.
        components.extend([self.renderer, self.input, self.session])
        return components

    # -- lifecycle ---------------------------------------------------------

    @property
    def state(self) -> ComponentState:
        return self._state

    @property
    def view_url(self) -> str | None:
        return self.view.url if self.view is not None else None

    def states(self) -> dict[str, ComponentState]:
        return self._registry.states()

    async def start(self) -> None:
        """Start every component in order, or roll back and raise."""
        self._state = ComponentState.STARTING
        self.workspace.ensure_exists()
        try:
            await self._registry.start_all()
        except Exception:
            self._state = ComponentState.FAILED
            raise
        self._state = ComponentState.STARTED
        logger.info(
            "Nomad started",
            extra={
                "backend": self._config.agent.backend,
                "mode": str(self.session.mode),
                "display": self._config.display.driver,
                "view_url": self.view_url or "disabled",
            },
        )

    async def stop(self) -> None:
        """Stop whatever is running. Safe after a partial or failed start.

        `ComponentRegistry` only stops what it actually started and swallows
        stop failures, so this is a no-op on a fresh app and a partial teardown
        on a half-started one — neither of which raises.
        """
        self._state = ComponentState.STOPPING
        await self._registry.stop_all()
        self._state = ComponentState.STOPPED
        logger.info("Nomad stopped")
