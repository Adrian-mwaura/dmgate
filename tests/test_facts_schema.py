import json

from dmgate.facts.schema import FACTS, SCHEMA_PATH, input_schema, validate


def _valid_record():
    return {spec.name: False if spec.domain is None else spec.domain[0] for spec in FACTS}


def test_every_fact_has_a_finite_domain():
    for spec in FACTS:
        assert spec.domain is None or len(set(spec.domain)) == len(spec.domain) > 0


def test_a_complete_record_validates():
    assert validate(_valid_record()) == []


def test_validate_reports_missing_unknown_and_out_of_domain_facts():
    values = _valid_record()
    del values["op"]
    values["colour"] = "red"
    values["volume"] = "huge"
    values["opaque"] = "yes"
    errors = validate(values)
    assert "missing fact op" in errors
    assert "unknown fact colour" in errors
    assert "volume='huge' is outside its domain" in errors
    assert "opaque='yes' is outside its domain" in errors


def test_only_model_facts_are_fillable():
    assert {s.name for s in FACTS if s.llm} == {
        "max_sensitivity", "provenance", "security_relevant_change", "cited_authorization", "residual_risk_flag",
    }


def test_json_schema_mirrors_the_specs():
    effect = input_schema()["properties"]["effects"]["items"]
    assert effect["required"] == [s.name for s in FACTS]
    assert effect["additionalProperties"] is False
    assert effect["properties"]["max_sensitivity"]["enum"] == ["S0", "S1", "S2", "S3", "unknown"]
    assert effect["properties"]["opaque"]["type"] == "boolean"


def test_committed_json_schema_is_current():
    # If this fails, run: python -m dmgate.facts.schema --write
    assert json.loads(SCHEMA_PATH.read_text(encoding="utf-8")) == input_schema()
