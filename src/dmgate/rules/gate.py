"""The rule gate: action -> effects (M1) -> facts (M2) -> LLM fact filler (M5) -> OPA policy (M3).

The contract comes from a provider: the M5 compiler (`dmgate.llm.compiler.ContractCompiler`), the
admin profile alone (no request grants: a lower bound), or the benchmark oracle's contract (an upper
bound that isolates the rules from compiler errors; never a result).

With a filler, one decision runs: build facts; answer the open facts and rebuild; decide; for
effects the soft layer holds back (outcome above the floor), ask for a citation, and if one
verifies, rebuild and decide again. A citation can only lower the soft layer (decision.rego).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from dmgate.contract import Contract
from dmgate.effects import Effect, Resolution, Verb, resolve_shell, resolve_sql
from dmgate.facts.builder import ActionFacts, build_facts
from dmgate.outcomes import Decision, Outcome
from dmgate.records import GateInput
from dmgate.rules.filler_protocol import Filler
from dmgate.rules.opa import POLICY_DIR, OpaServer
from dmgate.rules.soft import SoftTable

ContractProvider = Callable[[GateInput], Contract]

SHELL_TOOLS = frozenset({"bash", "execute_bash", "shell", "terminal"})
SQL_TOOLS = frozenset({"sql", "execute_sql", "query_database"})


def resolve_action(request: GateInput) -> Resolution:
    """Effects of any tool call. Tools without an effect spec yet are opaque (§6.2)."""
    action = request.action
    if action.tool in SHELL_TOOLS:
        return resolve_shell(str(action.args.get("command", "")), request.environment)
    if action.tool in SQL_TOOLS:
        return resolve_sql(str(action.args.get("query", "")), request.environment, action.args.get("database"))
    return Resolution((Effect(Verb.EXEC, "process", frozenset({action.tool}), opaque=True, command=action.tool,
                              note="no effect spec for this tool yet"),))


def policy_version(table: SoftTable) -> str:
    digest = hashlib.sha256()
    for path in sorted(POLICY_DIR.glob("*.rego")):
        if not path.name.endswith("_test.rego"):
            digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return f"{digest.hexdigest()[:12]}+{table.fingerprint()}"


def residual(facts: ActionFacts, effects: list[dict[str, Any]]) -> frozenset[str] | None:
    """What still executes after a Minimize: the targets of allowed effects plus the covered targets
    of minimized ones. None when that cannot be stated (an allowed effect with unresolved or opaque
    parts), which scores as a failed Minimize (position 1). Scoring looks only at the targets a
    label knows about (`Label.universe`)."""
    keep: set[str] = set()
    for record, decided in zip(facts.effects, effects):
        e = record.effect
        if decided["outcome"] == "minimize":
            keep |= record.covered
        elif decided["outcome"] == "allow":
            if e.unresolved or e.opaque:
                return None
            keep |= e.targets
    return frozenset(keep)


RANK = {"allow": 0, "minimize": 1, "ask": 2, "deny": 3}


class RuleGate:
    def __init__(self, contracts: ContractProvider, *, name: str = "rules", note: str = "",
                 table: SoftTable | None = None, server: OpaServer | None = None, sandbox: bool = False,
                 residual_risk_live: bool = False, filler: Filler | None = None):
        self.contracts = contracts
        self.filler = filler
        self.name = name
        self.note = note
        self.table = table or SoftTable.load()
        self.config = {"sandbox": sandbox, "residual_risk_live": residual_risk_live}
        self.version = policy_version(self.table)
        self._server = server
        self._owns_server = server is None
        self._loaded = False

    def _opa(self) -> OpaServer:
        if self._server is None:
            self._server = OpaServer().start()
        if not self._loaded:
            self._server.put_data("dmgate/soft", {"rows": self.table.to_data(), "version": self.table.version})
            self._server.put_data("dmgate/config", self.config)
            self._loaded = True
        return self._server

    def decide(self, request: GateInput) -> Decision:
        contract = self.contracts(request)
        resolution = resolve_action(request)
        facts = build_facts(request, resolution, contract)
        llm_log_start = len(self.filler.log) if self.filler is not None else 0
        fills = {}
        if self.filler is not None:
            fills = {**self.filler.answer_gaps(request, facts)}
            if fills:
                facts = build_facts(request, resolution, contract, fills=fills)
            risks = self.filler.flag_risks(request, facts)
            if risks:
                fills |= risks
                facts = build_facts(request, resolution, contract, fills=fills)
        result = self._opa().query("dmgate/decision/result", facts.to_input())
        if self.filler is not None:
            held = [d["effect"] for d in result["effects"] if RANK[d["outcome"]] > RANK[d["floor"]]]
            reasons_by_effect = {d["effect"]: d["reasons"] for d in result["effects"]}
            citations = self.filler.cite(request, facts, held, reasons_by_effect) if held else {}
            if citations:
                fills |= citations
                facts = build_facts(request, resolution, contract, fills=fills)
                result = self._opa().query("dmgate/decision/result", facts.to_input())
        outcome = Outcome[result["outcome"].upper()]
        deciding = [d for d in result["effects"] if d["outcome"] == result["outcome"]]
        reasons = tuple(dict.fromkeys(r for d in deciding for r in d["reasons"]))
        trace: dict[str, Any] = {"policy": self.version, "contract": contract.to_dict(), "facts": facts.trace(),
                                 "rules": result["effects"]}
        explain = getattr(self.contracts, "explain", None)
        if explain is not None:
            trace["compiler"] = explain(request)
        if self.filler is not None:
            trace["filler"] = self.filler.log[llm_log_start:]
        return Decision(
            outcome,
            residual_targets=residual(facts, result["effects"]) if outcome is Outcome.MINIMIZE else None,
            reasons=reasons or ("no effects",),
            trace=trace,
        )

    def close(self) -> None:
        if self._server is not None and self._owns_server:
            self._server.close()
            self._server = None
            self._loaded = False
