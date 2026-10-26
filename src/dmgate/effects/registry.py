"""The platform's resource registry: what exists and its attributes (owner, namespace, protected...).

Built from `GateInput.environment["resources"]`, a list of {"id", "kind", "attrs"}. Attribute
names are generic: owner, namespace, partition, bucket, key, name, status, protected, stale.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Resource:
    id: str
    kind: str
    attrs: Mapping[str, Any] = field(default_factory=dict)


class Registry:
    def __init__(self, resources: Iterable[Resource] = ()):
        self._by_id: dict[tuple[str, str], Resource] = {}
        self._by_kind: dict[str, list[Resource]] = {}
        for r in resources:
            self._by_id[(r.kind, r.id)] = r
            self._by_kind.setdefault(r.kind, []).append(r)

    @classmethod
    def from_environment(cls, environment: Mapping[str, Any]) -> Registry:
        return cls(Resource(r["id"], r["kind"], r.get("attrs", {})) for r in environment.get("resources", ()))

    def knows(self, kind: str) -> bool:
        """Whether the registry describes this kind at all (if not, names pass through unchecked)."""
        return kind in self._by_kind

    def get(self, kind: str, rid: str) -> Resource | None:
        return self._by_id.get((kind, rid))

    def of_kind(self, kind: str) -> list[Resource]:
        return list(self._by_kind.get(kind, ()))

    def select(self, kind: str, predicate: Callable[[Resource], bool]) -> frozenset[str]:
        return frozenset(r.id for r in self._by_kind.get(kind, ()) if predicate(r))
