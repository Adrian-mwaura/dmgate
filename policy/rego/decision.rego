# METADATA
# title: Composition (DESIGN.md §6.6, layer 3)
# description: |
#   Fixed, admin-owned. For each effect:
#
#     soft     = the most restrictive matching soft row (policy/soft_rules.json), or "ask" if none
#                matches. Soft rows are boxes in the fact space; the lifecycle loop patches them.
#     soft'    = "allow" if cited_authorization is verified, else soft. Citations lower soft only.
#     risk     = "ask" if the residual-risk flag is live and a risk fact corroborates it.
#     final    = max(invariant floor, soft', risk)
#
#   The action's outcome is the most restrictive of its effects' outcomes ("allow" when it has no
#   effects). Query data.dmgate.decision.result.
package dmgate.decision

import data.dmgate.invariants

rank := {"allow": 0, "minimize": 1, "ask": 2, "deny": 3}

names := ["allow", "minimize", "ask", "deny"]

# The soft outcome when no row matches: an effect nobody wrote a rule for needs a person.
unmatched := "ask"

default rows := []

rows := data.dmgate.soft.rows

# Shadow mode: the residual-risk flag is recorded in the trace but changes nothing until an admin
# turns it on (position 2).
default risk_live := false

risk_live := data.dmgate.config.residual_risk_live

matches(row, e) if {
	every fact, allowed in row.when {
		e[fact] in allowed
	}
}

highest(ranks) := max(array.concat([0], ranks))

soft_rank(matched) := highest([rank[r.outcome] | some r in matched]) if count(matched) > 0

soft_rank(matched) := rank[unmatched] if count(matched) == 0

after_citation(e, _) := 0 if e.cited_authorization == "verified"

after_citation(e, soft) := soft if e.cited_authorization != "verified"

corroborated(e) if {
	e.residual_risk_flag == "sensitive"
	e.max_sensitivity in invariants.sensitive
}

corroborated(e) if {
	e.residual_risk_flag == "destructive"
	e.op in invariants.destructive
}

corroborated(e) if {
	e.residual_risk_flag == "egress"
	e.op == "egress"
}

corroborated(e) if {
	e.residual_risk_flag == "untrusted_provenance"
	e.provenance in {"observed", "agent_generated", "untrusted_instruction"}
}

default flagged(_) := false

flagged(e) if corroborated(e)

default risk_rank(_) := 0

risk_rank(e) := rank.ask if {
	risk_live
	corroborated(e)
}

decide(i, e) := {
	"effect": i,
	"outcome": names[final],
	"floor": names[floor],
	"soft": names[soft],
	"soft_after_citation": names[lowered],
	"risk": names[risk],
	"risk_flag_corroborated": flagged(e),
	"invariants": [h.id | some h in floor_hits],
	"soft_rows": [r.id | some r in matched],
	"reasons": array.concat([h.reason | some h in floor_hits], [r.reason | some r in matched]),
} if {
	floor_hits := [h | some h in invariants.hits; h.effect == i]
	floor := highest([rank[h.outcome] | some h in floor_hits])
	matched := [r | some r in rows; matches(r, e)]
	soft := soft_rank(matched)
	lowered := after_citation(e, soft)
	risk := risk_rank(e)
	final := highest([floor, lowered, risk])
}

effects := [decide(i, e) | some i, e in input.effects]

result := {
	"outcome": names[highest([rank[d.outcome] | some d in effects])],
	"effects": effects,
}
