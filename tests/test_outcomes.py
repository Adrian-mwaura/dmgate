import dataclasses

import pytest

from dmgate.outcomes import Decision, Outcome


def test_outcomes_are_ordered_by_restrictiveness():
    assert Outcome.ALLOW < Outcome.MINIMIZE < Outcome.ASK < Outcome.DENY
    assert max(Outcome.MINIMIZE, Outcome.ASK) is Outcome.ASK


def test_decision_round_trips_through_a_dict():
    decision = Decision(
        Outcome.MINIMIZE,
        residual_targets=frozenset({"b", "a"}),
        reasons=("narrowed to authorized targets",),
        trace={"rule": "soft-12"},
    )
    data = decision.to_dict()
    assert data["outcome"] == "MINIMIZE"
    assert data["residual_targets"] == ["a", "b"]  # sorted, so output is deterministic
    assert Decision.from_dict(data) == decision


def test_missing_residual_stays_missing():
    data = Decision(Outcome.DENY).to_dict()
    assert data["residual_targets"] is None
    assert Decision.from_dict(data).residual_targets is None


def test_decisions_are_immutable():
    decision = Decision(Outcome.ALLOW)
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.outcome = Outcome.DENY