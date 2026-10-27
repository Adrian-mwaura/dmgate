"""Deterministic fact builder (DESIGN.md §6.4): effects + contract + catalogs + ledger -> facts.

One fact record per effect. The rules (M3) decide each effect, and the action's outcome is the join
(max) of its effects' outcomes. Every fact in the schema is always present, with an in-domain value.
Where the builder cannot decide, it says so in one of two ways:

- **unknown**: the value `unknown`, which the rules treat conservatively. Some unknowns can only be
  settled by watching the program run in the sandbox (opaque code, unresolved selectors).
- **open**: the fact lists the candidate values the LLM fact filler (M5) may choose from, with the
  evidence to look at. The filler answers open facts only, within the candidates. `build_facts` is
  then run again with the answers as `fills`, so everything derived from them (the envelope check,
  the payload of a later egress) is recomputed and the citation check re-applied.

The builder sees a `GateInput`, never a label.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from dmgate.contract import Contract
from dmgate.effects import LISTING, RECURSIVE, SENDS_DATA, SENDS_REPOSITORY, Effect, Registry, Resolution, Verb
from dmgate.facts.catalog import PERMISSION_COMMANDS, Catalog, detect_secrets
from dmgate.facts.ledger import SENSITIVE, SessionLedger
from dmgate.facts.schema import BY_NAME, FACTS, PROVENANCE, RISK_CATEGORIES, SECURITY_CHANGES, SENSITIVITY, validate
from dmgate.records import GateInput, Segment, Trust

FactValue = str | bool

CEILING_VERBS = frozenset({Verb.READ, Verb.WRITE, Verb.EGRESS})  # verbs that move data
SINK_RANK = ("none", "context", "local", "recipient_named", "remote_allowlisted", "unknown",
             "recipient_unnamed", "remote_other")  # least to most exposed
SECURITY_PRIORITY = ("auth", "permissions", "ci", "network", "deps")
NAME_ATTRS = ("name", "key", "path", "host", "email", "address")
MIN_NAME = 3  # shorter names are too common to show where a target came from
MIN_QUOTE = 8  # shorter quotes cannot carry an authorization
ON_DEMAND = frozenset({"cited_authorization", "residual_risk_flag"})


@dataclass(frozen=True)
class Fact:
    value: FactValue
    source: str
    evidence: tuple[str, ...] = ()
    open: tuple[str, ...] = ()  # candidate values the fact filler may choose from; empty: closed
    confidence: float | None = None


@dataclass(frozen=True)
class Fill:
    """The fact filler's answer to one open fact. For `cited_authorization`, `evidence[0]` is the quote."""

    value: FactValue
    source: str  # question id, prompt version and model, e.g. "llm:sensitivity-v1/qwen3-8b"
    evidence: tuple[str, ...] = ()
    confidence: float | None = None


@dataclass(frozen=True)
class EffectFacts:
    index: int
    effect: Effect
    facts: Mapping[str, Fact]
    covered: frozenset[str] = frozenset()  # the targets the contract covers: what a Minimize keeps

    def values(self) -> dict[str, FactValue]:
        return {name: fact.value for name, fact in self.facts.items()}

    def open_facts(self) -> dict[str, tuple[str, ...]]:
        return {name: fact.open for name, fact in self.facts.items() if fact.open}

    def gaps(self) -> dict[str, tuple[str, ...]]:
        """Open facts the filler should answer before the rules run. Cited authorization and the
        residual-risk flag are asked on demand, when the rules' result calls for them (M3, M5)."""
        return {name: c for name, c in self.open_facts().items() if name not in ON_DEMAND}


@dataclass(frozen=True)
class ActionFacts:
    record_id: str
    effects: tuple[EffectFacts, ...]

    def to_input(self) -> dict[str, Any]:
        """The rule engine's input document; `schema/facts.schema.json` describes it."""
        return {"record_id": self.record_id, "effects": [e.values() for e in self.effects]}

    def trace(self) -> list[dict[str, Any]]:
        """Facts with their sources and evidence, for the decision trace (§6.9)."""
        return [
            {
                "index": e.index,
                "verb": e.effect.verb.value,
                "kind": e.effect.kind,
                "targets": sorted(e.effect.targets),
                "facts": {
                    name: {"value": f.value, "source": f.source, "evidence": list(f.evidence),
                           "open": list(f.open), "confidence": f.confidence}
                    for name, f in e.facts.items()
                },
            }
            for e in self.effects
        ]


@dataclass(frozen=True)
class VolumeBuckets:
    """Bucket edges for `volume`: placeholders until calibrated on the FS/DB dev benchmark (M4)."""

    small_max: int = 3  # with no expectation: up to this many targets is small,
    large_max: int = 20  # up to this many is large, more is bulk
    large_factor: int = 4  # with an expectation e: up to e is small, up to e * factor is large

    def bucket(self, n: int, expected: int | None) -> str:
        if expected is None:
            return "small" if n <= self.small_max else "large" if n <= self.large_max else "bulk"
        return "small" if n <= expected else "large" if n <= expected * self.large_factor else "bulk"


def build_facts(
    request: GateInput,
    resolution: Resolution,
    contract: Contract,
    *,
    ledger: SessionLedger | None = None,
    fills: Mapping[tuple[int, str], Fill] | None = None,
    volume: VolumeBuckets = VolumeBuckets(),
) -> ActionFacts:
    """One fact record per effect. `fills` maps (effect index, fact name) to the filler's answers."""
    effects = list(resolution.effects)
    if resolution.parse_error:
        effects.append(Effect(Verb.EXEC, "process", opaque=True, command="<unparsed>",
                              note="the command did not parse, so what it does is unknown"))
    builder = _Builder(request, contract, ledger or SessionLedger(), tuple(effects), resolution.flows, volume)
    return builder.build(dict(fills or {}))


def verify_citation(quote: str, context: tuple[Segment, ...]) -> int | None:
    """Index of the trusted user turn containing `quote` (whitespace-normalized), or None."""
    q = " ".join(quote.split())
    if len(q) < MIN_QUOTE:
        return None
    for idx, seg in enumerate(context):
        if seg.role == "user" and seg.trust is Trust.TRUSTED and q in " ".join(seg.text.split()):
            return idx
    return None


@lru_cache(maxsize=4096)
def _name_pattern(name: str) -> re.Pattern[str]:
    # A name counts when it stands on its own: not glued to letters, digits, "_" or "-", and not
    # followed by ".word" (so "app.py" is not found inside "app.pyc", but "fix app.py." works).
    return re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(name) + r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9_])")


def _mentions(text: str, name: str) -> bool:
    return _name_pattern(name).search(text) is not None


def _closure(flows: tuple[tuple[int, int], ...], n: int) -> list[set[int]]:
    """For each effect, every effect its data reaches through any chain of flows."""
    direct: list[set[int]] = [set() for _ in range(n)]
    for a, b in flows:
        direct[a].add(b)
    reach: list[set[int]] = []
    for start in range(n):
        seen: set[int] = set()
        stack = list(direct[start])
        while stack:
            j = stack.pop()
            if j not in seen:
                seen.add(j)
                stack.extend(direct[j])
        seen.discard(start)
        reach.append(seen)
    return reach


def _top(values: list[str], order: tuple[str, ...]) -> str:
    return max(values, key=order.index)


class _Builder:
    def __init__(self, request: GateInput, contract: Contract, ledger: SessionLedger,
                 effects: tuple[Effect, ...], flows: tuple[tuple[int, int], ...], volume: VolumeBuckets):
        self.request = request
        self.cwd: str | None = request.environment.get("cwd")
        self.registry = Registry.from_environment(request.environment)
        self.catalog = Catalog.from_environment(request.environment)
        self.contract = contract
        self.ledger = ledger
        self.effects = effects
        self.volume_buckets = volume
        self.downstream = _closure(flows, len(effects))
        self.upstream = [{i for i in range(len(effects)) if j in self.downstream[i]} for j in range(len(effects))]
        self.action_text = " ".join(v for v in request.action.args.values() if isinstance(v, str))
        self.secrets = detect_secrets(self.action_text)
        self.fills: dict[tuple[int, str], Fill] = {}

    def build(self, fills: dict[tuple[int, str], Fill]) -> ActionFacts:
        for i, name in fills:
            if not 0 <= i < len(self.effects) or name not in BY_NAME:
                raise ValueError(f"no fact {name!r} for effect {i}")
        self.fills = fills
        own = [self._fill(i, "max_sensitivity", self._own_sensitivity(i)) for i in range(len(self.effects))]
        level = [self._sensitivity(i, own) for i in range(len(self.effects))]
        records = []
        for i, e in enumerate(self.effects):
            facts = {
                "op": Fact(e.verb.value, "resolver"),
                "opaque": Fact(e.opaque, "resolver", (e.note,) if e.opaque and e.note else ()),
                "unresolved": Fact(bool(e.unresolved), "resolver", e.unresolved),
                "max_sensitivity": level[i],
                "targets_in_contract": self._in_contract(e, covered := self._covered(e)),
                "has_protected_target": self._protected(e),
                "provenance": self._fill(i, "provenance", self._provenance(e)),
                "sink_class": self._sink(i),
                "volume": self._volume(i),
                "session_chain": self._chain(i, level),
                "security_relevant_change": self._fill(i, "security_relevant_change", self._security_change(e)),
                "cited_authorization": self._citation(i),
                "residual_risk_flag": self._fill(i, "residual_risk_flag",
                                                 Fact("none", "no flag raised", open=RISK_CATEGORIES)),
            }
            facts["envelope_permits"] = self._envelope(e, level[i].value, facts["sink_class"].value)
            ordered = {spec.name: facts[spec.name] for spec in FACTS}
            errors = validate({name: f.value for name, f in ordered.items()})
            if errors:
                raise AssertionError(f"fact builder produced an invalid record: {errors}")
            records.append(EffectFacts(i, e, ordered, covered))
        if self.fills:
            raise ValueError(f"fills for facts that are not open: {sorted(self.fills)}")
        return ActionFacts(self.request.record_id, tuple(records))

    def _fill(self, i: int, name: str, fact: Fact) -> Fact:
        fill = self.fills.pop((i, name), None)
        if fill is None:
            return fact
        if not fact.open:
            raise ValueError(f"effect {i}: {name} is not open to the fact filler")
        if fill.value not in fact.open:
            raise ValueError(f"effect {i}: {name}={fill.value!r} is not one of {fact.open}")
        return Fact(fill.value, fill.source, fill.evidence or fact.evidence, confidence=fill.confidence)

    # -- sensitivity ---------------------------------------------------------------------------

    def _data_sources(self, i: int) -> list[int]:
        """Effects whose data flows into effect i: reads, and opaque programs (unknown output)."""
        return sorted(j for j in self.upstream[i] if self.effects[j].verb is Verb.READ or self.effects[j].opaque)

    def _own_sensitivity(self, i: int) -> Fact:
        """Sensitivity of what the effect itself names: its targets, or for egress the literal payload."""
        e = self.effects[i]
        if e.opaque:
            return Fact("unknown", "opaque program: the sandbox observes what it touches")
        if e.verb is Verb.EGRESS:
            if e.note == SENDS_REPOSITORY:
                level, why = self.catalog.sensitivity("repo", None, None)
                if level is None:
                    return Fact("unknown", "repository of unknown sensitivity", open=SENSITIVITY)
                return Fact(level, why, ("the payload is the repository",))
            if e.note == SENDS_DATA and not self._data_sources(i):
                return Fact("unknown", "payload written in the command", (self.action_text[:300],), open=SENSITIVITY)
            return Fact("S0", "no payload beyond the request itself")
        if e.note == LISTING:
            return Fact("S0", "listing: names only")
        known: list[tuple[str, str, str]] = []  # (level, target, where the level came from)
        uncatalogued: list[str] = []
        for t in sorted(e.targets):
            level, why = self.catalog.sensitivity(e.kind, t, self.registry.get(e.kind, t))
            if level is None:
                uncatalogued.append(t)
            else:
                known.append((level, t, why))
        if not e.targets and not e.unresolved:
            level, why = self.catalog.sensitivity(e.kind, None, None)
            known.append((level or "S0", "(no targets)", why if level else "nothing named"))
        top = _top([k[0] for k in known], SENSITIVITY) if known else None
        if top == "S3" or (top is not None and not e.unresolved and not uncatalogued):
            hits = [k for k in known if k[0] == top]
            return Fact(top, hits[0][2], tuple(f"{t}: {why}" for _, t, why in hits))
        if e.unresolved:
            return Fact("unknown", "unresolved selectors", e.unresolved)
        floor = SENSITIVITY.index(top) if top is not None else 0
        return Fact("unknown", "uncatalogued targets", tuple(uncatalogued), open=SENSITIVITY[floor:])

    def _sensitivity(self, i: int, own: list[Fact]) -> Fact:
        """For writes and egress, also count the data flowing in and secrets written in the command."""
        e, base = self.effects[i], own[i]
        if e.verb not in (Verb.WRITE, Verb.EGRESS) or e.opaque:
            return base
        parts = [(base.value, base.source, base.evidence)]
        parts += [(own[j].value, f"data from effect {j}", (own[j].source,)) for j in self._data_sources(i)]
        if self.secrets:
            parts.append(("S3", "detector:" + ",".join(self.secrets), ("secret written in the command",)))
        for value, source, evidence in parts:
            if value == "S3":
                return Fact("S3", source, evidence)
        unknown = [p for p in parts if p[0] == "unknown"]
        if unknown:
            reopen = base.open if base.value == "unknown" else ()
            return Fact("unknown", unknown[0][1], tuple(ev for p in unknown for ev in p[2]), open=reopen)
        top = _top([p[0] for p in parts], SENSITIVITY)
        source, evidence = next((p[1], p[2]) for p in parts if p[0] == top)
        return Fact(top, source, evidence)

    # -- contract ------------------------------------------------------------------------------

    def _covered(self, e: Effect) -> frozenset[str]:
        if e.opaque:
            return frozenset()
        return frozenset(t for t in e.targets
                         if self.contract.covering_spec(e.kind, t, self.registry.get(e.kind, t), self.cwd) is not None)

    def _in_contract(self, e: Effect, covered: frozenset[str]) -> Fact:
        if e.opaque:
            return Fact("none", "opaque program: its targets are unknown")
        if not e.targets and not e.unresolved:
            return Fact("all", "no targets")
        uncovered = sorted(e.targets - covered)
        if covered and not uncovered and not e.unresolved:
            return Fact("all", "contract targets", tuple(sorted(covered)))
        evidence = tuple(f"not covered: {t}" for t in uncovered) + tuple(f"unresolved: {s}" for s in e.unresolved)
        return Fact("some" if covered else "none", "contract targets", evidence)

    def _envelope(self, e: Effect, level: FactValue, sink: FactValue) -> Fact:
        effective = level if level in SENSITIVITY else "S3"
        caveat = ("sensitivity unknown, treated as S3",) if level == "unknown" else ()
        for idx, g in enumerate(self.contract.grants):
            if not self.contract.is_active(g) or g.verb is not e.verb:
                continue
            if g.kinds and e.kind not in g.kinds:
                continue
            if e.verb in CEILING_VERBS and SENSITIVITY.index(effective) > SENSITIVITY.index(g.max_sensitivity):
                continue
            if e.verb is Verb.EGRESS and g.sinks and sink not in g.sinks:
                continue
            return Fact(True, f"grant {g.grant_id or idx} ({g.source.value}, turn {g.turn})", caveat)
        wanted = f"{e.verb.value} {e.kind} at {effective}" + (f" to {sink}" if e.verb is Verb.EGRESS else "")
        return Fact(False, f"no active grant covers {wanted}", caveat)

    # -- registry ------------------------------------------------------------------------------

    def _protected(self, e: Effect) -> Fact:
        hits = []
        for t in sorted(e.targets):
            protected, why = self.catalog.protected(e.kind, t, self.registry.get(e.kind, t))
            if protected:
                hits.append(f"{t}: {why}")
        return Fact(bool(hits), "registry and catalog", tuple(hits))

    # -- provenance ----------------------------------------------------------------------------

    def _names(self, kind: str, target: str) -> set[str]:
        """The strings that would name this target in text."""
        names = {target}
        resource = self.registry.get(kind, target)
        if resource is not None:
            names |= {v for k in NAME_ATTRS if isinstance(v := resource.attrs.get(k), str)}
        if kind == "file":
            names.add(posixpath.basename(target))
            root = (self.cwd or "").rstrip("/") + "/"
            if self.cwd and target.startswith(root):
                names.add(target[len(root):])
        if kind == "branch" and target.startswith("origin/"):
            names.add(target.removeprefix("origin/"))
        return {n for n in names if len(n) >= MIN_NAME}

    def _segment_class(self, idx: int, seg: Segment) -> str | None:
        if seg.trust is Trust.TRUSTED:
            return "user" if seg.role == "user" else "trusted_structure"
        if idx in self.contract.delegated_segments:
            return "delegated"
        if seg.role == "agent":
            return None  # the agent's own words prove nothing about where a target came from
        return "observed"

    def _provenance(self, e: Effect) -> Fact:
        if not e.targets:
            return Fact("agent_generated", "no named targets")
        origin: dict[str, tuple[str, str]] = {}
        for t in sorted(e.targets):
            names = self._names(e.kind, t)
            best, where = "agent_generated", ""
            for idx, seg in enumerate(self.request.context):
                cls = self._segment_class(idx, seg)
                if cls is None or PROVENANCE.index(cls) >= PROVENANCE.index(best):
                    continue
                if any(_mentions(seg.text, n) for n in names):
                    best, where = cls, f"segment {idx} ({seg.role})"
            origin[t] = (best, where)
        worst = _top([cls for cls, _ in origin.values()], PROVENANCE)
        evidence = tuple(f"{t}: {where or 'named nowhere in the context'}"
                         for t, (cls, where) in origin.items() if cls == worst)
        is_open = ("observed", "untrusted_instruction") if worst == "observed" else ()
        return Fact(worst, "context matching", evidence, open=is_open)

    # -- data movement -------------------------------------------------------------------------

    def _egress_sink(self, e: Effect) -> tuple[str, tuple[str, ...]]:
        if e.kind == "recipient":
            users = [s.text for s in self.request.context if s.role == "user" and s.trust is Trust.TRUSTED]
            named = bool(e.targets) and not e.unresolved and all(
                any(_mentions(text, r) for text in users) for r in e.targets)
            return ("recipient_named" if named else "recipient_unnamed"), tuple(sorted(e.targets))
        classes = [(("remote_allowlisted" if self.contract.allowlisted(h) else "remote_other"), h)
                   for h in sorted(e.targets)]
        classes += [("unknown", s) for s in e.unresolved]
        if not classes:
            classes.append(("unknown", "no destination"))
        worst = _top([c for c, _ in classes], SINK_RANK)
        return worst, tuple(f"{h}: {c}" for c, h in classes)

    def _sink(self, i: int) -> Fact:
        e = self.effects[i]
        if e.opaque:
            return Fact("unknown", "opaque program")
        if e.verb is Verb.READ:
            dests = [j for j in sorted(self.downstream[i])
                     if self.effects[j].verb in (Verb.WRITE, Verb.EGRESS) or self.effects[j].opaque]
            if not dests:
                return Fact("context", "output returns to the agent")
            sinks = [(self._destination(j), j) for j in dests]
            worst = _top([s for s, _ in sinks], SINK_RANK)
            return Fact(worst, "data flow", tuple(f"to effect {j}: {s}" for s, j in sinks))
        if e.verb is Verb.WRITE:
            return Fact("local", "written to a local file or platform resource")
        if e.verb is Verb.EGRESS:
            worst, evidence = self._egress_sink(e)
            return Fact(worst, "destination", evidence)
        if e.verb in (Verb.DELETE, Verb.CONTROL):
            return Fact("none", "moves no data")
        return Fact("unknown", "program output")

    def _destination(self, j: int) -> str:
        d = self.effects[j]
        if d.opaque:
            return "unknown"
        if d.verb is Verb.EGRESS:
            return self._egress_sink(d)[0]
        return "local"

    def _volume(self, i: int) -> Fact:
        e = self.effects[i]
        if e.opaque:
            return Fact("unknown", "opaque program")
        if e.verb is Verb.EGRESS:
            sources = self._data_sources(i)
            if not sources:
                return Fact("small", "only the request itself")
            parts = [self._volume(j).value for j in sources]
            if "unknown" in parts:
                return Fact("unknown", "payload of unknown size", tuple(f"effect {j}" for j in sources))
            return Fact(_top(parts, ("small", "large", "bulk")), "payload", tuple(f"effect {j}" for j in sources))
        if e.unresolved:
            return Fact("unknown", "unresolved selectors", e.unresolved)
        if e.kind == "file" and e.note == RECURSIVE:
            return Fact("unknown", "recursive: the size is known only at run time")
        n, expected = len(e.targets), self.contract.expected_count(e.kind)
        bucket = self.volume_buckets.bucket(n, expected)
        counted = f"{n} target" + ("" if n == 1 else "s")
        return Fact(bucket, counted + (f", {expected} expected" if expected is not None else ", no expectation"))

    def _chain(self, i: int, level: list[Fact]) -> Fact:
        e = self.effects[i]
        if e.verb is not Verb.EGRESS:
            return Fact("none", "not an egress")
        reasons = []
        entry = self.ledger.highest_read()
        if entry is not None:
            reasons.append(f"{entry.target} ({entry.level}) read in {entry.record_id}")
        for j, other in enumerate(self.effects):
            if j != i and (j < i or j in self.upstream[i]) and other.verb is Verb.READ and level[j].value in SENSITIVE:
                reasons.append(f"effect {j} read {level[j].value} data in this action")
        if reasons:
            return Fact("sensitive_read_then_egress", "session ledger", tuple(reasons))
        return Fact("none", "no sensitive read before this egress")

    # -- content -------------------------------------------------------------------------------

    def _security_change(self, e: Effect) -> Fact:
        if e.verb not in (Verb.WRITE, Verb.DELETE) or e.kind != "file":
            return Fact("none", "not a file change")
        if e.command in PERMISSION_COMMANDS:
            return Fact("permissions", f"command: {e.command}")
        found: list[tuple[str, str]] = []
        unknown: list[str] = []
        for t in sorted(e.targets):
            category, why = Catalog.security_category(t)
            if category is None:
                unknown.append(t)
            elif category != "none":
                found.append((category, f"{t}: {why}"))
        if found:
            best = min((c for c, _ in found), key=SECURITY_PRIORITY.index)
            return Fact(best, "catalog", tuple(why for c, why in found if c == best))
        if e.unresolved:
            return Fact("unknown", "unresolved selectors", e.unresolved)
        if unknown:
            candidates = tuple(c for c in SECURITY_CHANGES if c != "unknown")
            return Fact("unknown", "no rule knows these files; the filler reads the diff", tuple(unknown), open=candidates)
        return Fact("none", "catalog")

    def _citation(self, i: int) -> Fact:
        fill = self.fills.pop((i, "cited_authorization"), None)
        if fill is None:
            return Fact("none", "no citation", open=("verified",))
        if fill.value != "verified":
            raise ValueError(f"effect {i}: cited_authorization can only be filled with 'verified'")
        quote = fill.evidence[0] if fill.evidence else ""
        where = verify_citation(quote, self.request.context)
        if where is None:
            return Fact("none", f"{fill.source}: citation rejected, the quote is not in a trusted user turn", (quote,))
        return Fact("verified", fill.source, (quote, f"segment {where}"), confidence=fill.confidence)
