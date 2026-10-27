"""The Rego policies, run by the real OPA binary (skipped when it is missing)."""

import itertools
import random

import pytest

from dmgate.facts.schema import BY_NAME
from dmgate.rules.opa import POLICY_DIR, check, find_opa, run_opa
from dmgate.rules.soft import SoftTable, cells, describe, domain, representative

needs_opa = pytest.mark.skipif(find_opa() is None, reason="OPA binary missing (see CLAUDE.md)")

TABLE = SoftTable.load()
RANK = {"allow": 0, "minimize": 1, "ask": 2, "deny": 3}
ALLOW_EVERYTHING = [{"id": "T-ALLOW", "when": {}, "outcome": "allow", "reason": "test"}]
CONFIG = {"sandbox": False, "residual_risk_live": False}

BASE = {
    "op": "read", "opaque": False, "unresolved": False, "max_sensitivity": "S0", "targets_in_contract": "all",
    "has_protected_target": False, "envelope_permits": True, "provenance": "user", "sink_class": "context",
    "volume": "small", "session_chain": "none", "security_relevant_change": "none",
    "cited_authorization": "none", "residual_risk_flag": "none",
}


def evaluate(server, points, rows=None, config=CONFIG):
    """Decisions for many effects. Batched: each batch is one "action" with many effects."""
    server.put_data("dmgate/soft", {"rows": TABLE.to_data() if rows is None else rows})
    server.put_data("dmgate/config", config)
    out = []
    for start in range(0, len(points), 50):
        reply = server.query("dmgate/decision/result", {"record_id": "batch", "effects": points[start:start + 50]})
        out += reply["effects"]
    return out


# -- tooling ------------------------------------------------------------------------------------

@needs_opa
def test_policies_pass_strict_check_against_the_fact_schema():
    result = check()
    assert result.returncode == 0, result.stdout + result.stderr


@needs_opa
def test_the_schema_check_catches_a_misspelled_fact(tmp_path):
    (tmp_path / "typo.rego").write_text('package typo\n\nbad contains i if {\n\tsome i, e in input.effects\n'
                                        '\te.opp == "read"\n}\n', encoding="utf-8")
    from dmgate.rules.opa import SCHEMA_FILE
    result = run_opa("check", "--strict", "--schema", str(SCHEMA_FILE), str(tmp_path))
    assert result.returncode != 0 and "undefined ref: e.opp" in result.stdout + result.stderr


@needs_opa
def test_policies_are_formatted():
    result = run_opa("fmt", "--list", str(POLICY_DIR))
    assert result.returncode == 0 and result.stdout.strip() == "", result.stdout


@needs_opa
def test_rego_unit_tests_pass():
    result = run_opa("test", str(POLICY_DIR))
    assert result.returncode == 0, result.stdout + result.stderr


# -- invariants ---------------------------------------------------------------------------------

@pytest.mark.parametrize("invariant,outcome,changes", [
    ("INV-1", "deny", {"op": "egress", "max_sensitivity": "S2", "targets_in_contract": "none"}),
    ("INV-2", "deny", {"op": "delete", "provenance": "untrusted_instruction"}),
    ("INV-3", "deny", {"op": "control", "has_protected_target": True}),
    ("INV-4", "ask", {"op": "write", "has_protected_target": True}),
    ("INV-5", "ask", {"op": "delete", "unresolved": True}),
    ("INV-6", "ask", {"op": "exec", "opaque": True}),
    ("INV-7", "ask", {"op": "egress", "session_chain": "sensitive_read_then_egress", "sink_class": "remote_other"}),
    ("INV-8", "ask", {"op": "egress", "max_sensitivity": "S3", "targets_in_contract": "all"}),
    ("INV-9", "ask", {"max_sensitivity": "unknown", "sink_class": "unknown"}),
])
def test_each_invariant_fires_and_sets_its_floor(opa_server, invariant, outcome, changes):
    decided = evaluate(opa_server, [{**BASE, **changes}], rows=ALLOW_EVERYTHING)[0]
    assert invariant in decided["invariants"]
    assert decided["floor"] == outcome and decided["outcome"] == outcome


def test_a_harmless_effect_hits_no_invariant(opa_server):
    decided = evaluate(opa_server, [BASE])[0]
    assert decided["invariants"] == [] and decided["outcome"] == "allow"


# -- the floor guarantee (M3 validation) ----------------------------------------------------------

def _enumeration():
    """Every combination of the facts the invariants read most, with the other facts cycled through
    their domains, plus a seeded random sample of the whole space."""
    core = ["op", "max_sensitivity", "targets_in_contract", "provenance", "has_protected_target",
            "unresolved", "opaque", "cited_authorization"]
    rest = [f for f in BASE if f not in core]
    points = []
    for n, combo in enumerate(itertools.product(*(domain(BY_NAME[f]) for f in core))):
        p = dict(zip(core, combo))
        for k, fact in enumerate(rest):
            values = domain(BY_NAME[fact])
            p[fact] = values[(n * (2 * k + 3)) % len(values)]
        points.append(p)
    rng = random.Random(20261027)
    for _ in range(4000):
        points.append({f: rng.choice(domain(BY_NAME[f])) for f in BASE})
    return points


POINTS = _enumeration()


@pytest.mark.parametrize("risk_live", [False, True])
def test_no_soft_row_can_go_below_an_invariant_floor(opa_server, risk_live):
    """The loosest possible soft layer (every row allows, and citations lower it further) still
    cannot bring any effect below its floor: final = max(floor, risk) exactly."""
    config = {"sandbox": False, "residual_risk_live": risk_live}
    decided = evaluate(opa_server, POINTS, rows=ALLOW_EVERYTHING, config=config)
    assert len(decided) == len(POINTS) > 12_000
    for p, d in zip(POINTS, decided):
        assert d["soft_after_citation"] == "allow"
        expected = max(RANK[d["floor"]], RANK[d["risk"]])
        assert RANK[d["outcome"]] == expected, (p, d)


def test_composition_with_the_shipped_table(opa_server):
    decided = evaluate(opa_server, POINTS)
    for p, d in zip(POINTS, decided):
        assert RANK[d["outcome"]] == max(RANK[d["floor"]], RANK[d["soft_after_citation"]], RANK[d["risk"]]), p
        assert RANK[d["outcome"]] >= RANK[d["floor"]]
        if p["cited_authorization"] == "verified":
            assert d["soft_after_citation"] == "allow"
        else:
            assert d["soft_after_citation"] == d["soft"]


def test_rego_and_python_agree_on_every_cell_of_the_shipped_table(opa_server):
    """Differential test: the Rego matcher and dmgate.rules.soft decide every cell alike, so the
    geometry the lifecycle loop relies on describes what OPA actually does."""
    all_cells = list(cells(TABLE))
    reps = [representative(c) for c in all_cells]
    decided = evaluate(opa_server, reps)
    for c, p, d in zip(all_cells, reps, decided):
        assert d["soft"] == TABLE.decide(p).name.lower(), describe(c)
        assert d["soft_rows"] == [row.row_id for row in TABLE.matching(p)], describe(c)
