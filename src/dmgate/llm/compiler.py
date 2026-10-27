"""Intent contract compiler (DESIGN.md §6.1, position 3): trusted words -> envelope and targets.

Input is trusted only: the user's turns and a description of the platform's resources (kinds,
attributes, and the values of short categorical attributes). Tool output never reaches it, and
neither do resource names, which other people can choose.

The model answers in constrained JSON: grants (verb, kinds, sensitivity ceiling) and targets (ids,
path patterns, or attribute conditions that code evaluates against the registry). Code then:

- **grounds** every grant and target: each must quote the user's words, and the quote must appear
  in a user turn. Delete, control and egress grants must quote the *latest* turn, which enforces
  their one-turn lifetime; read, write and exec grants may quote any turn;
- stamps grants as coming from the user turn, with the lifetimes of position 3;
- adds the admin's default profile, which the model never writes.

An invalid answer leaves the profile alone in force, so a compiler failure fails closed (Ask).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from dmgate.contract import CONDITION_OPS, ONE_TURN_VERBS, Condition, Contract, Grant, GrantSource, TargetSource, TargetSpec
from dmgate.effects import Registry, Verb
from dmgate.facts.schema import SENSITIVITY
from dmgate.llm.client import LLMClient, Messages
from dmgate.records import GateInput, Segment, Trust

PROMPT_VERSION = "compiler-v1"
KINDS = ("file", "row", "column", "table", "branch", "job", "service", "object", "host", "recipient", "repo")
EGRESS_SINKS = frozenset({"remote_allowlisted", "remote_other", "recipient_named"})
MIN_QUOTE = 3
SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:@/+-]{1,40}$")
MAX_LISTED = 8

_SCALAR = {"anyOf": [{"type": "string", "maxLength": 200}, {"type": "number"}, {"type": "boolean"}]}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["grants", "targets"],
    "properties": {
        "grants": {"type": "array", "maxItems": 8, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["verb", "kinds", "max_sensitivity", "quote"],
            "properties": {
                "verb": {"type": "string", "enum": [v.value for v in Verb]},
                "kinds": {"type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "string", "enum": list(KINDS)}},
                "max_sensitivity": {"type": "string", "enum": list(SENSITIVITY)},
                "quote": {"type": "string", "maxLength": 200},
            }}},
        "targets": {"type": "array", "maxItems": 8, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "ids", "patterns", "where", "expected_count", "quote"],
            "properties": {
                "kind": {"type": "string", "enum": list(KINDS)},
                "ids": {"type": "array", "maxItems": 50, "items": {"type": "string", "maxLength": 300}},
                "patterns": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 300}},
                "where": {"type": "array", "maxItems": 8, "items": {
                    "type": "object", "additionalProperties": False, "required": ["attr", "op", "value"],
                    "properties": {
                        "attr": {"type": "string", "maxLength": 80},
                        "op": {"type": "string", "enum": list(CONDITION_OPS)},
                        "value": {"anyOf": [*_SCALAR["anyOf"], {"type": "array", "maxItems": 20, "items": _SCALAR}]},
                    }}},
                "expected_count": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}]},
                "quote": {"type": "string", "maxLength": 200},
            }}},
    },
}

SYSTEM = """You compile a user's request to an AI agent into a permission contract. You see only the \
user's own words and a description of the platform's resources. Answer with JSON only.

grants: one per kind of operation the request needs.
- verb: read, write, delete, control (cancel, restart, stop), egress (send data off the platform), exec.
- kinds: the resource kinds the operation applies to.
- max_sensitivity: the most sensitive data the operation needs. S0 public, S1 internal, S2 \
confidential or personal (customer data, emails, names), S3 secret (credentials, keys, identity \
numbers). Use the labels in the resource description when they exist.
- Grant only what the request asks for, plus reading what is needed to do it. A question asks for \
read only. Never grant delete, control or egress unless the latest message asks for it.
- quote: the exact words of the user that ask for this operation.

targets: which resources the request is about, one entry per kind.
- Prefer where-conditions on the listed attributes. Use ids or path patterns only for names the \
user writes out. "my" means the owner attribute equals the user. "old" or "stale" means a stale \
attribute is true. A range like 48xx means the prefix 48.
- Database records are kind row: add the condition table = <table> and conditions on values.<column>.
- For database reads and updates, add a column target with exactly the columns the request needs, \
as ids "<table>.<column>".
- expected_count: 1 when the request names one thing in the singular, otherwise null.
- quote: the exact words of the user that name the targets."""

EXEMPLARS: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "User: dana\n- file: attributes owner (string: dana, lee, ops), kind (string: build, cache, report), "
        "release (string: 2.2, 2.3, 2.4), stale (boolean), protected (boolean)\n"
        "  directories: /work/dist/dana, /work/dist/lee, /work/dist/ops\n\n"
        "Conversation (the user's own words, oldest first):\n[latest] Remove my old build output for release 2.3 "
        "from dist.",
        {"grants": [{"verb": "delete", "kinds": ["file"], "max_sensitivity": "S1", "quote": "Remove"}],
         "targets": [{"kind": "file", "ids": [], "patterns": [],
                      "where": [{"attr": "owner", "op": "eq", "value": "dana"},
                                {"attr": "kind", "op": "eq", "value": "build"},
                                {"attr": "release", "op": "eq", "value": "2.3"},
                                {"attr": "stale", "op": "eq", "value": True}],
                      "expected_count": 1, "quote": "my old build output for release 2.3"}]},
    ),
    (
        "User: dana\n- table: tickets\n- row: records, selected with table = <table> and values.<column>:\n"
        "  tickets: values.id (integer), values.owner "
        "(string: dana, lee), values.state (string: closed, open), values.product (string), values.reporter_email "
        "(string), values.title (string)\n- column: tickets.id S0, tickets.owner S1, tickets.state S0, "
        "tickets.product S0, tickets.reporter_email S2, tickets.title S1\n\n"
        "Conversation (the user's own words, oldest first):\n[latest] What are the reporter emails on my closed "
        "tickets for products starting with PAY-?",
        {"grants": [{"verb": "read", "kinds": ["row", "column"], "max_sensitivity": "S2",
                     "quote": "What are the reporter emails"}],
         "targets": [{"kind": "row", "ids": [], "patterns": [],
                      "where": [{"attr": "table", "op": "eq", "value": "tickets"},
                                {"attr": "values.owner", "op": "eq", "value": "dana"},
                                {"attr": "values.state", "op": "eq", "value": "closed"},
                                {"attr": "values.product", "op": "prefix", "value": "PAY-"}],
                      "expected_count": None, "quote": "my closed tickets"},
                     {"kind": "column", "ids": ["tickets.reporter_email"], "patterns": [], "where": [],
                      "expected_count": None, "quote": "reporter emails"}]},
    ),
    (
        "User: dana\n- file: attributes owner (string: dana, lee), protected (boolean)\n"
        "  directories: /work/reports\n\n"
        "Conversation (the user's own words, oldest first):\n[latest] Upload /work/reports/summary-q2.pdf to "
        "share.example.org please.",
        {"grants": [{"verb": "read", "kinds": ["file"], "max_sensitivity": "S1", "quote": "Upload"},
                    {"verb": "egress", "kinds": ["host"], "max_sensitivity": "S1", "quote": "to share.example.org"}],
         "targets": [{"kind": "file", "ids": ["/work/reports/summary-q2.pdf"], "patterns": [], "where": [],
                      "expected_count": 1, "quote": "/work/reports/summary-q2.pdf"},
                     {"kind": "host", "ids": ["share.example.org"], "patterns": [], "where": [],
                      "expected_count": None, "quote": "share.example.org"}]},
    ),
    (
        "User: dana\n- table: invoices\n- row: records, selected with table = <table> and values.<column>:\n"
        "  invoices: values.id (integer), values.number "
        "(string), values.status (string: open, paid, void), values.amount (number)\n"
        "- column: invoices.id S0, invoices.number S0, invoices.status S1, invoices.amount S1\n\n"
        "Conversation (the user's own words, oldest first):\n[latest] Mark invoice INV-77 as paid.",
        {"grants": [{"verb": "write", "kinds": ["row", "column"], "max_sensitivity": "S1",
                     "quote": "Mark invoice INV-77 as paid"}],
         "targets": [{"kind": "row", "ids": [], "patterns": [],
                      "where": [{"attr": "table", "op": "eq", "value": "invoices"},
                                {"attr": "values.number", "op": "eq", "value": "INV-77"}],
                      "expected_count": 1, "quote": "invoice INV-77"},
                     {"kind": "column", "ids": ["invoices.status"], "patterns": [], "where": [],
                      "expected_count": None, "quote": "as paid"}]},
    ),
)


# ---------------------------------------------------------------------------------------------
# What the compiler may see


def _summarize(values: list[Any]) -> str:
    present = [v for v in values if v is not None]
    if present and all(isinstance(v, bool) for v in present):
        return "boolean"
    kind = "integer" if present and all(isinstance(v, int) for v in present) else \
        "number" if present and all(isinstance(v, (int, float)) for v in present) else "string"
    distinct = sorted({str(v) for v in present})
    if distinct and len(distinct) <= MAX_LISTED and all(SAFE_VALUE.match(v) for v in distinct):
        return f"{kind}: {', '.join(distinct)}"
    return kind


def describe_resources(environment: Mapping[str, Any]) -> str:
    """The platform's resources as the compiler sees them: kinds, attribute names and types, and the
    values of short categorical attributes. Never free-form names or contents."""
    registry = Registry.from_environment(environment)
    lines = [f"User: {environment.get('user', 'unknown')}"]
    kinds = sorted({r.kind for kind in KINDS for r in registry.of_kind(kind)})
    for kind in kinds:
        resources = registry.of_kind(kind)
        if kind == "table":
            names = sorted(r.id for r in resources if SAFE_VALUE.match(r.id))
            lines.append(f"- table: {', '.join(names)}")
            continue
        if kind == "column":
            labelled = [f"{r.id} {r.attrs['sensitivity']}" if r.attrs.get("sensitivity") else r.id
                        for r in sorted(resources, key=lambda r: r.id) if SAFE_VALUE.match(r.id)]
            lines.append(f"- column: {', '.join(labelled)}")
            continue
        if kind == "row":
            tables: dict[str, dict[str, list[Any]]] = {}
            for r in resources:
                columns = tables.setdefault(str(r.attrs.get("table")), {})
                for c, v in r.attrs.get("values", {}).items():
                    columns.setdefault(c, []).append(v)
            lines.append("- row: records, selected with table = <table> and values.<column>:")
            for table, columns in sorted(tables.items()):
                if SAFE_VALUE.match(table):
                    described = ", ".join(f"values.{c} ({_summarize(vs)})" for c, vs in columns.items())
                    lines.append(f"  {table}: {described}")
            continue
        attrs: dict[str, list[Any]] = {}
        for r in resources:
            for a, v in r.attrs.items():
                if a != "name" and not isinstance(v, (dict, list)):
                    attrs.setdefault(a, []).append(v)
        described = ", ".join(f"{a} ({_summarize(vs)})" for a, vs in attrs.items())
        lines.append(f"- {kind}: attributes {described}" if described else f"- {kind}")
        if kind == "file":
            dirs = sorted({r.id.rsplit("/", 1)[0] for r in resources if "/" in r.id})
            safe = [d for d in dirs if SAFE_VALUE.match(d.replace("/", "")) or SAFE_VALUE.match(d)][:24]
            if safe:
                lines.append(f"  directories: {', '.join(safe)}")
    return "\n".join(lines)


def user_turns(context: tuple[Segment, ...]) -> list[str]:
    return [s.text for s in context if s.role == "user" and s.trust is Trust.TRUSTED]


def messages_for(turns: list[str], environment: Mapping[str, Any]) -> Messages:
    conversation = "\n".join(
        f"[{'latest' if i == len(turns) - 1 else f'turn {i + 1}'}] {text}" for i, text in enumerate(turns))
    out: Messages = [{"role": "system", "content": SYSTEM}]
    for prompt, answer in EXEMPLARS:
        out += [{"role": "user", "content": prompt}, {"role": "assistant", "content": json.dumps(answer)}]
    out.append({"role": "user", "content": f"{describe_resources(environment)}\n\n"
                                           f"Conversation (the user's own words, oldest first):\n{conversation}"})
    return out


# ---------------------------------------------------------------------------------------------
# From the model's answer to a contract


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def grounded(quote: str, texts: list[str]) -> bool:
    q = _normalize(quote)
    return len(q) >= MIN_QUOTE and any(q in _normalize(t) for t in texts)


def to_contract(output: Mapping[str, Any], turns: list[str], profile: Contract) -> tuple[Contract, list[str]]:
    """The contract, and notes on everything dropped. Turn numbers count user turns from 0."""
    turn = len(turns) - 1
    notes: list[str] = []
    grants = []
    for n, g in enumerate(output["grants"]):
        verb = Verb(g["verb"])
        scope = turns[-1:] if verb in ONE_TURN_VERBS else turns
        if not grounded(g["quote"], scope):
            where = "the latest turn" if verb in ONE_TURN_VERBS else "any user turn"
            notes.append(f"dropped grant {verb.value}: quote {g['quote']!r} is not in {where}")
            continue
        grants.append(Grant(verb, frozenset(g["kinds"]), g["max_sensitivity"],
                            EGRESS_SINKS if verb is Verb.EGRESS else frozenset(), GrantSource.USER_TURN, turn,
                            grant_id=f"compiled-{n}"))
    targets = []
    for n, t in enumerate(output["targets"]):
        if not grounded(t["quote"], turns):
            notes.append(f"dropped target {t['kind']}: quote {t['quote']!r} is not in any user turn")
            continue
        try:
            where = tuple(Condition(c["attr"], c["op"], c["value"] if c["op"] != "in" or isinstance(c["value"], list)
                                    else [c["value"]]) for c in t["where"])
            targets.append(TargetSpec(t["kind"], frozenset(t["ids"]), tuple(t["patterns"]), where, TargetSource.USER,
                                      t["expected_count"]))
        except (ValueError, TypeError) as error:
            notes.append(f"dropped target {t['kind']}: {error}")
    contract = replace(profile, turn=max(turn, profile.turn), grants=(*profile.grants, *grants),
                       targets=(*profile.targets, *targets))
    return contract, notes


@dataclass(frozen=True)
class Compiled:
    contract: Contract
    notes: tuple[str, ...]
    output: dict[str, Any] | None
    model: str
    prompt_version: str = PROMPT_VERSION

    def trace(self) -> dict[str, Any]:
        return {"model": self.model, "prompt_version": self.prompt_version, "output": self.output,
                "notes": list(self.notes)}


@dataclass
class ContractCompiler:
    """A contract provider for the gate. Compiles once per conversation state (cached)."""

    client: LLMClient
    profile: Contract
    max_tokens: int = 1200
    _cache: dict[str, Compiled] = field(default_factory=dict)

    def compile(self, context: tuple[Segment, ...], environment: Mapping[str, Any]) -> Compiled:
        turns = user_turns(context)
        if not turns:
            return Compiled(self.profile, ("no user turn: profile only",), None, self.client.model)
        messages = messages_for(turns, environment)
        key = hashlib.sha256(json.dumps(messages, sort_keys=True).encode("utf-8")).hexdigest()
        if key not in self._cache:
            completion = self.client.complete_json(messages, OUTPUT_SCHEMA, name="contract", max_tokens=self.max_tokens)
            output = completion.valid[0] if completion.valid else None
            if output is None:
                compiled = Compiled(replace(self.profile, turn=len(turns) - 1),
                                    ("invalid compiler output: profile only (fails closed)",), None, completion.model)
            else:
                contract, notes = to_contract(output, turns, self.profile)
                compiled = Compiled(contract, tuple(notes), output, completion.model)
            self._cache[key] = compiled
        return self._cache[key]

    def __call__(self, request: GateInput) -> Contract:
        return self.compile(request.context, request.environment).contract

    def explain(self, request: GateInput) -> dict[str, Any]:
        return self.compile(request.context, request.environment).trace()
