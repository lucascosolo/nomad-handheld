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

import asyncio
import contextlib
import os
import secrets
from pathlib import Path

from nomad.agent.session import AgentSession
from nomad.audio.selection import create_speaker_driver, create_synthesizer_driver
from nomad.core.config import NomadConfig, PermissionMode
from nomad.core.events import EventBus
from nomad.core.instance import InstanceLock
from nomad.core.lifecycle import Component, ComponentRegistry, ComponentState
from nomad.core.logging import get_logger
from nomad.hardware.fanout import DisplayFanout
from nomad.hardware.panel_keeper import PANEL_SURFACE, PanelKeeper
from nomad.hardware.selection import create_battery_driver, create_display_stack
from nomad.input.broker import InputBroker
from nomad.input.choice import (
    ExternalChoicePrompter,
    InputChoicePrompter,
    NullChoicePrompter,
    PendingQuestion,
)
from nomad.input.mapper import InputMapper
from nomad.input.router import InputRouter
from nomad.input.stream import InputStream
from nomad.input.wake import WAKE_LABEL, WakeAffordance
from nomad.mcp.offline import build_offline_tools
from nomad.mcp.server import build_hardware_tools
from nomad.mcp.skills import build_skill_tools
from nomad.mcp.voice import build_voice_tools
from nomad.memory.store import MemoryStore
from nomad.notifications.delivery import NotificationDelivery, ScreenNotificationSink
from nomad.notifications.queue import NotificationQueue
from nomad.offline import IntentLedger, OfflineResponder, PromotionAnalyst, default_router
from nomad.protocol import Link, LinkKind, create_transport
from nomad.resources.governor import ResourceGovernor
from nomad.resources.workload import InteractiveWorkload
from nomad.skills.library import SkillLibrary, default_seed_root
from nomad.status import STATUS_WRITER, collect_status, probe_backend, status_rows
from nomad.storage.db import Database
from nomad.storage.migrations import migrate
from nomad.storage.repositories.conversations import ConversationsRepository
from nomad.storage.repositories.grants import GrantsRepository
from nomad.targets.local import LocalTarget
from nomad.targets.registry import TargetRegistry
from nomad.tools.base import Tool
from nomad.tools.builtin import build_default_registry
from nomad.tools.workspace import Workspace
from nomad.triggers import SelfImproveTrigger
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
from nomad.view.server import (
    DISMISS_INDEX,
    AnswerChoice,
    PendingSource,
    ScreenServer,
    is_loopback,
)

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

#: How long after boot the status card is drawn a second time, when a panel is
#: attached. Opening the serial port toggles DTR/RTS, which on an ESP32-S3 is
#: the reset line — so the board restarts *because* Nomad connected to it, and
#: comes back up after the first card has already gone out. Every layer reports
#: success and the glass sits on "waiting for the Pi".
#:
#: A redraw on D30's reboot detection is the principled fix and it is wired
#: too, but it cannot cover this case: the panel's `seq` restarts from zero
#: while Nomad's view of it is *also* zero, so there is no backwards step to
#: detect on a first connect. One deliberate second draw is what makes the
#: screen right on a cold boot, which is the only boot an operator sees.
PANEL_SETTLE_SECONDS = 4.0

#: Where the view's shared secret is kept, under `[core].data_dir`. A file and
#: not a config key, because a secret in config is a secret in git.
VIEW_TOKEN_FILE = "view-token"

#: Named workloads registered as `INTERACTIVE` (D38). Registration is
#: declarative — the tier has no `run` and no handle — so this records what may
#: never be suspended, and nothing more. Speech-to-text is the reason the tier
#: exists: it is the one heavy local computation that a turn is *waiting on*.
#:
#: `notifications` used to be a third string here and nothing else — a workload
#: declared for a delivery loop that did not exist, which is how "timers never
#: fire" hid behind a device that looked correctly wired. It is now registered
#: from `self.delivery`, so the declaration cannot outlive the thing it names.
_INTERACTIVE_WORKLOADS = ("speech_to_text",)


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
        #: The deferred panel redraw, held so `stop()` can cancel it.
        self._panel_redraw: asyncio.Task[None] | None = None

        # Nothing here does I/O or binds anything; constructing the app must
        # be free of side effects so a failed `start()` has nothing stranded.
        # One Nomad per data directory, and so one Claude session per device
        # (D45). Constructed first and started first; it binds nothing until
        # `start()`, so a refused boot has opened no database and connected no
        # backend.
        self.instance_lock = InstanceLock(config.core.data_dir)
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

        # The link to the ESP32-S3, built only when a surface actually needs
        # one. `create_display_stack` raises without it, and a Link on a mock
        # transport that nothing talks to would be a reader task burning a
        # wakeup forever on a laptop build.
        self.esp32_link = self._build_esp32_link()
        self.display = create_display_stack(config.display, link=self.esp32_link)
        self.panel_keeper = self._build_panel_keeper()
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
        # What carries a press from the cable to the stream. `feed_*` had no
        # caller outside its own package until now, so a device with a real
        # panel could draw a question and never receive the answer. Built only
        # when there is a link to read: with no panel there is nothing feeding
        # input, and a router over a link that does not exist would be a task
        # waiting forever on a laptop build.
        self.input_router = (
            InputRouter(self.esp32_link, self.input) if self.esp32_link is not None else None
        )
        # The one reader of that stream, permanently (D44). It is not a feature
        # and so it is not conditional: whoever holds the stream decides who
        # sees a press, and "sometimes nobody is reading" is what let an idle
        # tap queue up and be delivered to the next authorization prompt as
        # though it answered it. Its idle handler is wired further down, once
        # the thing a tap should reach exists.
        self.input_broker = InputBroker(self.input)
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

        self.renderer = TurnRenderer(
            self.screen.view("renderer"),  # type: ignore[arg-type]
            bus=self.bus,
            wake_label=self._wake_label(),
        )
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
            # D39's always-in-context half. Without this the library is a
            # directory the model is never told about and `load_skill` a tool
            # it has no reason to call — progressive disclosure with nothing
            # disclosed.
            skill_index=self.skill_index(),
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
        # The consumer chunk N was written for and never got. Without it
        # `deliver_due()` had no caller in `src/` at all: every timer, alarm
        # and reminder was stored, confirmed to the operator, and then never
        # shown. It draws through the same `ScreenOwner` as the other writers
        # and declines rather than painting over an authorization prompt
        # (D36) — see `notifications/delivery.py` for why that is the right
        # arbitration for this writer specifically.
        #
        # `announce=` is left unset deliberately. D37 gives the device a
        # `speak` tool and speaking a fired timer is desirable, but the
        # configured audio path resolves to a stub with nothing in the jack —
        # so this is the seam it plugs into, not a call that would raise on
        # every delivery.
        self.delivery = (
            NotificationDelivery(self.notifications, ScreenNotificationSink(self.screen))
            if self.notifications is not None
            else None
        )
        # The first thing on this device that can begin a turn without a person
        # (chunk P). Built after the session because it drives it, and started
        # after it for the same reason — see `_ordered_components`.
        self.self_improve = self._build_self_improve()
        # The other end of the button the renderer draws (D44). Built here
        # because this is the first point at which both halves exist: the
        # broker needed the stream, and the thing a tap starts needed the
        # session. `self.wake` stays `None` when there is nothing to fire —
        # and `_wake_label()` returns `None` under the same condition, so the
        # device never draws a control with nothing behind it.
        self.wake = (
            WakeAffordance(self.self_improve.fire_now)
            if self.self_improve is not None and self._wake_label() is not None
            else None
        )
        if self.wake is not None:
            self.input_broker.set_idle_handler(self.wake)
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
        """Load the skill library from both its roots, or say there is none (D39).

        **Seeds first, authored second, and the order is the policy.**
        `SkillLibrary.add` keys by name, so the second load wins: a skill the
        operator authored under `var/skills` shadows a shipped one of the same
        name. That is what lets somebody disagree with a seed without editing
        Nomad's own source tree — the thing D22 makes deliberately expensive.

        Committing the seeds is what stops this whole subsystem being inert.
        `var/` is gitignored, so a fresh checkout has no `var/skills` at all;
        a library of zero renders no index and `build_skill_tools` returns
        `[]`, which means progressive disclosure over a directory nothing can
        reach. See `nomad.skills.default_seed_root` for why they live beside
        `NOMAD.md` rather than under `var/`.

        Neither root existing is not an error — that is a device with no
        skills, which is a real state and must boot.
        """
        skills = self._config.skills
        if not skills.enabled:
            return None
        library = SkillLibrary(index_budget_chars=skills.index_budget_chars)
        seed_root = Path(skills.seed_root) if skills.seed_root is not None else default_seed_root()
        seed_skipped = library.load_directory(seed_root) if skills.seed_root != "" else []
        seeded = len(library.names())
        skipped = library.load_directory(Path(skills.root))
        logger.info(
            "Skill library loaded",
            extra={
                "seed_root": str(seed_root),
                "seeded": seeded,
                "root": skills.root,
                "skills": len(library.names()),
                "skipped": len(seed_skipped) + len(skipped),
            },
        )
        return library

    def skill_index(self) -> str:
        """The block injected into every prompt (D39). Empty with no library.

        Rendered here and handed to `AgentSession` as a `str`, because
        `tests/test_layering.py` does not let `agent` import `nomad.skills` —
        the session can be told what the skills are and can never ask. That is
        the same one-way shape the permission path has: a skill body reaches
        the model's context and nothing else, so it cannot become an input to
        an authorization decision (D39).
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
        if self.delivery is not None:
            # D38 names the notification queue as interactive by name. It is
            # registered from the component so that the tier describes
            # something that is actually running.
            governor.register(InteractiveWorkload(self.delivery.name))
        if self.intent_ledger is not None:
            # The only opportunistic workload the device has: it drafts skills
            # from repeated misses and parks the instant a turn starts.
            governor.register(PromotionAnalyst(self.intent_ledger, config=self._config.offline))
        return governor

    def _build_esp32_link(self) -> Link | None:
        """A `Link` to the ESP32-S3, or `None` when no surface needs one.

        Built here rather than inside `create_display_stack` because a link is
        a *component* — it owns a reader task and has to be started and stopped
        in order with everything else — and the composition root is the only
        thing that may know that (D9's selection functions return drivers, not
        lifecycles).
        """
        if "esp32" not in self._config.display.surfaces:
            return None
        transport = create_transport(self._config.transports.esp32, name="esp32")
        link = Link(transport, kind=LinkKind.DISPLAY, name="esp32")
        link.on_reboot(self._redraw_after_reboot)
        return link

    def _build_panel_keeper(self) -> PanelKeeper | None:
        """Repaint the panel on a tick, when there is a panel to repaint.

        `_redraw_after_reboot` below covers the reset it can *see* — `seq`
        going backwards. It cannot see the first one: on a fresh connect both
        sides are at zero, so the reset that opening the port causes is
        invisible to reboot detection, and the fixed settle-delay redraw that
        was written for it is a guess about timing that was wrong on the glass.
        A tick needs no guess and covers the knocked cable and the brownout
        too, which reboot detection also misses when the Pi's write lands in
        the window before the panel is listening again.

        Requires a `DisplayFanout`, because `repaint` is the fanout's replay of
        its own last write and there is nothing else that remembers what was
        drawn. With `mirror = []` the stack is the bare driver, so a
        single-surface panel gets no keeper — worth knowing before wondering
        why the tick is absent.
        """
        if self._config.display.repaint_interval_s <= 0:
            return None
        if not isinstance(self.display, DisplayFanout):
            return None
        if self.display.surface(PANEL_SURFACE) is None:
            return None
        return PanelKeeper(
            self.display,
            interval_s=self._config.display.repaint_interval_s,
        )

    def _build_self_improve(self) -> SelfImproveTrigger | None:
        """Nomad's own scheduled turn, when the operator has asked for one.

        `None` unless `[triggers.self_improve].enabled` — off is the shipped
        answer, because this is the one component whose *existence* spends
        money. There is deliberately no "build it and let it idle": a trigger
        constructed but never firing is indistinguishable, from the outside,
        from the six inert subsystems this file was written to stop happening.

        The readiness probe is passed as a callable rather than reached for.
        `nomad.status` is a composition-root module and `triggers` is a package
        below one, so the import would point the wrong way — and the whole
        point is to use the probe that already exists (a CLI that answers
        `--version` and a credential that has not expired) rather than to write
        a second one that will disagree with `nomad status` about whether this
        device can think.
        """
        trigger = self._config.triggers.self_improve
        if not trigger.enabled:
            return None
        return SelfImproveTrigger(
            self.session,
            interval_s=trigger.interval_seconds,
            first_tick_s=trigger.first_tick_seconds,
            readiness=self._backend_ready,
            max_consecutive_failures=trigger.max_consecutive_failures,
        )

    def _wake_label(self) -> str | None:
        """The label of the idle screen's one option, or `None` for no button.

        Two conditions, and both are about honesty rather than taste (D44). A
        wake button with `[triggers.self_improve]` disabled would draw a
        control that cannot start anything, because the prompt and the
        provenance it fires with are that trigger's. A wake button with no
        panel would draw one nothing can report a tap on — a headless build
        answers over HTTP and never feeds `InputStream` at all.

        Read twice, from config both times, rather than cached: the renderer
        needs it before the session exists and the affordance needs it after.
        """
        if not self._config.triggers.self_improve.enabled:
            return None
        if self._config.display.driver in _HEADLESS_DRIVERS:
            return None
        return WAKE_LABEL

    async def _backend_ready(self) -> bool:
        """Could a turn actually run right now? The same probe `nomad status` uses."""
        health = await probe_backend(self._config)
        return health.ready

    async def _redraw_after_reboot(self, event: object) -> None:
        """Put something back on the glass after the panel restarts.

        D30 detects a reboot by watching `seq` go backwards, and until now
        nothing acted on that for the display. It has to, and the reason is not
        hypothetical: **opening the serial port resets the board.** Asserting
        DTR/RTS is how esptool reboots an ESP32, and pyserial does it on open —
        so the very first thing Nomad does to the panel is restart it. The boot
        card was drawn once, immediately, and the panel came back up *after* it
        had been sent. The Pi logged a successful write, the fanout reported no
        failure, and the screen sat on "waiting for the Pi" forever.

        That is the worst shape a bug can take on this device: every layer
        reports success and the glass is wrong. Redrawing on reboot fixes the
        boot race and, for free, makes the screen self-healing when the panel
        is unplugged and plugged back in.
        """
        logger.info("Panel rebooted; redrawing", extra={"event": str(event)})
        await self._show_status_card()

    def _build_view(self) -> ScreenServer | None:
        """The browser view, served whenever some surface is headless.

        An ESP32 has its own glass, so a device whose *only* screen is the
        panel does not need an HTTP copy of it. But `[display].mirror` exists
        precisely so a monitor plugged into the Pi can show the same state, and
        the browser page is how that happens without a second renderer — so the
        test is "is any surface headless", not "is the primary one".

        When the display *is* headless the page is also the only input device
        the machine has, so it is handed two write callables. It gets callables
        and not objects deliberately: `view` may not import `agent`, and the
        rule that has kept this tree layered is that the composition root does
        the introducing.
        """
        view = self._config.view
        if not view.enabled or not self._headless_surfaces():
            return None
        display = self._headless_display()

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

        host, token = self._view_binding()
        return ScreenServer(
            screen_html,
            host=host,
            token=token,
            port=view.port,
            refresh_seconds=view.refresh_seconds,
            width=self._config.display.width,
            height=self._config.display.height,
            submit_text=self.submit,
            pending_choice=pending,
            answer_choice=answer,
            mode_source=self._current_mode,
            set_mode=self._switch_mode,
        )

    def _current_mode(self) -> str:
        """The running session's permission mode, read from the HTTP thread.

        One attribute holding an enum, so there is nothing here to tear. Falls
        back to the configured mode before the session has started, which is
        what it will be — reporting nothing would make the selector render
        with no option marked, and a safety control that shows no state reads
        as broken.
        """
        if self.session.state is ComponentState.STARTED:
            return str(self.session.mode)
        return str(self._config.agent.mode)

    async def _switch_mode(self, mode: str) -> None:
        """Change the permission mode at runtime (D14).

        The composition root does the introducing, as it does for `submit` and
        the prompt: `view` is not allowed to know this lands on
        `AgentSession.set_mode`, and it takes a plain string rather than a
        `PermissionMode` so `view` need not import `core.config` for an enum
        it only displays. Validation of the string happens on both sides —
        `ScreenServer` refuses anything outside its selectable set, and this
        constructor raises on anything `PermissionMode` does not know.
        """
        await self.session.set_mode(PermissionMode(mode))

    def _view_binding(self) -> tuple[str, str | None]:
        """Where the view listens, and the secret that lets anyone in.

        Remote is the default because the alternative is an operator who has
        to SSH into a handheld to answer a yes/no question it just drew on its
        own screen — which means, in practice, the questions go unanswered.
        The property that makes it safe is the token, not the interface, so
        the two are decided in one place: a non-loopback bind never happens
        without one, and `ScreenServer.start()` refuses if this ever gets it
        wrong.
        """
        view = self._config.view
        host = view.host
        if view.remote and is_loopback(host):
            # A wildcard bind rather than a guessed address: the Pi is on
            # Wi-Fi and Ethernet at different times and its address changes
            # under it. `ScreenServer.url` resolves this back to a hostname a
            # browser can use.
            host = "0.0.0.0"  # noqa: S104 - deliberate, and gated on the token below
        if is_loopback(host) and view.token is None:
            # Loopback with no token is the original chunk F1 behaviour, and
            # it stays reachable without ceremony. Anything else needs one.
            return host, None
        return host, view.token or self._ensure_view_token()

    def _ensure_view_token(self) -> str:
        """Read the device's view token, generating it on first start.

        Kept in `var/` and not in config: a secret that lives in the config
        file is a secret that gets committed, and this one has to survive a
        restart without the operator re-learning it. 0600 at creation rather
        than a `chmod` afterwards, so it is never briefly world-readable.
        """
        path = Path(self._config.core.data_dir) / VIEW_TOKEN_FILE
        with contextlib.suppress(OSError):
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        token = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        logger.info("Generated a view token", extra={"path": str(path)})
        return token

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
        is worse than one that misses all of them. Since D44 that consumer is
        `InputBroker` rather than this prompter — the prompter *borrows* the
        stream for the length of one question — but the rule is unchanged and
        now enforced in one place: exactly one `InputChoicePrompter` is ever
        constructed, here, and nothing else in the tree is given `self.input`.

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
        # Keyed on the *primary* surface, not on any of them: the question is
        # "how does the operator answer", and that is decided by the device's
        # own face. A mirrored HDMI monitor adds a place to read the prompt, not
        # a way to answer it — an esp32 primary still answers with the joystick.
        if self._config.display.driver in _HEADLESS_DRIVERS:
            if self._config.view.enabled:
                return ExternalChoicePrompter(show=show)
            return NullChoicePrompter(show=show)
        return InputChoicePrompter(self.input_broker, show=show)

    def _headless_surfaces(self) -> list[str]:
        """Surfaces with no glass of their own, and so the ones a browser page
        is the renderer for."""
        return [s for s in self._config.display.surfaces if s in _HEADLESS_DRIVERS]

    def _headless_display(self) -> object:
        """The headless surface's own driver, which owns the HTML the view serves.

        With mirroring, `self.display` may be a fanout — and there is no
        meaningful way to mirror `.screen.html`, so the view has to reach the
        one surface that has it.
        """
        fanout_surface = getattr(self.display, "surface", None)
        if fanout_surface is not None:
            for name in self._headless_surfaces():
                found = fanout_surface(name)
                if found is not None:
                    return found
        return self.display

    def _ordered_components(self) -> list[Component]:
        """Start order. Stop order is exactly its reverse (`ComponentRegistry`)."""
        # First, before the database it protects and long before the backend
        # session it exists to keep singular (D45). A second instance must fail
        # having opened nothing — `ComponentRegistry` stops in reverse, so a
        # lock taken here is also the last thing released.
        components: list[Component] = [self.instance_lock, self.db, self.migrator, self.bus]
        if self.esp32_link is not None:
            # Before anything that draws. A display write to a link that has
            # not started raises, and the first thing F1 guarantees is that the
            # screen is never blank — so the cable comes up before the renderer.
            components.append(self.esp32_link)
        if self.view is not None:
            components.append(self.view)
        # The renderer and the authorization prompt subscribe, so both must be
        # up before the session can publish a turn — and both must still be up
        # while the session stops.
        components.extend([self.renderer, self.authprompt, self.input])
        # After the stream it reads and before the router that feeds it, so
        # the one reader is already iterating when the first press can arrive.
        # Stopping in reverse retires it before the stream closes, which is
        # what keeps shutdown from logging a broker that "stopped" on a
        # cancelled generator.
        components.append(self.input_broker)
        if self.input_router is not None:
            # After `self.input`, so the stream's tick loop is running before
            # anything can be fed into it, and after the link, which is already
            # earlier in this list.
            components.append(self.input_router)
        if self.delivery is not None:
            # After the migrations that create its table and after the
            # authorization prompt, so that a notification arriving during a
            # pending authorization finds the screen already claimed and
            # defers rather than racing it.
            components.append(self.delivery)
        if self.governor is not None:
            # After the database (its one opportunistic workload reads the
            # ledger) and before the session, so it is already subscribed when
            # the first turn starts. Stopping in reverse means background work
            # is cancelled *after* the session has closed its turns, which is
            # the order that leaves nothing half-written.
            components.append(self.governor)
        if self.panel_keeper is not None:
            # Last of the display chain and late in the list generally: it
            # replays whatever the fanout drew most recently, so starting it
            # before anything has drawn simply means its first few ticks are
            # no-ops. Stopping in reverse order retires it before the link it
            # writes through goes down, which is what keeps shutdown from
            # logging a repaint failure on the way out.
            components.append(self.panel_keeper)
        components.append(self.session)
        if self.self_improve is not None:
            # After the session, because it drives it: a tick that fired before
            # `AgentSession.start()` would reach a session with no id and no
            # backend. Stopping in reverse order is the half that matters more
            # — the trigger is retired first, so nothing can start a new turn
            # while the session is closing the one in flight.
            components.append(self.self_improve)
        return components

    # -- lifecycle ---------------------------------------------------------

    @property
    def config(self) -> NomadConfig:
        """The config this device was built from.

        Exposed read-only because `nomad.status` has to report what was asked
        for alongside what is actually running, and reaching into `_config`
        from outside would make every reader a maintainer of this class.
        """
        return self._config

    @property
    def state(self) -> ComponentState:
        return self._state

    @property
    def view_url(self) -> str | None:
        return self.view.url if self.view is not None else None

    @property
    def view_login_url(self) -> str | None:
        """The view's address *with* its token, for handing to the operator.

        Separate from `view_url` on purpose. `view_url` is logged at boot and
        goes into the structured record; this one is only ever printed by
        `nomad status`, at a terminal the operator is already sitting at.
        """
        return self.view.login_url if self.view is not None else None

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
        await self._show_status_card()
        self._schedule_panel_redraw()
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

    def _schedule_panel_redraw(self) -> None:
        """Draw the card once more, after the panel has finished restarting.

        Held as a task so `stop()` can cancel it: a four-second sleep that
        outlives the app would draw to a link that has already closed, which
        turns a clean shutdown into a traceback.
        """
        if self.esp32_link is None:
            return

        async def redraw() -> None:
            try:
                await asyncio.sleep(PANEL_SETTLE_SECONDS)
                await self._show_status_card()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a cosmetic redraw, never fatal
                logger.warning("Panel settle redraw failed", extra={"error": str(exc)})

        self._panel_redraw = asyncio.ensure_future(redraw())

    async def _show_status_card(self) -> None:
        """The first thing on the glass: what this device is and whether it works.

        F1 promised the screen is never blank; this is what makes that promise
        say something. It draws through `ScreenOwner` like every other writer
        (D36), so a boot that lands while an authorization prompt is up is
        suppressed rather than painting over the question.

        `probe=False` deliberately: the backend has already connected or
        failed by the time this runs, so spawning a CLI to ask `--version` a
        second time would put several seconds between power-on and the screen
        saying anything. The subprocess probe belongs to `nomad status`, which
        is a command someone is waiting on.

        A failure here is logged and dropped. A device that refuses to finish
        booting because it could not draw a summary of itself has turned a
        cosmetic problem into an outage.

        **On a device with a wake button it is a `choice` screen instead**
        (D44), carrying the same facts as its question and `Wake` as its one
        option. This is the frame that sits on the glass from power-on until
        something else draws — on a six-hour schedule, potentially all
        morning — so it is exactly the screen that must be tappable. The card
        layout is the thing given up, not the information.
        """
        try:
            report = await collect_status(self, probe=False)
            headline = "ready" if report.backend.ready else "backend down"
            view = self.screen.view(STATUS_WRITER)
            label = self._wake_label()
            if label is None:
                await view.show_card(self._config.core.name, headline, status_rows(report))
                return
            summary = " · ".join(
                [f"{self._config.core.name} — {headline}"]
                + [f"{key}: {value}" for key, value in status_rows(report)]
            )
            await view.show_choice(summary, [label])
        except Exception as exc:  # noqa: BLE001 - never fail a boot over a status card
            logger.warning("Could not draw the status card", extra={"error": str(exc)})

    async def stop(self) -> None:
        """Stop whatever is running. Safe after a partial or failed start.

        `ComponentRegistry` only stops what it actually started and swallows
        stop failures, so this is a no-op on a fresh app and a partial teardown
        on a half-started one — neither of which raises.
        """
        self._state = ComponentState.STOPPING
        if self._panel_redraw is not None:
            # Before the registry, so the sleep cannot wake into a closed link.
            self._panel_redraw.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._panel_redraw
            self._panel_redraw = None
        await self._registry.stop_all()
        self._state = ComponentState.STOPPED
        logger.info("Nomad stopped")
