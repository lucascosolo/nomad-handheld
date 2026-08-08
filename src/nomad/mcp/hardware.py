"""Nomad's hardware, as tools the model can call (D19).

Claude Code arrives with filesystem, shell, search and web tools that are
better than anything Nomad would write. What it has no equivalent for is *this
device*: a screen, a battery, a joystick, a USB HID output. That gap is what
`mcp/` fills, and it is the entire reason Nomad runs an MCP server at all.

These are ordinary Nomad `Tool`s. They carry a `ToolSpec`, they are gated by
the same broker, and they are executed by `ToolExecutor.run(grant, request)`
like everything else — being reachable over MCP changes how a tool is
*addressed*, never how it is *authorized*.

**Driver facades, not drivers.** The concrete drivers land in `hardware/` with
chunk E. Until then these tools talk to the narrow protocols below, which is
also what lets the whole surface be tested with no hardware attached (D9).
Chunk E implements these protocols; it should not need to touch this file.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from nomad.targets.base import Capability
from nomad.tools.base import Permission, Risk, ToolContext, ToolResult, ToolSpec


class BatteryStatus(BaseModel):
    """What the power system reports (D18)."""

    percent: float
    charging: bool = False
    voltage: float | None = None


@runtime_checkable
class DisplayDriver(Protocol):
    """The screen, as much of it as a tool needs to know about.

    Deliberately not one method. A display tool's schema is the ceiling on
    what the model can imagine its own face doing — `show_text` alone taught
    every self-authored app that the screen is a text box. Card, list and
    choice are the same ceiling raised to what a small pocket screen actually
    needs: a default way to answer, a way to enumerate things, and a way to
    ask the operator something answerable with a joystick.

    Row and item payloads are plain tuples rather than the pydantic models
    the MCP tools validate against, so an implementation needs nothing but
    the standard library to satisfy this protocol structurally (see
    `nomad.hardware`, which implements it without importing this module).
    """

    async def show_text(self, text: str, *, title: str | None = None) -> None: ...

    async def show_card(self, title: str, body: str, rows: list[tuple[str, str]]) -> None: ...

    async def show_list(
        self,
        title: str,
        items: list[tuple[str, str | None]],
        *,
        selectable: bool = False,
    ) -> None: ...

    async def show_choice(self, question: str, options: list[str]) -> None: ...


@runtime_checkable
class ChoiceResultLike(Protocol):
    """What a prompter hands back. `describe()` is what the model is told."""

    def describe(self) -> str: ...


@runtime_checkable
class ChoicePrompter(Protocol):
    """Asks the operator something and waits for the answer (D13).

    Declared here, next to the other driver facades, and implemented in
    `input/choice.py` — the facade must not import the input layer, exactly as
    it does not import `hardware`.
    """

    async def ask(
        self, question: str, options: list[str], *, timeout_s: float | None = None
    ) -> ChoiceResultLike: ...


@runtime_checkable
class BatteryDriver(Protocol):
    async def read(self) -> BatteryStatus: ...


@runtime_checkable
class HidDriver(Protocol):
    """The RP2040. Treated as an output weapon, never a convenience (D12)."""

    async def type_text(self, text: str) -> None: ...


class MockDisplay:
    """Records what would have been drawn. The default (D9).

    A mock that takes a different branch than production is worse than no
    mock, so every method of the widened protocol is recorded here, not just
    `show_text`.
    """

    def __init__(self) -> None:
        self.shown: list[tuple[str, str | None]] = []
        self.cards: list[tuple[str, str, list[tuple[str, str]]]] = []
        self.lists: list[tuple[str, list[tuple[str, str | None]], bool]] = []
        self.choices: list[tuple[str, list[str]]] = []

    async def show_text(self, text: str, *, title: str | None = None) -> None:
        self.shown.append((text, title))

    async def show_card(self, title: str, body: str, rows: list[tuple[str, str]]) -> None:
        self.cards.append((title, body, rows))

    async def show_list(
        self,
        title: str,
        items: list[tuple[str, str | None]],
        *,
        selectable: bool = False,
    ) -> None:
        self.lists.append((title, items, selectable))

    async def show_choice(self, question: str, options: list[str]) -> None:
        self.choices.append((question, options))


class MockBattery:
    def __init__(self, status: BatteryStatus | None = None) -> None:
        self.status = status or BatteryStatus(percent=87.0, charging=False, voltage=4.02)

    async def read(self) -> BatteryStatus:
        return self.status


class MockHid:
    def __init__(self) -> None:
        self.typed: list[str] = []

    async def type_text(self, text: str) -> None:
        self.typed.append(text)


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


class DisplayTextParams(BaseModel):
    text: str = Field(description="The text to show on Nomad's screen.")
    title: str | None = Field(default=None, description="Optional heading.")


class DisplayTextTool:
    """Draw text on the device's own screen."""

    spec = ToolSpec(
        name="display_text",
        device_local=True,
        description="Show a short message on Nomad's built-in screen.",
        params_model=DisplayTextParams,
        # Mutating rather than read-only: it changes what the user sees, and a
        # screen the model can silently overwrite is a screen that can lie
        # about what the device is doing.
        risk=Risk.MUTATING,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(self, display: DisplayDriver) -> None:
        self._display = display

    async def execute(self, params: DisplayTextParams, ctx: ToolContext) -> ToolResult:
        await self._display.show_text(params.text, title=params.title)
        return ToolResult.success(f"displayed {len(params.text)} characters")


class KeyValueRow(BaseModel):
    key: str = Field(description="Row label, e.g. 'Battery'.")
    value: str = Field(description="Row value, e.g. '84%'.")


class DisplayCardParams(BaseModel):
    title: str = Field(description="Card heading.")
    body: str = Field(default="", description="Short body text below the title.")
    rows: list[KeyValueRow] = Field(
        default_factory=list,
        max_length=8,
        description="Optional key/value rows, e.g. status fields.",
    )


class DisplayCardTool:
    """Draw a titled card: a short body plus optional key/value rows.

    The default way to answer — reach for this before falling back to
    `display_text`.
    """

    spec = ToolSpec(
        name="display_card",
        device_local=True,
        description=(
            "Show a titled card on Nomad's screen: a short body plus optional "
            "key/value rows. The default way to answer."
        ),
        params_model=DisplayCardParams,
        risk=Risk.MUTATING,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(self, display: DisplayDriver) -> None:
        self._display = display

    async def execute(self, params: DisplayCardParams, ctx: ToolContext) -> ToolResult:
        rows = [(row.key, row.value) for row in params.rows]
        await self._display.show_card(params.title, params.body, rows)
        return ToolResult.success(f"displayed card '{params.title}' with {len(rows)} row(s)")


class DisplayListItem(BaseModel):
    label: str = Field(description="Item text.")
    detail: str | None = Field(default=None, description="Optional secondary text.")


class DisplayListParams(BaseModel):
    title: str = Field(description="List heading.")
    items: list[DisplayListItem] = Field(
        min_length=1, max_length=20, description="Items to list."
    )
    selectable: bool = Field(
        default=False, description="Whether the operator can move a cursor through the items."
    )


class DisplayListTool:
    """Draw a titled list of items on Nomad's screen."""

    spec = ToolSpec(
        name="display_list",
        device_local=True,
        description="Show a titled list of items on Nomad's screen, optionally selectable.",
        params_model=DisplayListParams,
        risk=Risk.MUTATING,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(self, display: DisplayDriver) -> None:
        self._display = display

    async def execute(self, params: DisplayListParams, ctx: ToolContext) -> ToolResult:
        items = [(item.label, item.detail) for item in params.items]
        await self._display.show_list(params.title, items, selectable=params.selectable)
        return ToolResult.success(f"displayed list '{params.title}' with {len(items)} item(s)")


class DisplayChoiceParams(BaseModel):
    question: str = Field(description="What to ask the operator.")
    options: list[str] = Field(
        min_length=2,
        max_length=4,
        description="2-4 options, answerable with a joystick.",
    )
    timeout_s: float | None = Field(
        default=None,
        gt=0.0,
        le=600.0,
        description=(
            "How long to wait for an answer before giving up. Omit for the "
            "default. The operator may not be looking at the screen."
        ),
    )


class DisplayChoiceTool:
    """Ask the operator a question with 2-4 options, joystick-answerable."""

    spec = ToolSpec(
        name="display_choice",
        device_local=True,
        description=(
            "Ask the operator a question on Nomad's screen with 2-4 options they "
            "can answer with a joystick."
        ),
        params_model=DisplayChoiceParams,
        risk=Risk.MUTATING,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(self, display: DisplayDriver, prompter: ChoicePrompter | None = None) -> None:
        self._display = display
        # No prompter means no input hardware is wired up yet. The question is
        # still drawn — showing it is useful — but the tool reports that it
        # cannot be answered rather than returning a confirmation the model
        # would reasonably read as an answer.
        self._prompter = prompter

    async def execute(self, params: DisplayChoiceParams, ctx: ToolContext) -> ToolResult:
        if self._prompter is None:
            await self._display.show_choice(params.question, params.options)
            return ToolResult.success(
                f"showed '{params.question}', but no input device is attached, "
                "so it cannot be answered"
            )
        result = await self._prompter.ask(
            params.question, list(params.options), timeout_s=params.timeout_s
        )
        return ToolResult.success(result.describe())


class BatteryParams(BaseModel):
    """No parameters."""


class ReadBatteryTool:
    """Report charge state, so the agent can defer work before power runs out."""

    spec = ToolSpec(
        name="read_battery",
        description="Report Nomad's battery percentage and charging state.",
        params_model=BatteryParams,
        risk=Risk.READ_ONLY,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(self, battery: BatteryDriver) -> None:
        self._battery = battery

    async def execute(self, params: BatteryParams, ctx: ToolContext) -> ToolResult:
        status = await self._battery.read()
        state = "charging" if status.charging else "discharging"
        return ToolResult.success(
            f"battery: {status.percent:.0f}% ({state})",
            percent=status.percent,
            charging=status.charging,
            voltage=status.voltage,
        )


NetworkCheck = Callable[[], Awaitable[bool]]


async def _default_network_check() -> bool:
    """A quick, best-effort reachability probe. Never raises — offline is a
    normal answer, not a failure of this tool."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("1.1.1.1", 53), timeout=1.5
        )
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


class GetContextParams(BaseModel):
    """No parameters."""


class GetContextTool:
    """Ambient context: local time, battery, network reachability, uptime.

    "Understands my needs" is mostly knowing what time it is and whether the
    operator is on battery. Read-only, cheap, no prompts — a later trigger
    layer polls this to decide *when* to speak, rather than firing at random.

    `network_check` and `clock` are injectable so tests never depend on real
    network access or wall-clock time. `started_at` anchors "uptime" to when
    this tool was constructed — a process-uptime proxy, not a system call,
    since there is no hardware-backed uptime source yet.
    """

    spec = ToolSpec(
        name="get_context",
        description=(
            "Report local time, battery state, network reachability and uptime — "
            "the operator's ambient context."
        ),
        params_model=GetContextParams,
        risk=Risk.READ_ONLY,
        permissions=frozenset(),
        required_capabilities=frozenset(),
    )

    def __init__(
        self,
        battery: BatteryDriver,
        *,
        network_check: NetworkCheck | None = None,
        clock: Callable[[], datetime] | None = None,
        started_at: float | None = None,
    ) -> None:
        self._battery = battery
        self._network_check = network_check or _default_network_check
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._started_at = time.monotonic() if started_at is None else started_at

    async def execute(self, params: GetContextParams, ctx: ToolContext) -> ToolResult:
        status = await self._battery.read()
        moment = self._clock()
        reachable = await self._network_check()
        uptime_seconds = max(0.0, time.monotonic() - self._started_at)
        tz_name = moment.tzname() or "UTC"
        state = "charging" if status.charging else "discharging"
        summary = (
            f"{moment.strftime('%Y-%m-%d %H:%M')} {tz_name}, "
            f"battery {status.percent:.0f}% ({state}), "
            f"network {'reachable' if reachable else 'unreachable'}, "
            f"up {uptime_seconds:.0f}s"
        )
        return ToolResult.success(
            summary,
            local_time=moment.isoformat(),
            timezone=tz_name,
            battery_percent=status.percent,
            charging=status.charging,
            network_reachable=reachable,
            uptime_seconds=uptime_seconds,
        )


class HidTypeParams(BaseModel):
    text: str = Field(description="Text to type into the attached computer.")


class HidTypeTextTool:
    """Type into whatever Nomad is plugged into.

    The most dangerous thing this device can do, and the reason `never_auto`
    exists (D14, D21). It is `EXTERNAL_DEVICE` risk, it declares
    `HID_OUTPUT`, and it requires a target with the `HID_OUTPUT` capability —
    three independent reasons it can never be auto-approved, in any mode.
    A mode switch must not be able to turn a pocket computer into a keystroke
    injector.
    """

    spec = ToolSpec(
        name="hid_type_text",
        description=(
            "Type text as a USB keyboard into the computer Nomad is plugged into. "
            "Always requires explicit human approval."
        ),
        params_model=HidTypeParams,
        risk=Risk.EXTERNAL_DEVICE,
        permissions=frozenset({Permission.HID_OUTPUT}),
        required_capabilities=frozenset({Capability.HID_OUTPUT}),
        never_auto=True,
    )

    def __init__(self, hid: HidDriver) -> None:
        self._hid = hid

    async def execute(self, params: HidTypeParams, ctx: ToolContext) -> ToolResult:
        await self._hid.type_text(params.text)
        return ToolResult.success(f"typed {len(params.text)} characters")
