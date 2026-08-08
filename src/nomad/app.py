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

**Chunk F3 — what this file was for.** Six fully-tested subsystems (voice,
resources, notifications, utilities, skills, offline) shipped green and inert,
because a composition root is the one place they can be made real and this one
constructed almost none of them. A tested module nothing constructs is not a
feature, and two of the gaps were worse than dormant:

* `AgentSession.send()` had exactly one caller in `src/`, inside crash
  recovery. A stranger holding the device could not begin a conversation by
  any means. `submit()` below is now the way, and the browser page is the
  thing that calls it.
* With a headless display the prompter was `NullChoicePrompter`, which answers
  `NO_OPERATOR` to everything. On the shipped `mode = "manual"` that made every
  gated tool call denied permanently, by construction, on the default config —
  a safety guarantee whose first-boot behaviour is indistinguishable from a
  broken device, which is exactly the shape D35 named.
"""

from __future__ import annotations

from pathlib import Path

from nomad.agent.session import AgentSession
from nomad.audio.selection import create_speaker_driver, create_synthesizer_driver
from nomad.core.config import NomadConfig
from nomad.core.events import EventBus
from nomad.core.lifecycle import Component, ComponentRegistry, ComponentState
from nomad.core.logging import get_logger
from nomad.hardware.selection import create_battery_driver, create_display_driver
from nomad.input.choice import (
    ExternalChoicePrompter,
    InputChoicePrompter,
    NullChoicePrompter,
    PendingQuestion,
)
from nomad.input.mapper import InputMapper
from nomad.input.stream import InputStream
from nomad.mcp.offline import build_offline_tools
from nomad.mcp.server import build_hardware_tools
from nomad.mcp.skills import build_skill_tools
from nomad.mcp.voice import build_voice_tools
from nomad.memory.store import MemoryStore
from nomad.notifications.queue import NotificationQueue
from nomad.offline import IntentLedger, OfflineResponder, PromotionAnalyst, default_router
from nomad.resources.governor import ResourceGovernor
from nomad.resources.workload import InteractiveWorkload
from nomad.skills.library import SkillLibrary
from nomad.storage.db import Database
from nomad.storage.migrations import migrate
from nomad.storage.repositories.conversations import ConversationsRepository
from nomad.storage.repositories.grants import GrantsRepository
from nomad.targets.local import LocalTarget
from nomad.targets.registry import TargetRegistry
from nomad.tools.base import Tool
from nomad.tools.builtin import build_default_registry
from nomad.tools.workspace import Workspace
from nomad.utilities.notes import NoteStore
from nomad.utilities.stopwatch import StopwatchStore
from nomad.view.authprompt import (
    AUTH_PROMPT_WRITER,
    AuthorizationPrompter,
    ChoicePrompter,
    make_show,
)
from nomad.view.renderer import TurnRenderer
from nomad.view.screen import ScreenOwner
from nomad.view.server import DISMISS_INDEX, AnswerChoice, PendingSource, ScreenServer

logger = get_logger(__name__)

#: Display drivers with no glass of their own, and therefore the only ones for
#: which serving the screen to a browser means anything.
_HEADLESS_DRIVERS = frozenset({"mock", "headless"})

#: The screen handle the onboard tier draws under. A fourth writer, arbitrated
#: by the same `ScreenOwner` as the other three (D36) — an offline answer that
#: went nowhere the operator could see would be indistinguishable from the
#: device ignoring them.
OFFLINE_WRITER = "offline"

#: What an onboard answer is titled. Deliberately not the same chrome a turn
#: gets: a device that quietly answers some questions itself and others through
#: the model, with no way to tell which happened, is one you stop trusting
#: (chunk O, constraint 1).
OFFLINE_TITLE = "Onboard"

#: Named workloads registered as `INTERACTIVE` (D38). Registration is
#: declarative — the tier has no `run` and no handle — so this records what may
#: never be suspended, and nothing more. Speech-to-text is the reason the tier
#: exists: it is the one heavy local computation that a turn is *waiting on*.
_INTERACTIVE_WORKLOADS = ("notifications", "speech_to_text")


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

        # Durable stores behind the daily utilities. Each one is `None` when
        # its config section is off, and every builder below treats `None` as
        # "that capability does not exist" rather than "that capability is
        # broken" — a device wired without a timer store looks like a device
        # with no timers, which is the rule `build_hardware_tools` already
        # applies to memory.
        self.notifications = (
            NotificationQueue(self.db, config=config.notifications)
            if config.notifications.enabled
            else None
        )
        self.notes = (
            NoteStore(self.db, config=config.utilities) if config.utilities.enabled else None
        )
        self.stopwatches = (
            StopwatchStore(self.db, config=config.utilities) if config.utilities.enabled else None
        )

        # The one exception to "nothing here does I/O": the skill library is
        # read from disk during construction, because `build_skill_tools`
        # returns `[]` for an empty library and the toolset is fixed before
        # `start()` runs. Reading a directory of markdown binds nothing and
        # starts nothing, so a failed start still has nothing stranded — the
        # rule this bends is about *side effects*, and there are none.
        self.skills = self._build_skills()

        self.display = create_display_driver(config.display)
        self.battery = create_battery_driver(config.battery)
        # Output only, and that is not an omission. D37 gives the device a
        # `speak` tool and deliberately no `listen` tool at any price, so the
        # synthesizer and the speaker have a consumer and the recorder and the
        # transcriber do not: capture belongs to a push-to-talk handler, and
        # push-to-talk needs an input device that is feeding `InputStream`.
        # Constructing two drivers nothing can reach would be the same
        # inertness this chunk exists to remove, one layer down.
        self.speaker = create_speaker_driver(config.audio)
        self.synthesizer = create_synthesizer_driver(config.audio)
        # The same display object the renderer draws on is the one the model's
        # `display_*` tools reach. Two screens would be worse than none — but
        # one screen with two unarbitrated writers is what let streaming turn
        # output paint over a live authorization prompt, so both of them now
        # go through the one `ScreenOwner` (D36).
        self.screen = ScreenOwner(self.display)  # type: ignore[arg-type]

        # Built before anything that consumes it, because three things need
        # the *same* prompter: the authorization prompt (D36), the model's
        # `display_choice` tool, and — when the screen is a browser — the HTTP
        # answer endpoint. Two prompters would be two operators.
        self.input = InputStream(InputMapper(config.input))
        self.prompter = self._build_prompter()

        # The onboard tier (chunk O). The router is a fixed phrase set; the
        # ledger counts what missed. Both exist before the session, because the
        # tools that expose them are part of the toolset the session registers.
        self.intent_router = default_router() if config.offline.enabled else None
        self.intent_ledger = (
            IntentLedger(self.db, config=config.offline) if config.offline.enabled else None
        )

        self.hardware_tools = build_hardware_tools(
            display=self.screen.view("model"),  # type: ignore[arg-type]
            battery=self.battery,  # type: ignore[arg-type]
            # **No `prompter=`, deliberately.** `display_choice` would work if
            # it shared this one, and that is the problem: the model would then
            # author questions that arrive on the operator's screen through the
            # same widget, and are answered by the same buttons, as an
            # authorization prompt. D36 keeps one authorization UI so the
            # operator can trust the frame; handing the model a second question
            # channel into it is the spoof that rule exists to prevent.
            # `test_the_authorization_prompt_is_not_reachable_as_a_tool` pins it.
            store=self.memory if config.memory.enabled else None,
            notifications=self.notifications,
            notes=self.notes,
            stopwatches=self.stopwatches,
        )
        # One list, assembled here and nowhere else. `AgentSession` registers
        # whatever it is handed, so a builder that returned `[]` for missing
        # infrastructure simply contributes nothing — the absent-capability
        # rule survives assembly untouched.
        self.agent_tools: list[Tool] = [
            *self.hardware_tools,
            *build_voice_tools(
                synthesizer=self.synthesizer,  # type: ignore[arg-type]
                speaker=self.speaker,  # type: ignore[arg-type]
            ),
            *build_skill_tools(self.skills),
            *build_offline_tools(self.intent_router, ledger=self.intent_ledger),
        ]

        self.renderer = TurnRenderer(self.screen.view("renderer"), bus=self.bus)  # type: ignore[arg-type]
        self.view: ScreenServer | None = self._build_view()
        self.session = AgentSession(
            config=config,
            bus=self.bus,
            conversations=self.conversations,
            grants=self.grants,
            targets=self.targets,
            tools=self.tools,
            workspace=self.workspace,
            memory=self.memory if config.memory.enabled else None,
            hardware_tools=self.agent_tools,
        )
        # Built after the session and from *its* broker and executor, which is
        # the load-bearing half of chunk O: an onboard answer is a `ToolRequest`
        # through the same four-stage pipeline as anything the model asks for.
        # "Onboard" says where a call runs, never whether it was authorized —
        # a second execution path reachable only when nobody is watching would
        # undo the whole security layer at exactly the wrong moment.
        self.offline = (
            OfflineResponder(
                router=self.intent_router,
                broker=self.session.broker,
                executor=self.session.executor,
                ledger=self.intent_ledger,
            )
            if self.intent_router is not None
            else None
        )
        self.governor = self._build_governor()
        # Built last because it resolves through the session, which is what
        # supplies the permission mode the queue's `approve()` needs. Held
        # here and nowhere else: this handle must never reach `ToolRegistry`,
        # or `can_use_tool` would gate the question it is being asked (D36).
        self.authprompt = AuthorizationPrompter(
            bus=self.bus,
            screen=self.screen,
            prompter=self.prompter,
            resolver=self.session,
        )

        for component in self._ordered_components():
            self._registry.register(component)

    def _build_skills(self) -> SkillLibrary | None:
        """Load the skill library, or say there is none (D39)."""
        skills = self._config.skills
        if not skills.enabled:
            return None
        library = SkillLibrary(index_budget_chars=skills.index_budget_chars)
        skipped = library.load_directory(Path(skills.root))
        logger.info(
            "Skill library loaded",
            extra={"root": skills.root, "skills": len(library.names()), "skipped": len(skipped)},
        )
        return library

    def skill_index(self) -> str:
        """The block a prompt injects (D39). Empty when there is no library.

        Exposed rather than injected: the identity Claude Code receives is
        assembled inside `agent/`, and nothing there takes a hook today. This
        is the composition root holding the index where the injection will
        reach for it, which is the half of the wiring that belongs to F3.
        """
        return self.skills.render_index() if self.skills is not None else ""

    def _build_governor(self) -> ResourceGovernor | None:
        """D38's two tiers, populated. The governor learns a turn is live by
        watching the bus for two event names, so handing it `self.bus` *is* the
        connection to turn start and finish — there is no second wire to run.
        """
        if not self._config.resources.enabled:
            return None
        governor = ResourceGovernor(self.bus, config=self._config.resources)
        for name in _INTERACTIVE_WORKLOADS:
            governor.register(InteractiveWorkload(name))
        if self.intent_ledger is not None:
            # The only opportunistic workload the device has: it drafts skills
            # from repeated misses and parks the instant a turn starts.
            governor.register(PromotionAnalyst(self.intent_ledger, config=self._config.offline))
        return governor

    def _build_view(self) -> ScreenServer | None:
        """The browser view, for a headless display and nothing else.

        An ESP32 has its own glass; serving a second copy of it over HTTP
        would be a network service nobody asked for.

        When the display *is* headless the page is also the only input device
        the machine has, so it is handed two write callables. It gets callables
        and not objects deliberately: `view` may not import `agent`, and the
        rule that has kept this tree layered is that the composition root does
        the introducing.
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

        pending: PendingSource | None = None
        answer: AnswerChoice | None = None
        if isinstance(self.prompter, ExternalChoicePrompter):
            browser = self.prompter

            def pending() -> PendingQuestion | None:
                # An immutable snapshot, read from the HTTP thread.
                return browser.pending

            async def answer(token: str, index: int) -> bool:
                if index == DISMISS_INDEX:
                    return await browser.cancel(token)
                return await browser.answer(token, index)

        return ScreenServer(
            screen_html,
            host=view.host,
            port=view.port,
            refresh_seconds=view.refresh_seconds,
            width=self._config.display.width,
            height=self._config.display.height,
            submit_text=self.submit,
            pending_choice=pending,
            answer_choice=answer,
        )

    async def submit(self, text: str) -> object:
        """The one way a turn begins from outside the session (chunk F3).

        Onboard first, then the model. That order is chunk O's whole point and
        it is safe because the router matches by *equality* — an utterance it
        does not recognise exactly falls through to `AgentSession.send()`, so
        the failure mode is a round trip rather than a wrong answer. What the
        router does answer, it answers through the broker and the executor the
        session itself uses, and it says so on the screen under its own title:
        the operator can always tell which of the two answered them.
        """
        if self.offline is not None:
            outcome = await self.offline.handle(
                text, session_id=self.session.session_id, mode=self.session.mode
            )
            if outcome.handled:
                await self.screen.view(OFFLINE_WRITER).show_text(
                    outcome.content or outcome.reason, title=OFFLINE_TITLE
                )
                logger.info("Answered onboard", extra={"intent": outcome.intent})
                return outcome
        return await self.session.send(text)

    def _build_prompter(self) -> ChoicePrompter:
        """Who answers a question on the glass — and the single owner of the
        input stream.

        `InputStream.events()` is a single-consumer generator: two readers
        steal each other's presses, and a menu that misses every other press
        is worse than one that misses all of them. So exactly one
        `InputChoicePrompter` is ever constructed, here, and nothing else in
        the tree is given `self.input`.

        Which prompter is decided by the display driver, because on this
        device they are the same fact: the joystick and the buttons are on the
        ESP32 that is also the screen (D13, ARCHITECTURE). A headless screen
        means nothing is feeding `self.input`.

        Headless plus a browser view is the case chunk F3 added, and it is the
        one that was silently fatal before: `NullChoicePrompter` answers
        `NO_OPERATOR` to everything, so with `mode = "manual"` — what
        `nomad.toml` ships — *every* gated tool call was denied permanently, by
        construction, on the default configuration. `ExternalChoicePrompter`
        is the fix and it deliberately does **not** feed `self.input`: it is
        answered directly over HTTP, so `InputStream.events()` still has zero
        readers in a headless build and the single-consumer rule holds without
        anyone having to remember it.

        With no browser either, `NullChoicePrompter` remains the honest
        answer — it still *draws* the question, and reports `NO_OPERATOR`
        rather than a timeout, because retrying can never help (D32).
        """
        show = make_show(self.screen.view(AUTH_PROMPT_WRITER))
        if self._config.display.driver in _HEADLESS_DRIVERS:
            if self._config.view.enabled:
                return ExternalChoicePrompter(show=show)
            return NullChoicePrompter(show=show)
        return InputChoicePrompter(self.input, show=show)

    def _ordered_components(self) -> list[Component]:
        """Start order. Stop order is exactly its reverse (`ComponentRegistry`)."""
        components: list[Component] = [self.db, self.migrator, self.bus]
        if self.view is not None:
            components.append(self.view)
        # The renderer and the authorization prompt subscribe, so both must be
        # up before the session can publish a turn — and both must still be up
        # while the session stops.
        components.extend([self.renderer, self.authprompt, self.input])
        if self.governor is not None:
            # After the database (its one opportunistic workload reads the
            # ledger) and before the session, so it is already subscribed when
            # the first turn starts. Stopping in reverse means background work
            # is cancelled *after* the session has closed its turns, which is
            # the order that leaves nothing half-written.
            components.append(self.governor)
        components.append(self.session)
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
                "tools": len(self.agent_tools),
                "skills": len(self.skills.names()) if self.skills is not None else 0,
                "prompter": type(self.prompter).__name__,
                "offline": self.offline is not None,
                "workloads": len(self.governor.inventory()) if self.governor is not None else 0,
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
