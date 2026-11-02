import json

from dmgate.llm.client import CachedClient, ScriptedClient
from dmgate.llm.merge_cache import merge

SCHEMA = {"type": "object", "properties": {"level": {"type": "string"}}, "required": ["level"]}


def _ask(path, question, answer):
    client = CachedClient(ScriptedClient(lambda m, s, n: {"level": answer}), path)
    client.complete_json([{"role": "user", "content": question}], SCHEMA)


def test_merging_keeps_every_answer_from_both_machines(tmp_path):
    a, b = tmp_path / "a" / "m.jsonl", tmp_path / "b" / "m.jsonl"
    _ask(a, "shared", "S1")
    _ask(a, "only on a", "S2")
    _ask(b, "shared", "S1")
    _ask(b, "only on b", "S0")
    assert merge(b, a) == (1, 1, 0)
    assert merge(b, a) == (0, 2, 0)  # merging twice adds nothing
    replay = CachedClient(ScriptedClient(lambda m, s, n: {"level": "never asked"}), a)
    for question, answer in (("shared", "S1"), ("only on a", "S2"), ("only on b", "S0")):
        got = replay.complete_json([{"role": "user", "content": question}], SCHEMA)
        assert got.cached and got.valid == [{"level": answer}]


def test_a_conflicting_answer_is_reported_and_the_existing_one_kept(tmp_path):
    a, b = tmp_path / "a" / "m.jsonl", tmp_path / "b" / "m.jsonl"
    _ask(a, "q", "S1")
    _ask(b, "q", "S3")
    assert merge(b, a) == (0, 0, 1)
    (entry,) = [json.loads(line) for line in a.read_text(encoding="utf-8").splitlines()]
    assert entry["raw"] == ['{"level": "S1"}']


def test_merging_into_a_missing_file_copies_it(tmp_path):
    a, b = tmp_path / "new" / "m.jsonl", tmp_path / "b" / "m.jsonl"
    _ask(b, "q", "S0")
    assert merge(b, a) == (1, 0, 0) and a.is_file()
