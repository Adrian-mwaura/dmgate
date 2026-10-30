"""The FS/DB dev benchmark: structure, oracle self-consistency, labels, and resolver recovery (M4)."""

import itertools
from collections import Counter
from pathlib import Path

import pytest

from dmgate.datasets import fsdb
from dmgate.effects import Registry
from dmgate.effects.database import find_snapshot
from dmgate.facts.builder import build_facts
from dmgate.records import dump_jsonl, load_jsonl
from dmgate.rules.gate import resolve_action

PROMPTS = fsdb.prompts()
RECORDS = fsdb.probe_records()


CANDIDATE_FAMILIES = [p for p in PROMPTS if not p.prompt_id.startswith("FX")]  # the T/P/F candidate structure


def test_seven_families_each_a_full_4x4x2_matrix():
    assert len(PROMPTS) == 224 and len({p.prompt_id for p in PROMPTS}) == 224
    for code in fsdb.FAMILIES:
        cells = Counter((p.s, p.b, p.r) for p in PROMPTS if p.prompt_id.startswith(code))
        assert set(cells) == set(itertools.product(*fsdb.AXES.values())) and set(cells.values()) == {1}


def test_prompts_vary_one_slot_at_a_time():
    by_id = {p.prompt_id: p.text for p in PROMPTS}
    for p in PROMPTS:
        code = p.prompt_id[:2]
        for axis, values in fsdb.AXES.items():
            for other in values:
                ids = {"S": p.s, "B": p.b, "R": p.r, axis: other}
                neighbour = by_id[f"{code}-{ids['S']}-{ids['B']}-{ids['R']}"]
                assert (neighbour == p.text) == (other == {"S": p.s, "B": p.b, "R": p.r}[axis])
    s0 = next(p for p in PROMPTS if p.prompt_id == "FL-S0-B0-R0").text
    assert "delete the stale debug log `/srv/staging/logs/sam/debug-4812.log`" in s0 and "staging lane" in s0


def _attributes(prompt):
    """Each candidate's attributes read back from the environment (registry or database), not from
    the generator's own tables: the second, independent source the oracle is checked against."""
    env = fsdb.ENVIRONMENTS[prompt.r]
    registry = Registry.from_environment(env)
    code = prompt.prompt_id[:2]
    out = {}
    for item in prompt.items.values():
        if code.startswith("F"):
            a = registry.get("file", item.target).attrs
            main = fsdb._FILE_LAYOUT[code][3]
            out[item.target] = (a["owner"], a["stale"], a["category"] == main, a["ticket"])
        else:
            layout = fsdb.TABLES[code]
            v = registry.get("row", item.target).attrs["values"]
            out[item.target] = (v[layout.owner_col], bool(v[layout.stale_col]), v[layout.category_col] == layout.main,
                                v["ticket"])
    return out


def test_the_oracle_follows_from_the_request_meaning():
    for p in CANDIDATE_FAMILIES:
        derived = {t for t, attrs in _attributes(p).items() if fsdb.selects(p.b, *attrs)}
        main = {t for t in p.authorized if t in _attributes(p)}
        assert main == derived, p.prompt_id
        assert len(main) == (1 if p.b in ("B0", "B1") else 3)


def test_oracle_sets_are_consistent():
    for p in PROMPTS:
        assert p.authorized and p.must_preserve and p.authorized <= p.universe and p.must_preserve <= p.universe
        assert not p.authorized & p.must_preserve
        assert p.protected <= p.must_preserve and not p.authorized & p.protected
        assert len(p.protected) == (0 if p.prompt_id.startswith("FX") else 2 if p.r == "R1" else 1)


def test_protected_decoys_are_protected_in_the_environment():
    for p in PROMPTS:
        registry = Registry.from_environment(fsdb.ENVIRONMENTS[p.r])
        for target in p.protected:
            kind = "file" if target.startswith("/") else "row"
            assert registry.get(kind, target).attrs["protected"] is True, target


def test_labels():
    assert len(RECORDS) > 3000
    for r in RECORDS:
        assert r.label.should_block == (not r.label.targets <= r.label.authorized)
        assert r.label.targets <= r.label.universe, r.record_id
    by_prompt = Counter((r.cluster_id, r.label.should_block) for r in RECORDS)
    for p in PROMPTS:
        assert by_prompt[(p.prompt_id, False)] >= 1 and by_prompt[(p.prompt_id, True)] >= 1, p.prompt_id


def test_resolvers_recover_every_probe_target():
    """Construction and resolution agree on all probes: shell, SQL tool, dry-runs, globs, uploads."""
    for r in RECORDS:
        res = resolve_action(r.gate_input())
        assert res.exact and not res.opaque, r.record_id
        found = set().union(*(e.targets for e in res.effects)) & r.label.universe
        assert found == r.label.targets, r.record_id


def test_records_round_trip_through_jsonl(tmp_path: Path):
    sample = [r for r in RECORDS if r.axes["probe"] in ("predicate", "glob", "star")][:12]
    dump_jsonl(sample, tmp_path / "probes.jsonl")
    for original, loaded in zip(sample, load_jsonl(tmp_path / "probes.jsonl")):
        assert loaded.label == original.label
        a, b = resolve_action(original.gate_input()), resolve_action(loaded.gate_input())
        assert [e.targets for e in a.effects] == [e.targets for e in b.effects]


def test_snapshots_hold_the_lane_data():
    staging = find_snapshot(fsdb.ENVIRONMENTS["R0"], "/srv/staging/app.db")
    assert staging.query("SELECT COUNT(*) FROM customers") == [(9,)]
    assert staging.tables["audit_log"].protected


def test_the_coding_task_authorizes_the_workspace_and_names_only_the_tests():
    """Re-derived from the catalog: the benign targets are exactly the universe's workspace-class
    paths under the project, and the request names only the failing test(s)."""
    from dmgate.facts.catalog import Catalog

    for p in (p for p in PROMPTS if p.prompt_id.startswith("FX")):
        env = fsdb.ENVIRONMENTS[p.r]
        catalog, registry = Catalog.from_environment(env), Registry.from_environment(env)
        workspace = {t for t in p.universe if t.startswith(f"{env['cwd']}/app")
                     and catalog.target_class("file", t, registry.get("file", t))[0] == "workspace"}
        assert p.authorized == workspace, p.prompt_id
        assert p.named <= p.authorized and {t.rsplit("/", 1)[-1] for t in p.named} <= {"test_dates.py", "test_timezones.py"}
        assert len(p.named) == (1 if p.b in ("B0", "B1") else 2)


@pytest.mark.parametrize("code", [c for c in fsdb.FAMILIES if c != "FX"])
def test_with_the_oracle_contract_coverage_matches_every_label(code):
    """Plumbing: given a perfect contract, every effect's labeled targets are inside it exactly when
    the probe is benign. Not tuning, and not a result (the contract comes from ground truth). The
    coding family is excluded on purpose: its benign probes pass because the workspace is free
    (policy), not because the contract covers them; its check is the gate bound (test_rule_gate)."""
    contracts = {p.prompt_id: fsdb.oracle_contract(p) for p in PROMPTS if p.prompt_id.startswith(code)}
    for r in RECORDS:
        if r.cluster_id not in contracts:
            continue
        request = r.gate_input()
        facts = build_facts(request, resolve_action(request), contracts[r.cluster_id])
        labeled = [e for e in facts.effects if e.effect.targets & r.label.universe]
        inside = all(e.facts["targets_in_contract"].value == "all" for e in labeled)
        assert inside == (not r.label.should_block), r.record_id
