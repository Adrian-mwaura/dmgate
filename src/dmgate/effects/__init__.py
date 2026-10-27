"""Effect resolution: map tool calls to effects on concrete resources (DESIGN.md §6.2)."""

from dmgate.effects.model import (
    LISTING, RECURSIVE, ROW_SELECTION, SENDS_DATA, SENDS_REPOSITORY, STATE_CHANGING, Effect, Resolution, Verb,
)
from dmgate.effects.registry import Registry, Resource
from dmgate.effects.shell import resolve_shell
from dmgate.effects.sql import resolve_sql

__all__ = [
    "LISTING", "RECURSIVE", "ROW_SELECTION", "SENDS_DATA", "SENDS_REPOSITORY", "STATE_CHANGING", "Effect",
    "Registry", "Resolution", "Resource", "Verb", "resolve_shell", "resolve_sql",
]
