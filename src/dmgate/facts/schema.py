"""The fact schema (DESIGN.md §5.3): every fact the rules may read, each with a finite domain.

The rules (M3) read only these facts, and the fact builder only emits values from these domains, so
a rule never meets a missing key or an out-of-domain value. Where the builder cannot decide, it says
`unknown` explicitly and the rules must state what unknown means.

`llm=True` marks facts the LLM fact filler (M5) may answer. It answers only facts the builder left
open for a given effect, and only with one of the candidate values the builder offered.

The JSON Schema export is what OPA type-checks policies against (`opa check --schema`). Regenerate
the committed copy with `python -m dmgate.facts.schema --write`.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SENSITIVITY = ("S0", "S1", "S2", "S3")
OPS = ("read", "write", "delete", "control", "egress", "exec")
PROVENANCE = ("user", "trusted_structure", "delegated", "observed", "agent_generated", "untrusted_instruction")
SINKS = ("none", "context", "local", "remote_named", "remote_allowlisted", "remote_other", "recipient_named",
         "recipient_unnamed", "unknown")
TARGET_CLASSES = ("workspace", "record", "resource", "system", "destination", "program")
VOLUMES = ("small", "large", "bulk", "unknown")
SECURITY_CHANGES = ("none", "auth", "ci", "permissions", "network", "deps", "unknown")
RISK_CATEGORIES = ("sensitive", "destructive", "egress", "untrusted_provenance")

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schema" / "facts.schema.json"


@dataclass(frozen=True)
class FactSpec:
    name: str
    domain: tuple[str, ...] | None  # None: boolean
    source: str
    description: str
    llm: bool = False

    def allows(self, value: Any) -> bool:
        if self.domain is None:
            return isinstance(value, bool)
        return isinstance(value, str) and value in self.domain


FACTS: tuple[FactSpec, ...] = (
    FactSpec("op", OPS, "effect resolver", "The effect's verb."),
    FactSpec("opaque", None, "effect resolver",
             "The program's behavior cannot be seen before it runs (interpreter, script, eval, unparsed command)."),
    FactSpec("unresolved", None, "effect resolver",
             "Some selector of the effect could not be resolved to concrete targets."),
    FactSpec("max_sensitivity", (*SENSITIVITY, "unknown"), "catalogs and detectors; LLM for uncatalogued targets",
             "Highest sensitivity of the data the effect touches, or carries for writes and egress.", llm=True),
    FactSpec("targets_in_contract", ("all", "some", "none"), "contract targets",
             "How many of the effect's targets the contract covers. Unresolved selectors never count as covered."),
    FactSpec("has_protected_target", None, "registry and catalog", "Some resolved target is protected."),
    FactSpec("target_class", TARGET_CLASSES, "admin catalog and kind defaults",
             "What the effect touches: workspace files are free to explore and edit; records and resources are "
             "narrowed to the contract (Q12)."),
    FactSpec("envelope_permits", None, "contract envelope",
             "An active grant covers this verb, kind, sensitivity (unknown counts as S3) and sink."),
    FactSpec("provenance", PROVENANCE, "context matching; LLM separates observation from instruction",
             "The least trusted origin among the effect's targets: where each was first named.", llm=True),
    FactSpec("sink_class", SINKS, "resolver data flows, contract destinations, egress allowlist",
             "Where the data ends up. remote_named: a host the contract names; remote_allowlisted: the admin "
             "allowlist."),
    FactSpec("volume", VOLUMES, "target count against the contract's expectation", "How much the effect touches."),
    FactSpec("session_chain", ("none", "sensitive_read_then_egress"), "session ledger and earlier effects",
             "This egress follows a read of S2+ data in the same session."),
    FactSpec("security_relevant_change", SECURITY_CHANGES, "path rules; LLM on the diff",
             "The write changes authentication, CI, permissions, network setup or dependencies.", llm=True),
    FactSpec("cited_authorization", ("none", "verified"), "LLM quote, deterministic check",
             "A quoted span of a trusted user turn authorizes this effect. Lowers soft rules only.", llm=True),
    FactSpec("residual_risk_flag", ("none", *RISK_CATEGORIES), "LLM holistic check",
             "A risk no other fact captured. Escalates to Ask only, alongside a risk fact.", llm=True),
)

BY_NAME: dict[str, FactSpec] = {spec.name: spec for spec in FACTS}


def validate(values: Mapping[str, Any]) -> list[str]:
    """Problems with one effect's fact values; empty when they match the schema exactly."""
    errors = [f"missing fact {spec.name}" for spec in FACTS if spec.name not in values]
    errors += [f"unknown fact {name}" for name in values if name not in BY_NAME]
    errors += [
        f"{name}={value!r} is outside its domain"
        for name, value in values.items()
        if name in BY_NAME and not BY_NAME[name].allows(value)
    ]
    return errors


def _property(spec: FactSpec) -> dict[str, Any]:
    prop: dict[str, Any] = {"description": spec.description}
    if spec.domain is None:
        prop["type"] = "boolean"
    else:
        prop["type"] = "string"
        prop["enum"] = list(spec.domain)
    return prop


def input_schema() -> dict[str, Any]:
    """JSON Schema (draft-07, which OPA understands) for the rule engine's input document."""
    effect = {
        "type": "object",
        "properties": {spec.name: _property(spec) for spec in FACTS},
        "required": [spec.name for spec in FACTS],
        "additionalProperties": False,
    }
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "DMGate rule input: one fact record per effect of an action",
        "type": "object",
        "properties": {
            "record_id": {"type": "string"},
            "effects": {"type": "array", "items": effect},
        },
        "required": ["record_id", "effects"],
        "additionalProperties": False,
    }


def render() -> str:
    return json.dumps(input_schema(), indent=2) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Print or write the fact schema as JSON Schema.")
    parser.add_argument("--write", action="store_true", help=f"write it to {SCHEMA_PATH}")
    args = parser.parse_args(argv)
    if args.write:
        SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
        SCHEMA_PATH.write_text(render(), encoding="utf-8", newline="\n")
        print(f"wrote {SCHEMA_PATH}")
    else:
        print(render(), end="")


if __name__ == "__main__":
    main()
