"""The session ledger (DESIGN.md §6.4): what a session has already done, for facts that span actions.

Today it records reads of sensitive data, for the `session_chain` fact. Handles (§6.7) and the
strings each tool output introduced will join it in later milestones.

Record an action only once it has actually executed (it was allowed or minimized). A blocked
action read nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from dmgate.facts.schema import SENSITIVITY

if TYPE_CHECKING:
    from dmgate.facts.builder import ActionFacts

SENSITIVE = frozenset({"S2", "S3"})


@dataclass(frozen=True)
class LedgerEntry:
    record_id: str
    target: str
    level: str


@dataclass
class SessionLedger:
    sensitive_reads: list[LedgerEntry] = field(default_factory=list)

    def record(self, facts: ActionFacts) -> None:
        for e in facts.effects:
            level = e.facts["max_sensitivity"].value
            if e.facts["op"].value == "read" and level in SENSITIVE:
                for target in sorted(e.effect.targets) or ["<unnamed>"]:
                    self.sensitive_reads.append(LedgerEntry(facts.record_id, target, level))

    def highest_read(self) -> LedgerEntry | None:
        if not self.sensitive_reads:
            return None
        return max(self.sensitive_reads, key=lambda entry: SENSITIVITY.index(entry.level))
