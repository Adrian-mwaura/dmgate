"""Offline replay harness: run a gate over labeled action records and score it."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from dmgate.gate import ConstantGate, Gate, OracleGate
from dmgate.outcomes import Outcome
from dmgate.records import ActionRecord, load_jsonl
from dmgate.report import Result, format_summary, summarize
from dmgate.scoring import score


def run(gate: Gate, records: Iterable[ActionRecord]) -> tuple[list[Result], list[dict]]:
    """Returns scored results and the raw decisions (for traces)."""
    results, decisions = [], []
    for record in records:
        start = time.perf_counter()
        decision = gate.decide(record.gate_input())
        latency_ms = (time.perf_counter() - start) * 1000.0
        results.append(
            Result(
                record_id=record.record_id,
                family=record.family,
                cluster_id=record.cluster_id,
                intent_key=record.intent_key,
                should_block=record.label.should_block,
                nontrivial=record.label.nontrivial,
                outcome=decision.outcome,
                case=score(record.label, decision),
                case_ask_as_allow=score(record.label, decision, ask_blocks=False),
                latency_ms=latency_ms,
            )
        )
        decisions.append({"record_id": record.record_id, **decision.to_dict()})
    return results, decisions


def load_dataset(spec: str) -> list[ActionRecord]:
    if spec == "ampermbench-probes":
        from dmgate.datasets.ampermbench import probe_records

        return probe_records()
    if spec == "fsdb-dev-probes":
        from dmgate.datasets.fsdb import probe_records

        return probe_records()
    if spec.startswith("jsonl:"):
        return list(load_jsonl(Path(spec.removeprefix("jsonl:"))))
    raise ValueError(f"unknown dataset {spec!r}; use ampermbench-probes, fsdb-dev-probes or jsonl:<path>")


RULE_GATES = ("rules-profile-only", "rules-oracle-contract", "rules-llm")


def make_gate(name: str, records: list[ActionRecord]) -> Gate:
    constants = {f"{o.name.lower()}-all": o for o in Outcome}
    if name in constants:
        return ConstantGate(constants[name])
    if name in ("oracle", "oracle-minimize"):
        return OracleGate({r.record_id: r.label for r in records}, minimize=name == "oracle-minimize")
    if name in RULE_GATES:
        return _rule_gate(name, records)
    raise ValueError(f"unknown gate {name!r}; use one of {sorted(constants)}, oracle, oracle-minimize, "
                     f"or {', '.join(RULE_GATES)}")


def _rule_gate(name: str, records: list[ActionRecord]) -> Gate:
    """The M3 rule gate with a stand-in contract until the M5 compiler exists.

    rules-profile-only: the dataset's admin profile alone, no request grants (a lower bound).
    rules-oracle-contract: the profile plus the oracle's authorized set (an upper bound that isolates
    the rules from compiler errors; built from ground truth, so never reported as a result).
    rules-llm: the full pipeline: the M5 compiler and fact filler on the model at DMGATE_LLM_BASE_URL
    (DMGATE_LLM_MODEL; optional DMGATE_LLM_API_KEY, DMGATE_LLM_EXTRA_BODY), answers cached on disk.
    """
    from dmgate.datasets import ampermbench, fsdb
    from dmgate.rules.gate import RuleGate

    adapters = {ampermbench.DATASET: ampermbench, fsdb.DATASET: fsdb}
    datasets = {r.dataset for r in records}
    if len(datasets) != 1 or not datasets <= set(adapters):
        raise ValueError(f"{name} needs records from one of {sorted(adapters)}: it uses the dataset's admin profile")
    adapter = adapters[datasets.pop()]
    if name == "rules-profile-only":
        profile = adapter.profile_contract()
        return RuleGate(lambda request: profile, name=name, note="lower bound: no request grants")
    if name == "rules-llm":
        return _llm_gate(adapter.profile_contract())
    contracts = adapter.oracle_contracts(records)
    return RuleGate(lambda request: contracts[request.record_id], name=name,
                    note="upper bound: the contract comes from ground truth; not a result")


def _llm_gate(profile) -> Gate:
    import os

    from dmgate.llm.compiler import ContractCompiler
    from dmgate.llm.evaluate import client_from_args, default_cache
    from dmgate.llm.filler import FactFiller
    from dmgate.rules.gate import RuleGate

    base_url, model = os.environ.get("DMGATE_LLM_BASE_URL"), os.environ.get("DMGATE_LLM_MODEL")
    if not base_url or not model:
        raise ValueError("rules-llm needs DMGATE_LLM_BASE_URL and DMGATE_LLM_MODEL (see docs/DEPLOY.md)")
    extra = os.environ.get("DMGATE_LLM_EXTRA_BODY")
    client = client_from_args(base_url, model, api_key=os.environ.get("DMGATE_LLM_API_KEY"),
                              structured=os.environ.get("DMGATE_LLM_STRUCTURED", "response_format"),
                              extra_body=json.loads(extra) if extra else None, cache=default_cache(model))
    return RuleGate(ContractCompiler(client, profile), name=f"rules-llm[{model}]", filler=FactFiller(client),
                    note="full pipeline: compiled contract and filled facts")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, help="ampermbench-probes or jsonl:<path>")
    parser.add_argument("--gate", required=True)
    parser.add_argument("--out", type=Path, default=None, help="output directory (default results/<timestamp>-<gate>)")
    parser.add_argument("--n-boot", type=int, default=2000)
    args = parser.parse_args(argv)

    records = load_dataset(args.dataset)
    gate = make_gate(args.gate, records)
    try:
        results, decisions = run(gate, records)
    finally:
        close = getattr(gate, "close", None)
        if close is not None:
            close()
    summary = summarize(results, n_boot=args.n_boot)

    out = args.out or Path("results") / f"{datetime.now():%Y%m%d-%H%M%S}-{gate.name}"
    out.mkdir(parents=True, exist_ok=True)
    with (out / "results.jsonl").open("w", encoding="utf-8") as handle:
        for result, decision in zip(results, decisions):
            handle.write(json.dumps({**result.to_dict(), "decision": decision}, ensure_ascii=False) + "\n")
    meta = {"dataset": args.dataset, "gate": gate.name, "note": getattr(gate, "note", ""), "n_records": len(records)}
    (out / "summary.json").write_text(json.dumps({**meta, **summary}, indent=2, default=str), encoding="utf-8")

    print(f"dataset={args.dataset} gate={gate.name} -> {out}")
    if meta["note"]:
        print(f"NOTE: {meta['note']}")
    print(format_summary(summary))


if __name__ == "__main__":
    main()
