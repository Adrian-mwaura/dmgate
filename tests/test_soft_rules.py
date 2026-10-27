"""The soft-rule table and its geometry. No OPA needed: this is the Python side."""

import math

import pytest

from dmgate.facts.schema import FACTS
from dmgate.outcomes import Outcome
from dmgate.rules.soft import SoftTable, cells, compare, describe, domain, loosening, representative

TABLE = SoftTable.load()

BASE = {
    "op": "read", "opaque": False, "unresolved": False, "max_sensitivity": "S0", "targets_in_contract": "all",
    "has_protected_target": False, "envelope_permits": True, "provenance": "user", "sink_class": "context",
    "volume": "small", "session_chain": "none", "security_relevant_change": "none",
    "cited_authorization": "none", "residual_risk_flag": "none",
}


def point(**changes):
    return {**BASE, **changes}


def table(*rows):
    return SoftTable.from_dict({"rows": [
        {"id": i, "when": when, "outcome": outcome, "reason": "test"} for i, when, outcome in rows
    ]})


def test_the_shipped_table_is_valid():
    assert len(TABLE.rows) >= 15 and TABLE.version


@pytest.mark.parametrize("row,message", [
    ({"id": "X", "when": {"opp": ["read"]}, "outcome": "allow", "reason": "r"}, "unknown fact"),
    ({"id": "X", "when": {"op": []}, "outcome": "allow", "reason": "r"}, "allows no values"),
    ({"id": "X", "when": {"op": ["erase"]}, "outcome": "allow", "reason": "r"}, "outside its domain"),
    ({"id": "X", "when": {"opaque": ["yes"]}, "outcome": "allow", "reason": "r"}, "outside its domain"),
    ({"id": "X", "when": {}, "outcome": "permit", "reason": "r"}, "unknown outcome"),
    ({"id": "X", "when": {}, "outcome": "allow", "reason": ""}, "needs a reason"),
])
def test_rows_that_rego_would_silently_ignore_are_rejected(row, message):
    with pytest.raises(ValueError, match=message):
        SoftTable.from_dict({"rows": [row]})


def test_duplicate_ids_are_rejected():
    row = {"id": "X", "when": {}, "outcome": "allow", "reason": "r"}
    with pytest.raises(ValueError, match="duplicate"):
        SoftTable.from_dict({"rows": [row, row]})


def test_most_restrictive_match_wins_and_unmatched_asks():
    t = table(("A", {"op": ["read"]}, "allow"), ("B", {"sink_class": ["context"]}, "minimize"))
    assert t.decide(point()) is Outcome.MINIMIZE
    assert t.decide(point(sink_class="local")) is Outcome.ALLOW
    assert t.decide(point(op="write", sink_class="local")) is Outcome.ASK


# -- geometry -----------------------------------------------------------------------------------

def test_cells_partition_the_whole_fact_space():
    space = math.prod(len(domain(spec)) for spec in FACTS)
    assert sum(math.prod(len(v) for v in c.values()) for c in cells(TABLE)) == space


def test_a_table_decides_each_cell_alike():
    # Every point of a cell gets the cell's decision: check the first and last value of each class.
    for c in list(cells(TABLE))[::97]:
        first = representative(c)
        last = {fact: values[-1] for fact, values in c.items()}
        assert TABLE.decide(first) is TABLE.decide(last), describe(c)


def test_a_table_does_not_differ_from_itself():
    assert compare(TABLE, TABLE) == []


def test_adding_an_allow_row_over_unmatched_space_loosens_exactly_there():
    old = table(("D", {"op": ["delete"], "targets_in_contract": ["all"]}, "allow"))
    new = table(("D", {"op": ["delete"], "targets_in_contract": ["all"]}, "allow"),
                ("P", {"op": ["delete"], "targets_in_contract": ["some"]}, "allow"))
    found = loosening(old, new)
    assert len(found) == 1
    assert describe(found[0].cell) == "op = delete, targets_in_contract = some"
    assert (found[0].old, found[0].new) == (Outcome.ASK, Outcome.ALLOW)
    assert found[0].points == math.prod(len(domain(s)) for s in FACTS) // (6 * 3)


def test_adding_a_restrictive_row_never_loosens():
    old = TABLE
    new = SoftTable(TABLE.rows + table(("T", {"provenance": ["observed"], "op": ["delete"]}, "deny")).rows)
    assert compare(old, new) and loosening(old, new) == []


# -- properties of the shipped table (policy review, not tuning) --------------------------------

def _soft_cells(**constraints):
    for c in cells(TABLE):
        p = representative(c)
        if all(p[k] in v for k, v in constraints.items()):
            yield c, TABLE.decide(p)


def test_no_destructive_effect_outside_the_request_is_allowed_or_narrowed():
    for c, outcome in _soft_cells(op={"delete", "control"}, targets_in_contract={"none"}):
        assert outcome >= Outcome.ASK, describe(c)


def test_partially_requested_destruction_is_at_least_narrowed():
    for c, outcome in _soft_cells(op={"delete", "control"}, targets_in_contract={"some"}):
        assert outcome >= Outcome.MINIMIZE, describe(c)


def test_egress_to_unnamed_unapproved_destinations_is_never_allowed():
    for c, outcome in _soft_cells(op={"egress"}, targets_in_contract={"some", "none"},
                                  sink_class={"remote_other", "recipient_unnamed", "unknown"}):
        assert outcome >= Outcome.ASK, describe(c)


def test_unreviewed_writes_wait_for_review():
    for c, outcome in _soft_cells(op={"write"}, security_relevant_change={"unknown"}):
        assert outcome >= Outcome.ASK, describe(c)
