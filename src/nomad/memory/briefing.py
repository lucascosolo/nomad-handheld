"""The briefing: a small constant-size index, not the memory itself.

A memory reachable only through a `recall` tool is a memory the model has to
*suspect exists* before it will use one — it will not call `recall` before
answering "what am I working on". So something has to be injected. But the
opposite failure is worse and slower to notice: a block that grows with the
store is a context leak charged on every single turn of the session, and an
overloaded window makes the model write worse code and invent facts.

So the block is bounded twice over and by construction:

* **only pinned memories are injected**, and pinning is capped in the store —
  the injected set cannot grow with the corpus, only with deliberate pins;
* **whichever of `budget_chars` and `max_memories` binds first, wins**;
* everything else is summarised as **counts by kind**, one line, constant
  size. That line is what makes retrieval work: the model calls `recall` for
  things it knows are there, and it will not go looking for what it has never
  been told exists.

`compose_briefing` is pure and deterministic: same input, same block, in any
input order. That matters because the briefing is part of the system prompt,
and a prompt that reshuffles between sessions defeats caching and makes
behaviour irreproducible.

Truncation happens at a whole-memory boundary, never mid-sentence. A briefing
ending "the operator prefers to be told when a bui" is worse than one that
omits the line: the model reads the fragment as fact.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from nomad.memory.models import Memory

HEADING = "## What you know about your operator"

#: The one-line pointer at the rest of the store. Constant size by design.
INDEX_PREFIX = "Also stored: "
INDEX_SUFFIX = ". Use recall(query) — it is cheap."

EMPTY_BRIEFING = ""


def _order_key(memory: Memory) -> tuple[float, str]:
    """Most recently updated first; `id` makes same-millisecond ties stable.

    Stability is not decoration — it is what keeps the injected prompt byte
    identical across two boots with the same store.
    """
    return (-memory.updated_at.timestamp(), memory.id)


def format_memory(memory: Memory) -> str:
    """One briefing line. Terse, kind-tagged, no trailing whitespace."""
    text = " ".join(memory.text.split())
    return f"- ({memory.kind}) {text}"


def format_index(counts: Mapping[str, int]) -> str:
    """`Also stored: 34 project, 12 preference. Use recall(query) — ...`"""
    present = [(kind, n) for kind, n in counts.items() if n > 0]
    if not present:
        return ""
    present.sort(key=lambda pair: (-pair[1], pair[0]))
    listed = ", ".join(f"{n} {kind}" for kind, n in present)
    return f"{INDEX_PREFIX}{listed}{INDEX_SUFFIX}"


def compose_briefing(
    memories: Iterable[Memory],
    *,
    budget_chars: int,
    max_memories: int,
    unpinned_counts: Mapping[str, int] | None = None,
) -> str:
    """Render the bounded briefing block, or `""` if there is nothing to say.

    `memories` should already be the pinned set; unpinned and forgotten
    entries are dropped here anyway, because a pure function must not depend
    on its caller having filtered correctly.
    """
    pinned = [m for m in memories if m.forgotten_at is None and m.pinned]
    index_line = format_index(unpinned_counts or {})

    if budget_chars <= 0 or (not pinned and not index_line):
        return EMPTY_BRIEFING
    if len(HEADING) > budget_chars:
        return EMPTY_BRIEFING

    # The index line is the part that must survive truncation: it is what tells
    # the model the rest of the store is reachable at all. Reserve its room
    # before spending any on individual memories.
    reserved = len(index_line) + 1 if index_line else 0
    block = HEADING
    used = 0

    for memory in sorted(pinned, key=_order_key):
        if used >= max(0, max_memories):
            break
        line = format_memory(memory)
        if len(block) + 1 + len(line) + reserved > budget_chars:
            # Whole-memory boundary: stop rather than hunt for a shorter line
            # that still fits, which would reorder the briefing by length.
            break
        block = f"{block}\n{line}"
        used += 1

    if index_line:
        block = f"{block}\n{index_line}"
    elif not used:
        return EMPTY_BRIEFING

    return block
