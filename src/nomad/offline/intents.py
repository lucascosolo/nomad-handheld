"""Exact intent matching, and why there is no number to turn up.

The offline tier exists so that "what's my battery" does not pay a round trip
to a datacentre. The failure it must not have is *mis-reading* one: a round
trip is a delay the operator can see, and a wrong intent is an answer they
cannot tell apart from a right one. So this module fails **toward** the model,
and it does that structurally rather than by policy.

Three properties carry that, and each one is the absence of something:

- **No similarity.** Matching is equality over a canonical form, never a
  distance between two strings. There is no ratio, no edit distance, no
  embedding and nothing imported that could compute one. An utterance either
  *is* one of the phrases, once normalized, or it is not.
- **No tunable.** There is no cutoff parameter anywhere here, which is the
  point: a cutoff is a dial, a dial gets turned up the first afternoon someone
  is annoyed that "battery?" did not match, and every turn of it buys recall by
  spending correctness in a way nobody can observe afterwards. The way to catch
  a new phrasing is to add the phrase — a line in a diff, in `catalog.py`, that
  a human read.
- **No ranking.** When two intents match the same utterance the router returns
  nothing at all rather than picking a winner. Ranking is a cutoff wearing a
  different hat; ambiguity is exactly the case where the model is better than
  this file, and it is one line of code to hand it over.

Normalization *is* permitted to be lossy, and that is not a contradiction: it
is a fixed, total, closed transformation — case folded, a fixed punctuation set
removed, whitespace collapsed — applied identically to the catalog and to the
utterance. It cannot make two different phrases match "a bit"; it makes them
match or it does not, and what it does is readable at the top of this file
rather than emergent from a corpus.

Slot templates are the one place where an utterance is not compared whole, and
they are held to the same rule: a template is a literal token sequence with
exactly one hole, the tokens either align exactly or they do not, and the hole
must parse under a total grammar (`_parse_duration`) or the match is abandoned.
A template never skips, stems, or tolerates a word it did not expect.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

#: Characters dropped outright, so "what's" and "whats" are one phrase.
_ELIDED = "'’`"

#: The slot's raw text, substituted into a param by name.
SLOT_TEXT = "<slot:text>"
#: The slot's parsed value — seconds for a duration.
SLOT_VALUE = "<slot:value>"

#: Single-token number words. Compounds ("twenty five") are deliberately absent:
#: they are unambiguous but they are the first step toward a parser that guesses,
#: and an unparsed duration costs one round trip.
_NUMBER_WORDS: dict[str, float] = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "ninety": 90,
}

_UNIT_SECONDS: dict[str, float] = {
    "second": 1.0,
    "seconds": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "s": 1.0,
    "minute": 60.0,
    "minutes": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "m": 60.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "h": 3600.0,
}

#: Longest duration the router will build a timer for. Matches the ceiling on
#: `set_timer`'s own `seconds` parameter: a router that can construct a call the
#: tool's own model rejects has turned a phrase the operator said into a
#: validation error, which is a worse outcome than a round trip.
MAX_ROUTED_DURATION_SECONDS = 86400.0

#: Longest slot a place name may be, in tokens. "new york" and "asia tokyo" fit;
#: a sentence does not.
MAX_ZONE_TOKENS = 3


def normalize(text: str) -> str:
    """The canonical form both sides of a comparison are put in.

    Total and closed: every character is folded, elided, or turned into a word
    break, with no branch that depends on the rest of the string. Two utterances
    that normalize identically are the same utterance for every purpose in this
    package, including the evidence ledger's dedup key.

    Note that punctuation becoming a break costs the IANA spelling of a zone —
    "asia/tokyo" arrives as two tokens and `world_clock` will refuse it. That is
    the right trade: the alias table already covers "tokyo", and the refusal
    routes to the model rather than to a wrong answer.
    """
    out = [char if char.isalnum() else " " for char in text.casefold() if char not in _ELIDED]
    return " ".join("".join(out).split())


def tokens(text: str) -> tuple[str, ...]:
    return tuple(normalize(text).split())


class SlotKind(StrEnum):
    """What the one hole in a template is allowed to contain."""

    #: "ten minutes", "90 seconds", "1 hour". Parsed to seconds or refused.
    DURATION = "duration"
    #: A place or IANA zone, passed through as text for the tool to validate.
    ZONE = "zone"


@dataclass(frozen=True)
class Intent:
    """One thing the device can answer without the radio.

    A dataclass rather than a pydantic model on purpose: this is a source
    constant reviewed in a diff, not data crossing a boundary. `params` is the
    call the router will build, with `SLOT_TEXT` / `SLOT_VALUE` standing in for
    whatever the template captured.
    """

    name: str
    tool: str
    phrases: tuple[str, ...] = ()
    templates: tuple[str, ...] = ()
    slot: SlotKind | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    #: One line, shown by `offline_status`. The operator should be able to read
    #: the whole offline surface without reading this file.
    summary: str = ""


#: Sentinel for a phrase two catalog entries both claim. Never returned to a
#: caller: it makes the router refuse the phrase, which is what the ambiguity
#: rule would do anyway once both intents matched.
_AMBIGUOUS = Intent(name="<ambiguous>", tool="")


class IntentMatch(BaseModel):
    """A routed utterance: which intent, which tool, and the exact call.

    **There is no confidence field, and its absence is the design.** A number
    here would be a number somewhere else to compare it against, and this
    package's whole claim is that it either knew or it did not.
    """

    intent: str
    tool: str
    params: dict[str, Any] = Field(default_factory=dict)
    #: The catalog entry that matched, verbatim, so an audit trail can say why.
    matched: str
    #: The normalized utterance, which is also the ledger's key.
    normalized: str


def _parse_duration(slot: Sequence[str]) -> float | None:
    """`<number> <unit>`, and nothing else. `None` means "ask the model"."""
    if len(slot) != 2:
        return None
    raw_number, raw_unit = slot
    unit = _UNIT_SECONDS.get(raw_unit)
    if unit is None:
        return None
    if raw_number in _NUMBER_WORDS:
        count = _NUMBER_WORDS[raw_number]
    elif raw_number.isdigit():
        count = float(raw_number)
    else:
        return None
    total = count * unit
    if total <= 0 or total > MAX_ROUTED_DURATION_SECONDS:
        return None
    return total


def _parse_zone(slot: Sequence[str]) -> str | None:
    """A short run of alphabetic tokens. The tool decides if it is a real zone.

    Passing an unknown place through is safe in a way passing an unparsed
    duration is not: `world_clock` refuses it, the refusal is a failed tool
    result, and a failed result is not a handled utterance — so it reaches the
    model anyway, one tool call later.
    """
    if not 1 <= len(slot) <= MAX_ZONE_TOKENS:
        return None
    if not all(token.isalpha() for token in slot):
        return None
    return " ".join(slot)


def _slot_value(kind: SlotKind | None, slot: Sequence[str]) -> Any | None:
    if kind is SlotKind.DURATION:
        return _parse_duration(slot)
    if kind is SlotKind.ZONE:
        return _parse_zone(slot)
    return None


def template_tokens(template: str) -> tuple[str, ...]:
    """Tokenize a template, keeping its one hole intact.

    `normalize` deletes punctuation, which would eat the `{}` marker along with
    everything else — so the literal halves are normalized and the marker is
    reinserted between them. A template with no hole, or with two, tokenizes to
    something `_align` refuses, which is the safe reading of a malformed entry.
    """
    parts = template.split("{}")
    if len(parts) != 2:
        return tokens(template)
    return (*tokens(parts[0]), "{}", *tokens(parts[1]))


def _align(template: tuple[str, ...], utterance: tuple[str, ...]) -> tuple[str, ...] | None:
    """Split `utterance` around the one hole in `template`, or refuse.

    The hole is the literal token `{}`. Everything either side must match
    position for position; the hole must capture at least one token. No
    skipping, no optional words, no second hole.
    """
    if template.count("{}") != 1:
        return None
    hole = template.index("{}")
    prefix, suffix = template[:hole], template[hole + 1 :]
    if len(utterance) <= len(prefix) + len(suffix):
        return None
    if utterance[: len(prefix)] != prefix:
        return None
    if suffix and utterance[len(utterance) - len(suffix) :] != suffix:
        return None
    return utterance[len(prefix) : len(utterance) - len(suffix)]


class IntentRouter:
    """A fixed phrase set, matched exactly, onto tools that already exist.

    The router knows tool *names* and nothing else about them: it does not hold
    a registry, cannot see a `ToolSpec`, and has no way to run anything. What it
    produces is a proposed call, which `OfflineResponder` then puts through the
    broker like any other. Keeping the two apart is what makes "the offline path
    cannot execute without a grant" checkable by reading one small file.
    """

    def __init__(self, intents: Sequence[Intent]) -> None:
        self._intents = tuple(intents)
        self._phrases: dict[str, Intent] = {}
        self._templates: list[tuple[tuple[str, ...], Intent]] = []
        for intent in self._intents:
            for phrase in intent.phrases:
                key = normalize(phrase)
                if key in self._phrases and self._phrases[key] is not intent:
                    # Two intents claiming one phrase is a catalog bug, and the
                    # safe reading of a bug is "we do not know what this means".
                    # Dropping both is what the ambiguity rule would do anyway.
                    self._phrases[key] = _AMBIGUOUS
                else:
                    self._phrases[key] = intent
            for template in intent.templates:
                self._templates.append((template_tokens(template), intent))

    @property
    def intents(self) -> tuple[Intent, ...]:
        return self._intents

    def phrase_count(self) -> int:
        return sum(len(i.phrases) + len(i.templates) for i in self._intents)

    def match(self, utterance: str) -> IntentMatch | None:
        """The whole router. `None` means "the model answers this"."""
        normalized = normalize(utterance)
        if not normalized:
            return None

        hits: list[IntentMatch] = []
        exact = self._phrases.get(normalized)
        if exact is _AMBIGUOUS:
            return None
        if exact is not None:
            hits.append(
                IntentMatch(
                    intent=exact.name,
                    tool=exact.tool,
                    params=_resolve_params(exact.params, text=None, value=None),
                    matched=normalized,
                    normalized=normalized,
                )
            )

        spoken = tuple(normalized.split())
        for template, intent in self._templates:
            slot = _align(template, spoken)
            if slot is None:
                continue
            value = _slot_value(intent.slot, slot)
            if value is None:
                continue
            hits.append(
                IntentMatch(
                    intent=intent.name,
                    tool=intent.tool,
                    params=_resolve_params(intent.params, text=" ".join(slot), value=value),
                    matched=" ".join(template),
                    normalized=normalized,
                )
            )

        if len(hits) != 1:
            # Zero is "not ours". More than one is "we do not know which", and
            # the model is strictly better at that than a table is.
            return None
        return hits[0]


def _resolve_params(
    params: Mapping[str, Any], *, text: str | None, value: Any | None
) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    for key, raw in params.items():
        if raw == SLOT_TEXT:
            resolved[key] = text
        elif raw == SLOT_VALUE:
            resolved[key] = value
        else:
            resolved[key] = raw
    return resolved
