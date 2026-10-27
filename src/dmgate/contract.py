"""Intent contracts (DESIGN.md §6.1, position 3): what the user's request authorizes.

A contract has two layers.

- The **envelope** is a set of grants, each a kind of permission: read up to a sensitivity ceiling,
  delete, control, egress to some sink classes, and so on. Only trusted sources widen it: the admin's
  default profile, a user turn, or an answered Ask.
- The **targets** say which resources, files and hosts the request is about: by id, by path
  pattern, or by a selector evaluated against the registry. Targets that come from observations
  (a listing, a stack trace) are not in the contract. The `provenance` fact carries those.

Grant lifetimes: read, write and exec grants last the session. Delete, control and egress grants
given by a user turn or an answered Ask last only that turn. Local writes are session-long because
the transactional sandbox makes them reversible (§6.3).

The M5 compiler builds contracts from trusted context only. Until then they are written by hand in
tests, or derived from a benchmark oracle for evaluation, which is never given to a gate.
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from fnmatch import fnmatchcase
from typing import Any

from dmgate.effects import Resource, Verb
from dmgate.facts.schema import SENSITIVITY, SINKS


class Lifetime(str, Enum):
    SESSION = "session"
    TURN = "turn"


class GrantSource(str, Enum):
    DEFAULT_PROFILE = "default_profile"
    USER_TURN = "user_turn"
    ANSWERED_ASK = "answered_ask"


class TargetSource(str, Enum):
    USER = "user"
    TRUSTED_STRUCTURE = "trusted_structure"  # imports, foreign keys: structure the platform trusts
    DELEGATED = "delegated"  # a document the user pointed to ("do what TICKET-12 says")


ONE_TURN_VERBS = frozenset({Verb.DELETE, Verb.CONTROL, Verb.EGRESS})


@dataclass(frozen=True)
class Grant:
    """One envelope permission. `kinds` empty means any kind; `sinks` empty means any sink.

    The sensitivity ceiling applies to verbs that move data (read, write, egress). `sinks` applies
    to egress.
    """

    verb: Verb
    kinds: frozenset[str] = frozenset()
    max_sensitivity: str = "S1"
    sinks: frozenset[str] = frozenset()
    source: GrantSource = GrantSource.USER_TURN
    turn: int = 0
    lifetime: Lifetime | None = None  # None: the default for the verb and source
    grant_id: str = ""

    def __post_init__(self) -> None:
        if self.max_sensitivity not in SENSITIVITY:
            raise ValueError(f"unknown sensitivity {self.max_sensitivity!r}")
        if not self.sinks <= set(SINKS):
            raise ValueError(f"unknown sink classes {sorted(self.sinks - set(SINKS))}")
        one_turn = self.verb in ONE_TURN_VERBS and self.source is not GrantSource.DEFAULT_PROFILE
        if self.lifetime is None:
            object.__setattr__(self, "lifetime", Lifetime.TURN if one_turn else Lifetime.SESSION)
        elif one_turn and self.lifetime is Lifetime.SESSION:
            raise ValueError(f"a {self.verb.value} grant from a {self.source.value} lasts one turn only")

    def to_dict(self) -> dict[str, Any]:
        return {
            "verb": self.verb.value,
            "kinds": sorted(self.kinds),
            "max_sensitivity": self.max_sensitivity,
            "sinks": sorted(self.sinks),
            "source": self.source.value,
            "turn": self.turn,
            "lifetime": self.lifetime.value,
            "grant_id": self.grant_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Grant:
        return cls(
            verb=Verb(data["verb"]),
            kinds=frozenset(data.get("kinds", ())),
            max_sensitivity=data.get("max_sensitivity", "S1"),
            sinks=frozenset(data.get("sinks", ())),
            source=GrantSource(data.get("source", GrantSource.USER_TURN.value)),
            turn=data.get("turn", 0),
            lifetime=Lifetime(data["lifetime"]) if data.get("lifetime") else None,
            grant_id=data.get("grant_id", ""),
        )


_MISSING = object()
CONDITION_OPS = ("eq", "in", "contains", "prefix", "lt", "le", "gt", "ge")


@dataclass(frozen=True)
class Condition:
    """One test on a resource: `attr` eq / in / contains / prefix / lt / le / gt / ge `value`.

    The attr "id" is the resource id; a dotted attr reads nested values ("values.owner" is the owner
    column of a database row). Comparing values of different types is simply false.
    """

    attr: str
    op: str
    value: Any

    def __post_init__(self) -> None:
        if self.op not in CONDITION_OPS:
            raise ValueError(f"unknown condition operator {self.op!r}")
        if self.op == "in":
            object.__setattr__(self, "value", tuple(self.value))

    def holds(self, rid: str, attrs: Mapping[str, Any]) -> bool:
        actual = rid if self.attr == "id" else _lookup(attrs, self.attr)
        if actual is _MISSING:
            return False
        if self.op == "eq":
            return actual == self.value
        if self.op == "in":
            return actual in self.value
        if self.op == "contains":
            return isinstance(actual, str) and str(self.value) in actual
        if self.op == "prefix":
            return isinstance(actual, str) and actual.startswith(str(self.value))
        try:
            return {"lt": actual < self.value, "le": actual <= self.value,
                    "gt": actual > self.value, "ge": actual >= self.value}[self.op]
        except TypeError:
            return False

    def to_dict(self) -> dict[str, Any]:
        return {"attr": self.attr, "op": self.op, "value": list(self.value) if self.op == "in" else self.value}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Condition:
        return cls(data["attr"], data["op"], data["value"])


def _lookup(attrs: Mapping[str, Any], path: str) -> Any:
    value: Any = attrs
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return _MISSING
        value = value[part]
    return value


@dataclass(frozen=True)
class TargetSpec:
    """Which targets of one kind the request is about.

    A target matches if its id is in `ids`, or it matches one of `patterns` (fnmatch), or every
    condition in `where` holds for it. For files, relative ids and patterns are taken from the cwd.
    `expected_count` is how many targets the request implies ("my stale branch" is 1), if it says.
    """

    kind: str
    ids: frozenset[str] = frozenset()
    patterns: tuple[str, ...] = ()
    where: tuple[Condition, ...] = ()
    source: TargetSource = TargetSource.USER
    expected_count: int | None = None

    def __post_init__(self) -> None:
        if not (self.ids or self.patterns or self.where):
            raise ValueError("a target spec needs ids, patterns or conditions")
        if self.expected_count is not None and self.expected_count < 1:
            raise ValueError("expected_count must be at least 1")

    def matches(self, target: str, resource: Resource | None = None, cwd: str | None = None) -> bool:
        ids, patterns = self.ids, self.patterns
        if self.kind == "file" and cwd:
            ids = frozenset(_absolute(i, cwd) for i in ids)
            patterns = tuple(_absolute(p, cwd) for p in patterns)
        if target in ids or any(fnmatchcase(target, p) for p in patterns):
            return True
        attrs = resource.attrs if resource is not None else {}
        return bool(self.where) and all(c.holds(target, attrs) for c in self.where)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ids": sorted(self.ids),
            "patterns": list(self.patterns),
            "where": [c.to_dict() for c in self.where],
            "source": self.source.value,
            "expected_count": self.expected_count,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TargetSpec:
        return cls(
            kind=data["kind"],
            ids=frozenset(data.get("ids", ())),
            patterns=tuple(data.get("patterns", ())),
            where=tuple(Condition.from_dict(c) for c in data.get("where", ())),
            source=TargetSource(data.get("source", TargetSource.USER.value)),
            expected_count=data.get("expected_count"),
        )


def _absolute(path: str, cwd: str) -> str:
    return path if path.startswith("/") else posixpath.normpath(posixpath.join(cwd, path))


@dataclass(frozen=True)
class Contract:
    """The envelope and targets in force at user turn `turn`.

    `egress_allowlist` holds the admin profile's host patterns (fnmatch, e.g. "*.pythonhosted.org").
    `delegated_segments` are context indices of untrusted documents the user pointed to.
    """

    turn: int = 0
    grants: tuple[Grant, ...] = ()
    targets: tuple[TargetSpec, ...] = ()
    egress_allowlist: frozenset[str] = frozenset()
    delegated_segments: frozenset[int] = field(default_factory=frozenset)

    def is_active(self, grant: Grant) -> bool:
        return grant.lifetime is Lifetime.SESSION or grant.turn == self.turn

    def active_grants(self) -> tuple[Grant, ...]:
        return tuple(g for g in self.grants if self.is_active(g))

    def covering_spec(self, kind: str, target: str, resource: Resource | None = None,
                      cwd: str | None = None) -> TargetSpec | None:
        return next((s for s in self.targets if s.kind == kind and s.matches(target, resource, cwd)), None)

    def expected_count(self, kind: str) -> int | None:
        """Total targets of this kind the request implies; None if any spec of the kind does not say."""
        specs = [s for s in self.targets if s.kind == kind]
        if not specs or any(s.expected_count is None for s in specs):
            return None
        return sum(s.expected_count for s in specs)

    def allowlisted(self, host: str) -> bool:
        return any(fnmatchcase(host, pattern) for pattern in self.egress_allowlist)

    def advance(self, turn: int, *, carry_forward: bool = False) -> Contract:
        """The contract at a later user turn. One-turn grants expire, unless the new turn is a
        "continue"-style turn (`carry_forward`), which carries the previous turn's grants forward."""
        if turn <= self.turn:
            raise ValueError("a contract only advances to a later turn")
        grants = []
        for g in self.grants:
            if g.lifetime is Lifetime.SESSION:
                grants.append(g)
            elif carry_forward and g.turn == self.turn:
                grants.append(replace(g, turn=turn))
        return replace(self, turn=turn, grants=tuple(grants))

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "grants": [g.to_dict() for g in self.grants],
            "targets": [t.to_dict() for t in self.targets],
            "egress_allowlist": sorted(self.egress_allowlist),
            "delegated_segments": sorted(self.delegated_segments),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Contract:
        return cls(
            turn=data.get("turn", 0),
            grants=tuple(Grant.from_dict(g) for g in data.get("grants", ())),
            targets=tuple(TargetSpec.from_dict(t) for t in data.get("targets", ())),
            egress_allowlist=frozenset(data.get("egress_allowlist", ())),
            delegated_segments=frozenset(data.get("delegated_segments", ())),
        )
