"""Errors raised while loading or reading skills."""

from __future__ import annotations

from nomad.core.errors import NomadError


class SkillError(NomadError):
    """A skill could not be parsed, or was asked for and does not exist."""
