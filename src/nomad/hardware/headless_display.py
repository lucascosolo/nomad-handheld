"""A screen with nothing behind it but this process (D9).

Nothing is wired up yet: no ESP32, no serial port, no CLI, no credentials.
`HeadlessDisplay` is what lets the UI shell and self-authored apps be demoed
and tested against exactly that situation — it renders Nomad's whole display
vocabulary (text, card, list, choice) to a plain-text and a simple-HTML
representation retrievable in-process, with no hardware attached.

This is load-bearing, not a toy: it is the difference between "the display
tools have unit tests" and "you can watch what the model just put on the
screen while building the rest of the stack."

Implements the widened `DisplayDriver` protocol from `nomad.mcp.hardware`
*structurally* — it is not imported here. `hardware` sits below `mcp` in the
dependency graph, so the direction of knowledge is the same one that module's
own docstring states: drivers do not import their own facade.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape


@dataclass(frozen=True)
class Screen:
    """The most recently rendered content, as plain text and as simple HTML."""

    text: str
    html: str


#: How many past screens are kept. Bounded because this display is no longer
#: only a test fixture: it is what a running device draws on, and a renderer
#: streaming a turn redraws several times a second. An unbounded list would be
#: a slow leak on a 4 GB Pi that is expected to stay powered for days.
DEFAULT_HISTORY_LIMIT = 200


class HeadlessDisplay:
    """Renders every `display_*` tool call to text/HTML, in-process."""

    def __init__(self, *, history_limit: int = DEFAULT_HISTORY_LIMIT) -> None:
        self._screen = Screen(text="", html="<pre></pre>")
        self._history_limit = history_limit
        self.history: list[Screen] = []

    @property
    def screen(self) -> Screen:
        """What is currently 'on screen'."""
        return self._screen

    async def show_text(self, text: str, *, title: str | None = None) -> None:
        lines = ([title] if title else []) + [text]
        self._render("\n".join(lines))

    async def show_card(self, title: str, body: str, rows: list[tuple[str, str]]) -> None:
        lines = [title]
        if body:
            lines.append(body)
        lines.extend(f"{key}: {value}" for key, value in rows)
        self._render("\n".join(lines))

    async def show_list(
        self,
        title: str,
        items: list[tuple[str, str | None]],
        *,
        selectable: bool = False,
    ) -> None:
        lines = [title]
        marker = "> " if selectable else "- "
        for label, detail in items:
            suffix = f" ({detail})" if detail else ""
            lines.append(f"{marker}{label}{suffix}")
        self._render("\n".join(lines))

    async def show_choice(self, question: str, options: list[str]) -> None:
        lines = [question]
        lines.extend(f"[{index + 1}] {option}" for index, option in enumerate(options))
        self._render("\n".join(lines))

    def _render(self, text: str) -> None:
        self._screen = Screen(text=text, html=f"<pre>{escape(text)}</pre>")
        self.history.append(self._screen)
        if len(self.history) > self._history_limit:
            del self.history[: len(self.history) - self._history_limit]
