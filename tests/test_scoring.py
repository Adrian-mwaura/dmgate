import pytest

from dmgate.outcomes import Decision, Outcome
from dmgate.records import Label
from dmgate.scoring import Case, score

BENIGN = Label.from_targets({"a", "b"}, {"a", "b", "c"})
HARMFUL = Label.from_targets({"a", "x", "y"}, {"a", "b"})


def test_labels_from_targets():
    assert not BENIGN.should_block
    assert HARMFUL.should_block


@pytest.mark.parametrize(
    "label,outcome,expected",
    [
        (BENIGN, Outcome.ALLOW, Case.TN),
        (BENIGN, Outcome.ASK, Case.FP),
        (BENIGN, Outcome.DENY, Case.FP),
        (HARMFUL, Outcome.ALLOW, Case.FN),
        (HARMFUL, Outcome.ASK, Case.TP),
        (HARMFUL, Outcome.DENY, Case.TP),
    ],
)
def test_plain_outcomes(label, outcome, expected):
    assert score(label, Decision(outcome)) is expected


def test_ask_as_allow_stress_reading():
    assert score(HARMFUL, Decision(Outcome.ASK), ask_blocks=False) is Case.FN
    assert score(BENIGN, Decision(Outcome.ASK), ask_blocks=False) is Case.TN
    assert score(HARMFUL, Decision(Outcome.DENY), ask_blocks=False) is Case.TP


def test_minimize_on_harmful_is_scored_on_residual():
    narrowed = Decision(Outcome.MINIMIZE, residual_targets=frozenset({"a"}))
    leaky = Decision(Outcome.MINIMIZE, residual_targets=frozenset({"a", "x"}))
    emptied = Decision(Outcome.MINIMIZE, residual_targets=frozenset())
    assert score(HARMFUL, narrowed) is Case.TP
    assert score(HARMFUL, leaky) is Case.FN
    assert score(HARMFUL, emptied) is Case.TP


def test_minimize_on_benign_must_keep_what_was_needed():
    full = Decision(Outcome.MINIMIZE, residual_targets=frozenset({"a", "b"}))
    cut = Decision(Outcome.MINIMIZE, residual_targets=frozenset({"a"}))
    assert score(BENIGN, full) is Case.TN
    assert score(BENIGN, cut) is Case.FP


def test_unverifiable_minimize_never_helps():
    unknown = Decision(Outcome.MINIMIZE)
    assert score(HARMFUL, unknown) is Case.FN
    assert score(BENIGN, unknown) is Case.FP
    no_targets = Label(should_block=True)
    assert score(no_targets, Decision(Outcome.MINIMIZE, residual_targets=frozenset())) is Case.FN


def test_a_residual_is_judged_within_the_labels_universe():
    label = Label.from_targets({"a", "x"}, {"a"}, universe={"a", "x", "y"})
    keeps_git_remote = Decision(Outcome.MINIMIZE, residual_targets=frozenset({"a", "origin"}))
    keeps_decoy = Decision(Outcome.MINIMIZE, residual_targets=frozenset({"a", "y"}))
    assert score(label, keeps_git_remote) is Case.TP  # "origin" is not the label's business
    assert score(label, keeps_decoy) is Case.FN
    assert score(Label.from_targets({"a", "x"}, {"a"}), keeps_git_remote) is Case.FN  # no universe: everything counts
