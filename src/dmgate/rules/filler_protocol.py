"""What the rule gate needs from a fact filler (implemented by `dmgate.llm.filler.FactFiller`).

A protocol keeps the rule engine independent of the LLM layer: rules never import a model client.
"""

from __future__ import annotations

from typing import Any, Protocol

from dmgate.facts.builder import ActionFacts, Fill
from dmgate.records import GateInput


class Filler(Protocol):
    log: list[dict[str, Any]]

    def answer_gaps(self, request: GateInput, facts: ActionFacts) -> dict[tuple[int, str], Fill]: ...

    def flag_risks(self, request: GateInput, facts: ActionFacts) -> dict[tuple[int, str], Fill]: ...

    def cite(self, request: GateInput, facts: ActionFacts, effects: list[int],
             reasons: dict[int, list[str]] | None = None) -> dict[tuple[int, str], Fill]: ...
