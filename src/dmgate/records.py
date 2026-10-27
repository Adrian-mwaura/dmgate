"""Labeled action records: the unit the replay harness scores (DESIGN.md §8)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Trust(str, Enum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


@dataclass(frozen=True)
class Segment:
    """One piece of context the agent saw. `role` is user, system, tool or agent."""

    role: str
    text: str
    trust: Trust


@dataclass(frozen=True)
class Action:
    """The tool call the agent wants to make, e.g. tool="bash", args={"command": ...}."""

    tool: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Label:
    """Ground truth for one action. Positive means the action should be blocked.

    `targets` are the resources the action really affects and `authorized` the resources the
    user's request authorizes; both are needed to rescore a Minimize on its residual. `universe` is
    every target the oracle has an opinion on; a residual is judged only within it (a listing or a
    git remote outside it is not the label's business).
    """

    should_block: bool
    targets: frozenset[str] | None = None
    authorized: frozenset[str] | None = None
    nontrivial: bool = True
    source: str = ""
    universe: frozenset[str] | None = None

    @classmethod
    def from_targets(
        cls,
        targets: Iterable[str],
        authorized: Iterable[str],
        *,
        nontrivial: bool = True,
        source: str = "",
        universe: Iterable[str] | None = None,
    ) -> Label:
        targets = frozenset(targets)
        authorized = frozenset(authorized)
        return cls(
            should_block=not targets <= authorized,
            targets=targets,
            authorized=authorized,
            nontrivial=nontrivial,
            source=source,
            universe=frozenset(universe) if universe is not None else None,
        )


@dataclass(frozen=True)
class GateInput:
    """Everything a gate may see. Deliberately excludes the label."""

    record_id: str
    context: tuple[Segment, ...]
    action: Action
    environment: dict[str, Any]


@dataclass(frozen=True)
class ActionRecord:
    record_id: str
    dataset: str
    family: str
    cluster_id: str  # prompt or session id; actions sharing it are correlated
    intent_key: str  # retries of the same intent share this
    context: tuple[Segment, ...]
    action: Action
    label: Label
    axes: dict[str, str] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)

    def gate_input(self) -> GateInput:
        return GateInput(self.record_id, self.context, self.action, self.environment)

    def to_dict(self) -> dict[str, Any]:
        label = self.label
        return {
            "record_id": self.record_id,
            "dataset": self.dataset,
            "family": self.family,
            "cluster_id": self.cluster_id,
            "intent_key": self.intent_key,
            "context": [{"role": s.role, "text": s.text, "trust": s.trust.value} for s in self.context],
            "action": {"tool": self.action.tool, "args": self.action.args},
            "label": {
                "should_block": label.should_block,
                "targets": sorted(label.targets) if label.targets is not None else None,
                "authorized": sorted(label.authorized) if label.authorized is not None else None,
                "nontrivial": label.nontrivial,
                "source": label.source,
                "universe": sorted(label.universe) if label.universe is not None else None,
            },
            "axes": self.axes,
            "environment": self.environment,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActionRecord:
        label = data["label"]
        return cls(
            record_id=data["record_id"],
            dataset=data["dataset"],
            family=data["family"],
            cluster_id=data["cluster_id"],
            intent_key=data["intent_key"],
            context=tuple(Segment(s["role"], s["text"], Trust(s["trust"])) for s in data["context"]),
            action=Action(data["action"]["tool"], data["action"]["args"]),
            label=Label(
                should_block=label["should_block"],
                targets=frozenset(label["targets"]) if label.get("targets") is not None else None,
                authorized=frozenset(label["authorized"]) if label.get("authorized") is not None else None,
                nontrivial=label.get("nontrivial", True),
                source=label.get("source", ""),
                universe=frozenset(label["universe"]) if label.get("universe") is not None else None,
            ),
            axes=data.get("axes", {}),
            environment=data.get("environment", {}),
        )


def dump_jsonl(records: Iterable[ActionRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")


def load_jsonl(path: Path) -> Iterator[ActionRecord]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield ActionRecord.from_dict(json.loads(line))
