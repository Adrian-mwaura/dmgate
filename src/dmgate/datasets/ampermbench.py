"""AmPermBench adapter (arXiv 2604.04978).

Reads the benchmark's data files only; never imports or runs its code. AmPermBench ships prompts,
deterministic oracles and initial resource states, but no recorded agent runs, so there are two
record sources:

- `probe_records()`: candidate actions enumerated per prompt (every single target, the authorized
  batch, and authorized-plus-one-extra batches), labeled by the oracle. Used to validate the
  harness and stress target-set reasoning. It is derived from the benchmark, so the test-only rule
  applies: never tune prompts, rules or exemplars on it.
- Agent runs (P1): OpenHands trajectories in the benchmark's task environments.

An action is safe iff every resource it affects is in the prompt's authorized set.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dmgate.records import Action, ActionRecord, Label, Segment, Trust

DATASET = "ampermbench-probes"


@dataclass(frozen=True)
class FamilySpec:
    prompt_file: str
    oracle_file: str
    state_file: str
    authorized_key: str


FAMILIES: dict[str, FamilySpec] = {
    "clean-up-branches": FamilySpec("clean_branches.json", "prompt_oracle.json", "branches.json", "authorized_delete_set"),
    "cancel-jobs": FamilySpec("cancel_jobs.json", "jobs_oracle.json", "jobs.initial.json", "authorized_cancel_set"),
    "restart-services": FamilySpec("restart_services.json", "services_oracle.json", "services.initial.json", "authorized_restart_set"),
    "clean-up-artifacts": FamilySpec("cleanup_artifacts.json", "artifacts_oracle.json", "objects.initial.json", "authorized_delete_set"),
}

_KIND = {"clean-up-branches": "branch", "cancel-jobs": "job", "restart-services": "service", "clean-up-artifacts": "object"}

# How the benchmark's agents reach its CLIs besides calling them by name (see its trace normalizer).
COMMAND_WRAPPERS = [
    {"argv_prefix": ["ampermbench-dispatch"], "skip": 1},
    {"argv_prefix": ["python3", "-m", "ampermbench.tasks.dispatch"], "skip": 1},
    {"argv_prefix": ["python", "-m", "ampermbench.tasks.dispatch"], "skip": 1},
]

# Families whose state file is a list of resources: (list key, id field).
_LIST_STATES = {
    "cancel-jobs": ("jobs", "job_id"),
    "restart-services": ("services", "service_id"),
    "clean-up-artifacts": ("objects", "object_id"),
}


@dataclass(frozen=True)
class FamilyData:
    family: str
    prompts: list[dict[str, Any]]
    oracle: dict[str, dict[str, Any]]
    resources: dict[str, dict[str, Any]]  # resource id -> attributes (the platform registry)
    user: str


def default_root() -> Path:
    env = os.environ.get("AMPERMBENCH_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / "external" / "cc-auto-mode-measurement"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_family(root: Path, family: str) -> FamilyData:
    spec = FAMILIES[family]
    harness = root / "tasks" / family / "harness"
    if not harness.is_dir():
        raise FileNotFoundError(f"AmPermBench task data not found at {harness}; see CLAUDE.md for the clone command")
    prompts = _read_json(harness / "prompts" / spec.prompt_file)
    oracle = _read_json(harness / "meta" / spec.oracle_file)
    state = _read_json(harness / "meta" / spec.state_file)
    user = _read_json(harness / "meta" / "user_context.json")["user"]

    if family in _LIST_STATES:
        key, id_field = _LIST_STATES[family]
        resources = {item[id_field]: item for item in state[key]}
    else:
        resources = dict(state["branches"])

    prompt_ids = {p["prompt_id"] for p in prompts}
    if prompt_ids != set(oracle):
        raise ValueError(f"{family}: prompts and oracle disagree on prompt ids")
    return FamilyData(family, prompts, oracle, resources, user)


def registry_entries(data: FamilyData) -> list[dict[str, Any]]:
    """Resources in the generic registry format, with generic attribute aliases added."""
    entries = []
    for rid, attrs in sorted(data.resources.items()):
        a = dict(attrs)
        if data.family == "cancel-jobs":
            a["partition"] = attrs["queue"]
        elif data.family == "restart-services":
            a["namespace"] = attrs["env"]
        elif data.family == "clean-up-artifacts":
            a["bucket"] = "bench-artifacts"
            a["namespace"] = attrs["ns"]
        entries.append({"id": rid, "kind": _KIND[data.family], "attrs": a})
    return entries


def environment_for(data: FamilyData) -> dict[str, Any]:
    return {
        "domain": data.family,
        "user": data.user,
        "cwd": "/bench",
        "resources": registry_entries(data),
        "command_wrappers": COMMAND_WRAPPERS,
    }


def command_for(family: str, targets: Iterable[str], resources: dict[str, dict[str, Any]]) -> str:
    """The shell command the benchmark's shims accept for acting on `targets`."""
    ids = sorted(targets)
    if family == "clean-up-branches":
        local = [i for i in ids if not i.startswith("origin/")]
        remote = [i.removeprefix("origin/") for i in ids if i.startswith("origin/")]
        parts = []
        if local:
            parts.append("git branch -D " + " ".join(local))
        if remote:
            parts.append("git push origin --delete " + " ".join(remote))
        return " && ".join(parts)
    if family == "cancel-jobs":
        return "scancel " + " ".join(ids)
    if family == "restart-services":
        by_env: dict[str, list[str]] = defaultdict(list)
        for i in ids:
            by_env[resources[i]["env"]].append(i)
        return " && ".join(
            "kubectl rollout restart " + " ".join(f"deployment/{i}" for i in group) + f" -n {env}"
            for env, group in sorted(by_env.items())
        )
    if family == "clean-up-artifacts":
        return " && ".join(f"aws s3 rm s3://bench-artifacts/{resources[i]['key']}" for i in ids)
    raise ValueError(f"unknown family {family!r}")


def probe_records(root: Path | None = None) -> list[ActionRecord]:
    root = root or default_root()
    records = []
    for family, spec in FAMILIES.items():
        data = load_family(root, family)
        environment = environment_for(data)
        for prompt in data.prompts:
            prompt_id = prompt["prompt_id"]
            oracle = data.oracle[prompt_id]
            authorized = frozenset(oracle[spec.authorized_key])
            context = (
                Segment("system", f"Platform user: {data.user}", Trust.TRUSTED),
                Segment("user", prompt["prompt"], Trust.TRUSTED),
            )
            probes = [("single", frozenset({rid})) for rid in sorted(data.resources)]
            if len(authorized) > 1:
                probes.append(("batch-authorized", authorized))
            probes += [("batch-overscope", authorized | {extra}) for extra in sorted(oracle["must_preserve"])]

            for kind, targets in probes:
                intent = "+".join(sorted(targets))
                records.append(
                    ActionRecord(
                        record_id=f"{prompt_id}:{kind}:{intent}",
                        dataset=DATASET,
                        family=family,
                        cluster_id=prompt_id,
                        intent_key=intent,
                        context=context,
                        action=Action("bash", {"command": command_for(family, targets, data.resources)}),
                        label=Label.from_targets(targets, authorized, source="ampermbench-oracle"),
                        axes={"S": prompt["s_axis"], "B": prompt["b_axis"], "R": prompt["r_axis"], "probe": kind},
                        environment=environment,
                    )
                )
    return records
