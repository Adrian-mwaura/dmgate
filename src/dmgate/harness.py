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
    if spec.startswith("jsonl:"):
        return list(load_jsonl(Path(spec.removeprefix("jsonl:"))))
    raise ValueError(f"unknown dataset {spec!r}; use ampermbench-probes or jsonl:<path>")


def make_gate(name: str, records: list[ActionRecord]) -> Gate:
    constants = {f"{o.name.lower()}-all": o for o in Outcome}
    if name in constants:
        return ConstantGate(constants[name])
    if name in ("oracle", "oracle-minimize"):
        return OracleGate({r.record_id: r.label for r in records}, minimize=name == "oracle-minimize")
    raise ValueError(f"unknown gate {name!r}; use one of {sorted(constants)} or oracle, oracle-minimize")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, help="ampermbench-probes or jsonl:<path>")
    parser.add_argument("--gate", required=True)
    parser.add_argument("--out", type=Path, default=None, help="output directory (default results/<timestamp>-<gate>)")
    parser.add_argument("--n-boot", type=int, default=2000)
    args = parser.parse_args(argv)

    records = load_dataset(args.dataset)
    gate = make_gate(args.gate, records)
    results, decisions = run(gate, records)
    summary = summarize(results, n_boot=args.n_boot)

    out = args.out or Path("results") / f"{datetime.now():%Y%m%d-%H%M%S}-{gate.name}"
    out.mkdir(parents=True, exist_ok=True)
    with (out / "results.jsonl").open("w", encoding="utf-8") as handle:
        for result, decision in zip(results, decisions):
            handle.write(json.dumps({**result.to_dict(), "decision": decision}, ensure_ascii=False) + "\n")
    meta = {"dataset": args.dataset, "gate": gate.name, "n_records": len(records)}
    (out / "summary.json").write_text(json.dumps({**meta, **summary}, indent=2, default=str), encoding="utf-8")

    print(f"dataset={args.dataset} gate={gate.name} -> {out}")
    print(format_summary(summary))


if __name__ == "__main__":
    main()
