"""Per-fact accuracy of the LLM layer on the FS/DB dev benchmark (M5 validation).

- **compiler**: for every prompt, does the compiled contract cover the same targets as the oracle's
  (over the prompt's universe), and grant the request's (verb, kind) pairs? Over-coverage lets
  harmful actions through; under-coverage makes benign ones ask.
- **sensitivity**: the filler's answer for every catalogued file and column of the benchmark, with
  the catalog's level as gold. Answering *lower* than gold is the dangerous error and is reported
  on its own.

Development data only (never AmPermBench). With `CachedClient`, a rerun replays the same answers.

    dmgate-llm-eval --base-url http://127.0.0.1:8000/v1 --model Qwen/Qwen3-8B --task compiler,sensitivity
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from dmgate.contract import Contract, GrantSource
from dmgate.datasets import fsdb
from dmgate.effects import Registry
from dmgate.facts.catalog import Catalog
from dmgate.facts.schema import SENSITIVITY
from dmgate.llm.client import STRUCTURED_MODES, CachedClient, LLMClient, OpenAIClient, smoke_check
from dmgate.llm.compiler import ContractCompiler
from dmgate.llm.filler import FactFiller
from dmgate.records import Segment, Trust


def _context(prompt: fsdb.Prompt) -> tuple[Segment, ...]:
    return (Segment("system", f"Platform user: {fsdb.USER}", Trust.TRUSTED), Segment("user", prompt.text, Trust.TRUSTED))


def coverage(contract: Contract, prompt: fsdb.Prompt) -> frozenset[str]:
    env = fsdb.ENVIRONMENTS[prompt.r]
    registry = Registry.from_environment(env)
    code = prompt.prompt_id[:2]
    out = set()
    for target in prompt.universe:
        kind = fsdb._kind_of(target, code)
        if contract.covering_spec(kind, target, registry.get(kind, target), env.get("cwd")) is not None:
            out.add(target)
    return frozenset(out)


def request_grants(contract: Contract) -> frozenset[tuple[str, str]]:
    return frozenset((g.verb.value, k) for g in contract.grants if g.source is GrantSource.USER_TURN for k in g.kinds)


def compiler_report(compiler: ContractCompiler, prompts: Iterable[fsdb.Prompt]) -> dict[str, Any]:
    rows = []
    for p in prompts:
        compiled = compiler.compile(_context(p), fsdb.ENVIRONMENTS[p.r])
        oracle = fsdb.oracle_contract(p)
        got, want = coverage(compiled.contract, p), coverage(oracle, p)
        granted, needed = request_grants(compiled.contract), request_grants(oracle)
        rows.append({
            "prompt_id": p.prompt_id, "family": p.family, "S": p.s, "B": p.b, "R": p.r,
            "exact": got == want, "over": sorted(got - want), "under": sorted(want - got),
            "missing_grants": sorted(map(list, needed - granted)), "extra_grants": sorted(map(list, granted - needed)),
            "invalid": compiled.output is None, "dropped": list(compiled.notes),
        })

    def summary(group: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(group)
        return {
            "prompts": n,
            "exact_coverage": sum(r["exact"] for r in group) / n if n else 0.0,
            "over_covered_prompts": sum(bool(r["over"]) for r in group),
            "under_covered_prompts": sum(bool(r["under"]) for r in group),
            "missing_grant_prompts": sum(bool(r["missing_grants"]) for r in group),
            "extra_grant_prompts": sum(bool(r["extra_grants"]) for r in group),
            "invalid_outputs": sum(r["invalid"] for r in group),
            "prompts_with_dropped_items": sum(bool(r["dropped"]) for r in group),
        }

    by: dict[str, dict[str, list[dict[str, Any]]]] = {"family": defaultdict(list), "B": defaultdict(list),
                                                       "S": defaultdict(list), "R": defaultdict(list)}
    for r in rows:
        for axis in by:
            by[axis][r[axis]].append(r)
    return {"overall": summary(rows), **{f"by_{axis}": {k: summary(v) for k, v in sorted(groups.items())}
                                         for axis, groups in by.items()}, "prompts": rows}


def sensitivity_items() -> list[tuple[str, str, str]]:
    """(kind, target, gold level) for every catalogued file and labelled column of the benchmark."""
    items = []
    for lane in fsdb.LANES:
        env = fsdb.ENVIRONMENTS[lane]
        registry, catalog = Registry.from_environment(env), Catalog.from_environment(env)
        for kind in ("file", "column"):
            for r in registry.of_kind(kind):
                level, _ = catalog.sensitivity(kind, r.id, r)
                if level is not None:
                    items.append((kind, r.id, level))
    return sorted(set(items))


def sensitivity_report(filler: FactFiller, items: list[tuple[str, str, str]]) -> dict[str, Any]:
    confusion: Counter[tuple[str, str]] = Counter()
    rows = []
    for kind, target, gold in items:
        fill = filler.classify(target, f"read {kind}: {target}", SENSITIVITY)
        answer = fill.value if fill is not None else "none"
        confusion[(gold, answer)] += 1
        rows.append({"kind": kind, "target": target, "gold": gold, "answer": answer})
    n = len(rows)
    lower = sum(r["answer"] != "none" and SENSITIVITY.index(r["answer"]) < SENSITIVITY.index(r["gold"]) for r in rows)
    return {
        "items": n,
        "accuracy": sum(r["answer"] == r["gold"] for r in rows) / n if n else 0.0,
        "under_classified": lower / n if n else 0.0,
        "no_answer": sum(r["answer"] == "none" for r in rows),
        "confusion": {f"{g}->{a}": c for (g, a), c in sorted(confusion.items())},
        "answers": rows,
    }


def client_from_args(base_url: str | None, model: str, *, api_key: str | None, structured: str,
                     extra_body: dict[str, Any] | None, cache: Path | None, check: bool = True,
                     replay: bool = False) -> LLMClient:
    """An endpoint client, smoke-checked first (uncached) so a server that ignores schemas is caught.
    With `replay`, answers come from the cache only and nothing is sent."""
    if replay:
        if cache is None:
            raise SystemExit("replay needs the answer cache")
        print(f"replay: answers from {cache} only")
        return CachedClient(OpenAIClient(base_url or "replay://", model, structured=structured, extra_body=extra_body,
                                         offline=True), cache)
    if not base_url:
        raise SystemExit("an endpoint is needed: set --base-url or DMGATE_LLM_BASE_URL (or replay from the cache)")
    client = OpenAIClient(base_url, model, api_key=api_key, structured=structured, extra_body=extra_body)
    if check:
        ok, message = smoke_check(client)
        if not ok:
            raise SystemExit(f"smoke check failed: {message}")
        print(f"smoke check: {message}")
    return CachedClient(client, cache) if cache is not None else client


def default_cache(model: str) -> Path:
    return Path("results") / "llm-cache" / (model.replace("/", "__") + ".jsonl")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Per-fact accuracy of the LLM layer on the FS/DB dev benchmark.")
    parser.add_argument("--base-url", default=os.environ.get("DMGATE_LLM_BASE_URL"), help="e.g. http://127.0.0.1:8000/v1")
    parser.add_argument("--model", default=os.environ.get("DMGATE_LLM_MODEL"))
    parser.add_argument("--api-key-env", default="DMGATE_LLM_API_KEY", help="name of the variable holding the key")
    parser.add_argument("--structured", choices=STRUCTURED_MODES,
                        default=os.environ.get("DMGATE_LLM_STRUCTURED", "response_format"))
    parser.add_argument("--extra-body", default=os.environ.get("DMGATE_LLM_EXTRA_BODY"),
                        help='JSON merged into each request, e.g. {"chat_template_kwargs": {"enable_thinking": false}}')
    parser.add_argument("--task", default="compiler,sensitivity", help="smoke, compiler, sensitivity (comma-separated)")
    parser.add_argument("--families", default="", help="comma-separated family codes (FL,FR,FS,DD,DU,DQ); default all")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--replay", action="store_true", default=os.environ.get("DMGATE_LLM_REPLAY") == "1",
                        help="answer from results/llm-cache only, without an endpoint (DMGATE_LLM_REPLAY=1)")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    if not args.model or not (args.base_url or args.replay):
        parser.error("--model and --base-url are required (or DMGATE_LLM_MODEL and DMGATE_LLM_BASE_URL; or --replay)")

    client = client_from_args(args.base_url, args.model, api_key=os.environ.get(args.api_key_env),
                              structured=args.structured,
                              extra_body=json.loads(args.extra_body) if args.extra_body else None,
                              cache=None if args.no_cache else default_cache(args.model), replay=args.replay)
    tasks = set(args.task.split(","))
    if tasks == {"smoke"}:
        return
    families = set(filter(None, args.families.split(",")))
    out = args.out or Path("results") / f"{datetime.now():%Y%m%d-%H%M%S}-llm-eval"
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"model": args.model, "base_url": args.base_url}
    if "compiler" in tasks:
        prompts = [p for p in fsdb.prompts() if not families or p.prompt_id[:2] in families]
        report["compiler"] = compiler_report(ContractCompiler(client, fsdb.profile_contract()), prompts)
        print("compiler:", json.dumps(report["compiler"]["overall"], indent=2))
        print("compiler by family:", json.dumps({k: v["exact_coverage"] for k, v in report["compiler"]["by_family"].items()}))
    if "sensitivity" in tasks:
        report["sensitivity"] = sensitivity_report(FactFiller(client), sensitivity_items())
        shown = {k: v for k, v in report["sensitivity"].items() if k != "answers"}
        print("sensitivity:", json.dumps(shown, indent=2))
    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"-> {out / 'report.json'}")


if __name__ == "__main__":
    main()
