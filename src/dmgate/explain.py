"""Explain one action end to end: the prompt, the ground truth, the resolved effects, the label,
and how every possible gate outcome would score.

A teaching and debugging aid. The ground truth is printed for the human reader; it never reaches a
gate. Example:

    python -m dmgate.explain --prompt CA-S2-B2-R0 --command "aws s3 rm s3://bench-artifacts/dev/alex/ --recursive"
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from dmgate.datasets.ampermbench import FAMILIES, FamilyData, default_root, environment_for, load_family
from dmgate.effects import STATE_CHANGING, resolve_shell
from dmgate.outcomes import Decision, Outcome
from dmgate.records import Label
from dmgate.scoring import score


def find_prompt(prompt_id: str, root: Path | None = None) -> tuple[FamilyData, dict[str, Any], frozenset[str]]:
    """The family data, the prompt record and the authorized set for an AmPermBench prompt id."""
    root = root or default_root()
    for family, spec in FAMILIES.items():
        data = load_family(root, family)
        for prompt in data.prompts:
            if prompt["prompt_id"] == prompt_id:
                return data, prompt, frozenset(data.oracle[prompt_id][spec.authorized_key])
    raise KeyError(f"no AmPermBench prompt {prompt_id!r}")


def _ids(items: frozenset[str] | set[str]) -> str:
    return ", ".join(sorted(items)) if items else "(none)"


def explain(prompt_id: str, command: str, root: Path | None = None) -> str:
    data, prompt, authorized = find_prompt(prompt_id, root)
    resolution = resolve_shell(command, environment_for(data))
    out = [
        f"PROMPT {prompt_id}  family={data.family}  S={prompt['s_axis']} B={prompt['b_axis']} R={prompt['r_axis']}",
        f"  {prompt['prompt']}",
        "",
        "GROUND TRUTH (from the benchmark's oracle; a gate never sees this)",
        f"  authorized: {_ids(authorized)}",
        "",
        f"COMMAND  {command}",
        "",
        "EFFECTS (what the M1 resolver says the command does)",
    ]
    for i, e in enumerate(resolution.effects):
        flags = [f for f, on in (("opaque", e.opaque), ("inexact", not e.exact)) if on]
        out.append(f"  [{i}] {e.verb.value:<7} {e.kind:<8} targets: {_ids(e.targets)}")
        if e.unresolved:
            out.append(f"      unresolved: {', '.join(e.unresolved)}")
        if flags or e.note:
            out.append("      " + " ".join([*flags, e.note]).strip())
    if not resolution.effects:
        out.append("  (no effects)")
    if resolution.flows:
        out.append("  data flows: " + ", ".join(f"[{a}]->[{b}]" for a, b in resolution.flows))
    if resolution.parse_error:
        out.append("  WARNING: the command did not parse cleanly")

    changing = [e for e in resolution.effects if e.verb in STATE_CHANGING]
    targets = resolution.changed()
    out += ["", "LABEL"]
    if not changing:
        out.append("  No state-changing effects. Reads are not labeled in AmPermBench.")
        if resolution.opaque:
            out.append("  But the command is opaque: we cannot see what it changes. That is what the")
            out.append("  transactional sandbox (DESIGN.md section 6.3) exists for.")
        return "\n".join(out)
    label = Label.from_targets(targets, authorized, source="ampermbench-oracle")
    out.append(f"  affected:     {_ids(targets)}")
    out.append(f"  authorized:   {_ids(targets & authorized)}")
    out.append(f"  unauthorized: {_ids(targets - authorized)}")
    out.append(f"  -> {'HARMFUL: should be blocked' if label.should_block else 'BENIGN: should be allowed'}")
    if not resolution.exact:
        out.append("  WARNING: some effects are unresolved or opaque, so the true target set may be larger.")

    residual = targets & authorized
    decisions = [
        ("Allow", Decision(Outcome.ALLOW)),
        ("Minimize", Decision(Outcome.MINIMIZE, residual_targets=residual)),
        ("Ask", Decision(Outcome.ASK)),
        ("Deny", Decision(Outcome.DENY)),
    ]
    out += [
        "",
        "HOW EACH GATE OUTCOME WOULD SCORE (headline / Ask-as-allow stress reading)",
        f"  (Minimize here narrows the command to the authorized part: {_ids(residual)})",
    ]
    for name, decision in decisions:
        out.append(f"  {name:<10} {score(label, decision).value} / {score(label, decision, ask_blocks=False).value}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Explain one action against an AmPermBench prompt.")
    parser.add_argument("--prompt", required=True, help="AmPermBench prompt id, e.g. CA-S2-B2-R0")
    parser.add_argument("--command", required=True, help="the shell command the agent wants to run")
    args = parser.parse_args(argv)
    print(explain(args.prompt, args.command))


if __name__ == "__main__":
    main()
