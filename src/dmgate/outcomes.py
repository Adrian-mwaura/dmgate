"""Gate outcomes and decisions (DESIGN.md §3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Outcome(IntEnum):
    """Ordered lattice: a larger value is more restrictive."""

    ALLOW = 0
    MINIMIZE = 1
    ASK = 2
    DENY = 3


@dataclass(frozen=True)
class Decision:
    """What the gate decided for one action.

    `residual_targets` is what a Minimize still executes. It may be None for read-side
    minimization, in which case offline scoring treats the Minimize as unverifiable.
    """

    outcome: Outcome
    residual_targets: frozenset[str] | None = None
    reasons: tuple[str, ...] = ()
    trace: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.name,
            "residual_targets": sorted(self.residual_targets) if self.residual_targets is not None else None,
            "reasons": list(self.reasons),
            "trace": self.trace,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Decision:
        residual = data.get("residual_targets")
        return cls(
            outcome=Outcome[data["outcome"]],
            residual_targets=frozenset(residual) if residual is not None else None,
            reasons=tuple(data.get("reasons", ())),
            trace=data.get("trace", {}),
        )
