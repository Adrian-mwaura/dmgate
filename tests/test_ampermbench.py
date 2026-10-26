import dataclasses
from collections import Counter

import pytest

from dmgate.datasets.ampermbench import FAMILIES, default_root, load_family, probe_records
from dmgate.gate import ConstantGate, OracleGate
from dmgate.harness import run
from dmgate.outcomes import Outcome
from dmgate.records import ActionRecord, GateInput
from dmgate.report import summarize

pytestmark = pytest.mark.skipif(
    not (default_root() / "tasks").is_dir(), reason="AmPermBench checkout missing (see CLAUDE.md)"
)


@pytest.fixture(scope="module")
def records():
    return probe_records()


def test_all_families_have_32_prompts():
    for family in FAMILIES:
        data = load_family(default_root(), family)
        assert len(data.prompts) == 32
        assert data.user == "alex"


def test_probe_set_shape(records):
    assert len({r.record_id for r in records}) == len(records)
    assert len({r.cluster_id for r in records}) == 128
    counts = Counter(r.label.should_block for r in records)
    assert counts[False] > 0 and counts[True] > 0


def test_labels_follow_the_oracle(records):
    for r in records:
        assert r.label.should_block == (not r.label.targets <= r.label.authorized)
        attrs = {e["id"]: e["attrs"] for e in r.environment["resources"]}
        for target in r.label.targets:
            if attrs[target].get("protected"):
                assert r.label.should_block, r.record_id


def test_gate_input_carries_no_label(records):
    fields = {f.name for f in dataclasses.fields(GateInput)}
    assert "label" not in fields
    assert "label" not in str(records[0].gate_input().environment.keys())


def test_records_round_trip(records):
    for r in records[:50]:
        assert ActionRecord.from_dict(r.to_dict()) == r


def test_baselines_and_oracles_score_as_expected(records):
    allow, _ = run(ConstantGate(Outcome.ALLOW), records)
    s = summarize(allow, n_boot=50)["headline"]
    assert s["FPR"] == 0.0 and s["FNR"] == 1.0

    deny, _ = run(ConstantGate(Outcome.DENY), records)
    s = summarize(deny, n_boot=50)["headline"]
    assert s["FPR"] == 1.0 and s["FNR"] == 0.0

    labels = {r.record_id: r.label for r in records}
    for gate in (OracleGate(labels), OracleGate(labels, minimize=True)):
        results, _ = run(gate, records)
        s = summarize(results, n_boot=50)["headline"]
        assert s["FPR"] == 0.0 and s["FNR"] == 0.0, gate.name

    minimized, _ = run(OracleGate(labels, minimize=True), records)
    assert any(r.outcome is Outcome.MINIMIZE for r in minimized)
