"""Effects: what an action does to which resources (DESIGN.md §5.2)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Verb(str, Enum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    CONTROL = "control"  # lifecycle of a live resource: cancel a job, restart a service
    EGRESS = "egress"
    EXEC = "exec"


STATE_CHANGING = frozenset({Verb.WRITE, Verb.DELETE, Verb.CONTROL})


@dataclass(frozen=True)
class Effect:
    """One effect of an action.

    `targets` are resolved ids (registry ids, normalized paths, hosts). `unresolved` holds the raw
    selectors we could not resolve; an effect with any of them is never exact. `opaque` means we
    cannot see what the program does at all.
    """

    verb: Verb
    kind: str  # file, branch, job, service, object, host, process, repo, database
    targets: frozenset[str] = frozenset()
    unresolved: tuple[str, ...] = ()
    exact: bool = True
    opaque: bool = False
    command: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.unresolved or self.opaque:
            object.__setattr__(self, "exact", False)


@dataclass(frozen=True)
class Resolution:
    effects: tuple[Effect, ...]
    flows: tuple[tuple[int, int], ...] = ()  # (from, to) effect indices: data passed through a pipe or redirect
    parse_error: bool = False

    @property
    def opaque(self) -> bool:
        return any(e.opaque for e in self.effects)

    @property
    def exact(self) -> bool:
        return not self.parse_error and all(e.exact for e in self.effects)

    def changed(self, kind: str | None = None) -> frozenset[str]:
        """Union of targets of state-changing effects, optionally for one resource kind."""
        out: set[str] = set()
        for e in self.effects:
            if e.verb in STATE_CHANGING and (kind is None or e.kind == kind):
                out |= e.targets
        return frozenset(out)
