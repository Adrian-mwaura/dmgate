"""Gate interface and reference gates for harness validation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from dmgate.outcomes import Decision, Outcome
from dmgate.records import GateInput, Label


class Gate(Protocol):
    name: str

    def decide(self, request: GateInput) -> Decision: ...


class ConstantGate:
    """Returns the same outcome for every action. Baseline only."""

    def __init__(self, outcome: Outcome):
        self.outcome = outcome
        self.name = f"{outcome.name.lower()}-all"

    def decide(self, request: GateInput) -> Decision:
        return Decision(self.outcome, reasons=("constant baseline",))


class OracleGate:
    """Decides from ground-truth labels. Validates the scoring path; never a real result.

    With `minimize=True`, harmful actions that still contain authorized targets are narrowed to
    those targets instead of denied, exercising residual scoring.
    """

    def __init__(self, labels: Mapping[str, Label], *, minimize: bool = False):
        self.labels = labels
        self.minimize = minimize
        self.name = "oracle-minimize" if minimize else "oracle"

    def decide(self, request: GateInput) -> Decision:
        label = self.labels[request.record_id]
        if not label.should_block:
            return Decision(Outcome.ALLOW, reasons=("oracle: authorized",))
        if self.minimize and label.targets is not None and label.authorized is not None:
            residual = label.targets & label.authorized
            if residual:
                return Decision(Outcome.MINIMIZE, residual_targets=residual, reasons=("oracle: narrowed",))
        return Decision(Outcome.DENY, reasons=("oracle: unauthorized",))
