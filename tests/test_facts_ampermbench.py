"""The fact builder on all 2,560 AmPermBench probes, with the oracle's contract.

This checks plumbing (set comparison, selectors, registry, schema), not behavior: with a perfect
contract, every effect's targets are in the contract exactly when the probe is benign. Nothing here
is tuned on the benchmark.
"""

import pytest

from dmgate.datasets.ampermbench import FAMILIES, default_root, load_family, oracle_contract, probe_records
from dmgate.effects import resolve_shell
from dmgate.facts.builder import build_facts
from dmgate.facts.schema import validate

pytestmark = pytest.mark.skipif(
    not (default_root() / "tasks").is_dir(), reason="AmPermBench checkout missing (see CLAUDE.md)"
)


@pytest.fixture(scope="module")
def families():
    return {family: load_family(default_root(), family) for family in FAMILIES}


def test_oracle_contract_reproduces_every_probe_label(families):
    mismatches = []
    for record in probe_records():
        request = record.gate_input()
        contract = oracle_contract(families[record.family], record.cluster_id)
        facts = build_facts(request, resolve_shell(request.action.args["command"], request.environment), contract)
        effects = [e.values() for e in facts.effects]
        assert effects and all(validate(v) == [] for v in effects), record.record_id
        assert all(v["envelope_permits"] for v in effects), record.record_id

        # Labels are about what the action changes (a remote delete also talks to the git remote).
        changing = [v for v in effects if v["op"] in ("write", "delete", "control")]
        inside = all(v["targets_in_contract"] == "all" for v in changing)
        if inside == record.label.should_block:
            mismatches.append(record.record_id)
        assert all(v["max_sensitivity"] == "S1" for v in changing), record.record_id

        protected = {r["id"] for r in request.environment["resources"] if r["attrs"].get("protected")}
        assert any(v["has_protected_target"] for v in effects) == bool(record.label.targets & protected)
    assert mismatches == []
