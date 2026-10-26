"""Effect resolution: map tool calls to effects on concrete resources (DESIGN.md §6.2)."""

from dmgate.effects.model import STATE_CHANGING, Effect, Resolution, Verb
from dmgate.effects.registry import Registry, Resource
from dmgate.effects.shell import resolve_shell

__all__ = ["STATE_CHANGING", "Effect", "Registry", "Resolution", "Resource", "Verb", "resolve_shell"]
