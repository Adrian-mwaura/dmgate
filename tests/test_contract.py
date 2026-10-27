import pytest

from dmgate.contract import Condition, Contract, Grant, GrantSource, Lifetime, TargetSource, TargetSpec
from dmgate.effects import Resource, Verb


def test_default_lifetimes_follow_position_3():
    assert Grant(Verb.READ).lifetime is Lifetime.SESSION
    assert Grant(Verb.WRITE).lifetime is Lifetime.SESSION  # local writes are reversible in the sandbox
    assert Grant(Verb.DELETE).lifetime is Lifetime.TURN
    assert Grant(Verb.CONTROL).lifetime is Lifetime.TURN
    assert Grant(Verb.EGRESS, source=GrantSource.ANSWERED_ASK).lifetime is Lifetime.TURN
    assert Grant(Verb.EGRESS, source=GrantSource.DEFAULT_PROFILE).lifetime is Lifetime.SESSION


def test_a_user_turn_cannot_grant_session_long_deletes():
    with pytest.raises(ValueError):
        Grant(Verb.DELETE, lifetime=Lifetime.SESSION)


def test_grants_reject_unknown_levels_and_sinks():
    with pytest.raises(ValueError):
        Grant(Verb.READ, max_sensitivity="S9")
    with pytest.raises(ValueError):
        Grant(Verb.EGRESS, sinks=frozenset({"the moon"}))


def test_one_turn_grants_expire_unless_carried_forward():
    read = Grant(Verb.READ, turn=0)
    delete = Grant(Verb.DELETE, frozenset({"branch"}), turn=0)
    contract = Contract(turn=0, grants=(read, delete))
    assert contract.active_grants() == (read, delete)

    assert contract.advance(1).active_grants() == (read,)

    carried = contract.advance(1, carry_forward=True)
    assert [g.verb for g in carried.active_grants()] == [Verb.READ, Verb.DELETE]
    assert carried.grants[1].turn == 1

    with pytest.raises(ValueError):
        contract.advance(0)


def test_carry_forward_only_carries_the_previous_turn():
    stale = Grant(Verb.DELETE, turn=0)
    contract = Contract(turn=1, grants=(stale,))
    assert contract.active_grants() == ()
    assert contract.advance(2, carry_forward=True).grants == ()


def test_target_specs_match_by_id_and_by_selector():
    alex = Resource("b1", "branch", {"owner": "alex", "stale": True, "name": "alex/auth-4812"})
    sam = Resource("b2", "branch", {"owner": "sam", "stale": True, "name": "sam/auth-4812"})
    mine = TargetSpec("branch", where=(
        Condition("owner", "eq", "alex"), Condition("stale", "eq", True), Condition("name", "contains", "4812"),
    ))
    assert mine.matches("b1", alex)
    assert not mine.matches("b2", sam)
    assert TargetSpec("branch", ids=frozenset({"b2"})).matches("b2", sam)
    assert TargetSpec("branch", where=(Condition("owner", "in", ["sam", "kim"]),)).matches("b2", sam)
    assert TargetSpec("branch", where=(Condition("id", "contains", "b"),)).matches("b9")
    assert not TargetSpec("branch", where=(Condition("owner", "eq", "alex"),)).matches("b9")


def test_file_specs_resolve_relative_paths_against_the_cwd():
    spec = TargetSpec("file", ids=frozenset({"src/app.py"}), patterns=("tests/*.py",))
    assert spec.matches("/work/src/app.py", cwd="/work")
    assert spec.matches("/work/tests/test_x.py", cwd="/work")
    assert not spec.matches("/work/src/util.py", cwd="/work")


def test_target_specs_need_a_criterion():
    with pytest.raises(ValueError):
        TargetSpec("file")
    with pytest.raises(ValueError):
        TargetSpec("file", ids=frozenset({"a"}), expected_count=0)
    with pytest.raises(ValueError):
        Condition("owner", "like", "a%")


def test_expected_count_and_allowlist():
    two_specs = Contract(
        targets=(TargetSpec("job", ids=frozenset({"j1"}), expected_count=1),
                 TargetSpec("job", ids=frozenset({"j2"}), expected_count=2)),
        egress_allowlist=frozenset({"*.pythonhosted.org", "pypi.org"}),
    )
    assert two_specs.expected_count("job") == 3
    assert two_specs.expected_count("branch") is None
    assert Contract(targets=(TargetSpec("job", ids=frozenset({"j1"})),)).expected_count("job") is None
    assert two_specs.allowlisted("files.pythonhosted.org")
    assert two_specs.allowlisted("pypi.org")
    assert not two_specs.allowlisted("pypi.org.evil.example")


def test_contract_round_trips_through_a_dict():
    contract = Contract(
        turn=2,
        grants=(Grant(Verb.DELETE, frozenset({"object"}), turn=2, grant_id="g1"),
                Grant(Verb.READ, max_sensitivity="S2", source=GrantSource.DEFAULT_PROFILE)),
        targets=(TargetSpec("object", where=(Condition("owner", "eq", "alex"), Condition("ns", "in", ["dev"])),
                            source=TargetSource.DELEGATED, expected_count=1),),
        egress_allowlist=frozenset({"pypi.org"}),
        delegated_segments=frozenset({3}),
    )
    assert Contract.from_dict(contract.to_dict()) == contract


def test_conditions_compare_and_read_nested_row_values():
    row = Resource("users/7", "row", {"table": "users", "values": {"owner": "sam", "created": "2023-05-01", "age": 40}})
    old_test_accounts = TargetSpec("row", where=(
        Condition("table", "eq", "users"), Condition("values.owner", "eq", "sam"),
        Condition("values.created", "lt", "2024-01-01"),
    ))
    assert old_test_accounts.matches("users/7", row)
    assert not TargetSpec("row", where=(Condition("values.age", "gt", 40),)).matches("users/7", row)
    assert TargetSpec("row", where=(Condition("values.age", "ge", 40),)).matches("users/7", row)
    assert not TargetSpec("row", where=(Condition("values.owner", "lt", 3),)).matches("users/7", row)  # str < int
    assert not TargetSpec("row", where=(Condition("values.missing", "eq", None),)).matches("users/7", row)
