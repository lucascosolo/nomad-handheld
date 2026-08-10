"""Things that begin a turn nobody asked for (chunk P).

This package sits *above* `agent`: a trigger drives the session, the session
knows nothing about triggers. That is why `TurnSource` lives in
`nomad.core.turns` and not here — `agent` has to stamp a turn with its
provenance, and it may not import this package to do it.

`SelfImproveTrigger` is the only member today. Foreground/background lanes and
sensor triggers are the rest of chunk P and are deliberately absent: the one
thing that unblocks self-improvement is the ability to start a turn on a tick,
and everything else can be added without moving this boundary.
"""

from __future__ import annotations

from nomad.triggers.self_improve import SELF_IMPROVE_PROMPT, ReadinessProbe, SelfImproveTrigger

__all__ = ["SELF_IMPROVE_PROMPT", "ReadinessProbe", "SelfImproveTrigger"]
