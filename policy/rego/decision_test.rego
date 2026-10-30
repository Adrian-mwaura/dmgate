# Rego unit tests for the composition: `opa test policy/rego`. The pytest suite runs them too.
package dmgate.decision_test

import data.dmgate.decision

# A harmless effect: reading a public file the user named, output back to the agent.
base := {
	"op": "read", "opaque": false, "unresolved": false, "max_sensitivity": "S0",
	"targets_in_contract": "all", "has_protected_target": false, "target_class": "workspace",
	"envelope_permits": true,
	"provenance": "user", "sink_class": "context", "volume": "small", "session_chain": "none",
	"security_relevant_change": "none", "cited_authorization": "none", "residual_risk_flag": "none",
}

allow_everything := [{"id": "T-ALLOW", "when": {}, "outcome": "allow", "reason": "test"}]

one(e) := {"record_id": "t", "effects": [e]}

test_no_effects_allows if {
	decision.result.outcome == "allow" with input as {"record_id": "t", "effects": []}
}

test_an_effect_no_row_matches_needs_a_person if {
	r := decision.result with input as one(base) with data.dmgate.soft.rows as []
	r.outcome == "ask"
	r.effects[0].soft_rows == []
}

test_the_most_restrictive_matching_row_wins if {
	rows := [
		{"id": "A", "when": {"op": ["read"]}, "outcome": "allow", "reason": "a"},
		{"id": "B", "when": {"sink_class": ["context"]}, "outcome": "minimize", "reason": "b"},
		{"id": "C", "when": {"op": ["write"]}, "outcome": "deny", "reason": "c"},
	]
	r := decision.result with input as one(base) with data.dmgate.soft.rows as rows
	r.outcome == "minimize"
	r.effects[0].soft_rows == ["A", "B"]
}

test_soft_rows_cannot_go_below_an_invariant if {
	e := object.union(base, {"op": "delete", "has_protected_target": true})
	r := decision.result with input as one(e) with data.dmgate.soft.rows as allow_everything
	r.outcome == "deny"
	r.effects[0].invariants == ["INV-3"]
}

test_citation_lowers_the_soft_layer_only if {
	asked := object.union(base, {"op": "delete", "targets_in_contract": "none", "cited_authorization": "verified"})
	rows := [{"id": "ASK", "when": {"op": ["delete"]}, "outcome": "ask", "reason": "x"}]
	lowered := decision.result with input as one(asked) with data.dmgate.soft.rows as rows
	lowered.outcome == "allow"

	floored := object.union(asked, {"has_protected_target": true})
	kept := decision.result with input as one(floored) with data.dmgate.soft.rows as rows
	kept.outcome == "deny"
}

test_residual_risk_flag_is_shadowed_until_live if {
	flagged := object.union(base, {"max_sensitivity": "S2", "residual_risk_flag": "sensitive"})
	shadow := decision.result with input as one(flagged) with data.dmgate.soft.rows as allow_everything
	shadow.outcome == "allow"
	shadow.effects[0].risk_flag_corroborated == true

	live := decision.result with input as one(flagged)
		with data.dmgate.soft.rows as allow_everything
		with data.dmgate.config.residual_risk_live as true
	live.outcome == "ask"
}

test_an_uncorroborated_flag_does_nothing if {
	flagged := object.union(base, {"residual_risk_flag": "sensitive"}) # S0 data: nothing sensitive
	r := decision.result with input as one(flagged)
		with data.dmgate.soft.rows as allow_everything
		with data.dmgate.config.residual_risk_live as true
	r.outcome == "allow"
}

test_the_action_takes_its_most_restrictive_effect if {
	risky := object.union(base, {"op": "egress", "max_sensitivity": "S3", "targets_in_contract": "none"})
	r := decision.result with input as {"record_id": "t", "effects": [base, risky]}
		with data.dmgate.soft.rows as allow_everything
	[d.outcome | some d in r.effects] == ["allow", "deny"]
	r.outcome == "deny"
}

test_opaque_programs_wait_for_the_sandbox if {
	e := object.union(base, {"op": "exec", "opaque": true})
	without := decision.result with input as one(e) with data.dmgate.soft.rows as allow_everything
	without.outcome == "ask"
	with_sandbox := decision.result with input as one(e)
		with data.dmgate.soft.rows as allow_everything
		with data.dmgate.config.sandbox as true
	with_sandbox.outcome == "allow"
}
