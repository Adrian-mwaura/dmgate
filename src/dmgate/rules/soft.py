"""The soft-rule table (DESIGN.md §6.6, layer 2) and its geometry.

A row is a box in the discrete fact space: for each fact it names, a set of allowed values; facts
it does not name are unconstrained. An effect's soft outcome is the most restrictive row whose box
contains it, or Ask when none does. `policy/rego/decision.rego` implements the same semantics for
OPA; a differential test keeps the two in step.

Every fact has a finite domain and rows only test set membership, so the space splits into
finitely many *cells*: for each fact, values that every row treats alike are interchangeable, and
a cell picks one such class per fact. Every table's soft decision is constant on each cell, so
comparing two tables cell by cell is exact, not sampled. That is how a patch is classified (§6.6,
§6.10): it *loosens* wherever the new table is less restrictive than the old one.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dmgate.facts.schema import BY_NAME, FACTS, FactSpec
from dmgate.outcomes import Outcome

DEFAULT_TABLE = Path(__file__).resolve().parents[3] / "policy" / "soft_rules.json"
UNMATCHED = Outcome.ASK  # must equal `unmatched` in decision.rego

FactValue = str | bool


def domain(spec: FactSpec) -> tuple[FactValue, ...]:
    return (False, True) if spec.domain is None else spec.domain


@dataclass(frozen=True)
class SoftRow:
    row_id: str
    when: Mapping[str, frozenset[FactValue]]
    outcome: Outcome
    reason: str

    def contains(self, point: Mapping[str, FactValue]) -> bool:
        return all(point[fact] in allowed for fact, allowed in self.when.items())

    def to_data(self) -> dict[str, Any]:
        """The row as OPA data: values in schema order, outcome in lower case."""
        return {
            "id": self.row_id,
            "when": {f: [v for v in domain(BY_NAME[f]) if v in vs] for f, vs in self.when.items()},
            "outcome": self.outcome.name.lower(),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SoftTable:
    rows: tuple[SoftRow, ...]
    version: str = ""

    @classmethod
    def load(cls, path: Path = DEFAULT_TABLE) -> SoftTable:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SoftTable:
        """Validates every row against the fact schema. A row naming an unknown fact or value would
        silently never match in Rego, so it is an error here."""
        rows, seen = [], set()
        for raw in data["rows"]:
            row_id = raw["id"]
            if row_id in seen:
                raise ValueError(f"duplicate soft row id {row_id!r}")
            seen.add(row_id)
            when = {}
            for fact, values in raw["when"].items():
                if fact not in BY_NAME:
                    raise ValueError(f"{row_id}: unknown fact {fact!r}")
                if not values:
                    raise ValueError(f"{row_id}: {fact} allows no values, so the row can never match")
                bad = [v for v in values if not BY_NAME[fact].allows(v)]
                if bad:
                    raise ValueError(f"{row_id}: {fact} has values outside its domain: {bad}")
                when[fact] = frozenset(values)
            outcome = raw["outcome"]
            if outcome not in ("allow", "minimize", "ask", "deny"):
                raise ValueError(f"{row_id}: unknown outcome {outcome!r}")
            if not raw.get("reason"):
                raise ValueError(f"{row_id}: a row needs a reason (it is shown to the agent and the user)")
            rows.append(SoftRow(row_id, when, Outcome[outcome.upper()], raw["reason"]))
        return cls(tuple(rows), data.get("version", ""))

    def to_data(self) -> list[dict[str, Any]]:
        return [row.to_data() for row in self.rows]

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.to_data(), sort_keys=True).encode()).hexdigest()[:12]

    def matching(self, point: Mapping[str, FactValue]) -> tuple[SoftRow, ...]:
        return tuple(row for row in self.rows if row.contains(point))

    def decide(self, point: Mapping[str, FactValue]) -> Outcome:
        matched = self.matching(point)
        return max((row.outcome for row in matched), default=UNMATCHED)


# ---------------------------------------------------------------------------------------------
# Geometry

Cell = dict[str, tuple[FactValue, ...]]


def cells(*tables: SoftTable) -> Iterator[Cell]:
    """The cells of the partition the tables' rows induce. Every table decides each cell alike."""
    classes: dict[str, list[tuple[FactValue, ...]]] = {}
    for spec in FACTS:
        sets = [row.when[spec.name] for t in tables for row in t.rows if spec.name in row.when]
        groups: dict[tuple[bool, ...], list[FactValue]] = {}
        for value in domain(spec):
            groups.setdefault(tuple(value in s for s in sets), []).append(value)
        classes[spec.name] = [tuple(g) for g in groups.values()]
    for combo in itertools.product(*classes.values()):
        yield dict(zip(classes, combo))


def representative(cell: Cell) -> dict[str, FactValue]:
    return {fact: values[0] for fact, values in cell.items()}


def describe(cell: Cell) -> str:
    """The cell's constrained facts, e.g. "op in {delete, control}, targets_in_contract = none"."""
    parts = []
    for fact, values in cell.items():
        if len(values) == len(domain(BY_NAME[fact])):
            continue
        shown = [str(v).lower() if isinstance(v, bool) else v for v in values]
        parts.append(f"{fact} = {shown[0]}" if len(shown) == 1 else f"{fact} in {{{', '.join(shown)}}}")
    return ", ".join(parts) or "everywhere"


@dataclass(frozen=True)
class Difference:
    cell: Cell
    old: Outcome
    new: Outcome

    @property
    def points(self) -> int:
        """How many fact records the cell holds."""
        return math.prod(len(values) for values in self.cell.values())

    @property
    def loosens(self) -> bool:
        return self.new < self.old


def compare(old: SoftTable, new: SoftTable) -> list[Difference]:
    """Every cell where the two tables decide differently: exact, by enumeration of cells."""
    out = []
    for cell in cells(old, new):
        point = representative(cell)
        a, b = old.decide(point), new.decide(point)
        if a is not b:
            out.append(Difference(cell, a, b))
    return out


def loosening(old: SoftTable, new: SoftTable) -> list[Difference]:
    """Where a patch makes the soft layer less restrictive: the part the promotion gate must check."""
    return [d for d in compare(old, new) if d.loosens]
