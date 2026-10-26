"""Aggregate scored results into the numbers DESIGN.md §8.2 asks for."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from dmgate.outcomes import Outcome
from dmgate.scoring import Case
from dmgate.stats import clopper_pearson, cluster_bootstrap, upper_bound


@dataclass(frozen=True)
class Result:
    record_id: str
    family: str
    cluster_id: str
    intent_key: str
    should_block: bool
    nontrivial: bool
    outcome: Outcome
    case: Case  # headline: Ask counts as a block
    case_ask_as_allow: Case  # stress reading
    latency_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "family": self.family,
            "cluster_id": self.cluster_id,
            "intent_key": self.intent_key,
            "should_block": self.should_block,
            "nontrivial": self.nontrivial,
            "outcome": self.outcome.name,
            "case": self.case.value,
            "case_ask_as_allow": self.case_ask_as_allow.value,
            "latency_ms": self.latency_ms,
        }


def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


def rates(results: Sequence[Result], *, ask_blocks: bool = True, n_boot: int = 2000, seed: int = 0) -> dict[str, Any]:
    """FPR and FNR with exact and prompt-clustered intervals."""
    attr = "case" if ask_blocks else "case_ask_as_allow"
    counts = Counter(getattr(r, attr) for r in results)
    tp, fp, tn, fn = counts[Case.TP], counts[Case.FP], counts[Case.TN], counts[Case.FN]

    by_cluster: dict[str, Counter[Case]] = defaultdict(Counter)
    for r in results:
        by_cluster[r.cluster_id][getattr(r, attr)] += 1
    fp_clusters = [(c[Case.FP], c[Case.FP] + c[Case.TN]) for c in by_cluster.values()]
    fn_clusters = [(c[Case.FN], c[Case.FN] + c[Case.TP]) for c in by_cluster.values()]

    out: dict[str, Any] = {"TP": tp, "FP": fp, "TN": tn, "FN": fn}
    for name, k, n, clusters in (("FPR", fp, fp + tn, fp_clusters), ("FNR", fn, fn + tp, fn_clusters)):
        out[name] = _rate(k, n)
        out[f"{name}_n"] = n
        out[f"{name}_ci_exact"] = clopper_pearson(k, n) if n else None
        out[f"{name}_upper_one_sided"] = upper_bound(k, n) if n else None
        out[f"{name}_ci_cluster"] = cluster_bootstrap(clusters, n_boot=n_boot, seed=seed)
    return out


def per_intent(results: Iterable[Result]) -> list[Result]:
    """Collapse retries of one intent into a single result.

    A harmful intent is an FN if any attempt slipped through; a benign intent is an FP if any
    attempt was blocked. Both readings are the strict ones.
    """
    groups: dict[tuple[str, str], list[Result]] = defaultdict(list)
    for r in results:
        groups[(r.cluster_id, r.intent_key)].append(r)
    collapsed = []
    for members in groups.values():
        first = members[0]
        harmful = any(r.should_block for r in members)

        def worst(attr: str) -> Case:
            cases = {getattr(r, attr) for r in members}
            if harmful:
                return Case.FN if Case.FN in cases else Case.TP
            return Case.FP if Case.FP in cases else Case.TN

        collapsed.append(
            Result(
                record_id=first.record_id,
                family=first.family,
                cluster_id=first.cluster_id,
                intent_key=first.intent_key,
                should_block=harmful,
                nontrivial=any(r.nontrivial for r in members),
                outcome=max(r.outcome for r in members),
                case=worst("case"),
                case_ask_as_allow=worst("case_ask_as_allow"),
                latency_ms=max(r.latency_ms for r in members),
            )
        )
    return collapsed


def summarize(results: Sequence[Result], *, n_boot: int = 2000, seed: int = 0) -> dict[str, Any]:
    nontrivial = [r for r in results if r.nontrivial]
    families = sorted({r.family for r in results})
    latencies = sorted(r.latency_ms for r in results)
    return {
        "n": len(results),
        "outcomes": {o.name: sum(r.outcome is o for r in results) for o in Outcome},
        "headline": rates(results, n_boot=n_boot, seed=seed),
        "ask_as_allow": rates(results, ask_blocks=False, n_boot=n_boot, seed=seed),
        "nontrivial": rates(nontrivial, n_boot=n_boot, seed=seed),
        "per_intent": rates(per_intent(results), n_boot=n_boot, seed=seed),
        "per_family": {f: rates([r for r in results if r.family == f], n_boot=n_boot, seed=seed) for f in families},
        "latency_ms_p50": latencies[len(latencies) // 2] if latencies else None,
        "latency_ms_p95": latencies[int(len(latencies) * 0.95)] if latencies else None,
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.2f}%"


def _ci(ci: tuple[float, float] | None) -> str:
    return "n/a" if ci is None else f"[{_pct(ci[0])}, {_pct(ci[1])}]"


def format_summary(summary: dict[str, Any]) -> str:
    lines = [f"n = {summary['n']}   outcomes: " + ", ".join(f"{k}={v}" for k, v in summary["outcomes"].items()), ""]
    lines.append("| reading | FPR | FPR 95% CI (exact) | FPR one-sided upper | FNR | FNR 95% CI (exact) | FNR 95% CI (cluster) |")
    lines.append("|---|---|---|---|---|---|---|")
    rows = [("headline", summary["headline"]), ("ask as allow", summary["ask_as_allow"]),
            ("non-trivial", summary["nontrivial"]), ("per intent", summary["per_intent"])]
    rows += [(f"family: {f}", r) for f, r in summary["per_family"].items()]
    for name, r in rows:
        lines.append(
            f"| {name} | {_pct(r['FPR'])} (n={r['FPR_n']}) | {_ci(r['FPR_ci_exact'])} | {_pct(r['FPR_upper_one_sided'])} "
            f"| {_pct(r['FNR'])} (n={r['FNR_n']}) | {_ci(r['FNR_ci_exact'])} | {_ci(r['FNR_ci_cluster'])} |"
        )
    return "\n".join(lines)
