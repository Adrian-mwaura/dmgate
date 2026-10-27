# METADATA
# title: Invariants (DESIGN.md §6.6, layer 1)
# description: |
#   Admin-owned floors. Each hit sets a minimum outcome for one effect. Nothing in the soft layer,
#   cited authorization included, can bring an effect below its floor. Edited by admins only;
#   the lifecycle loop never patches this file.
#
#   Rules read only facts declared in schema/facts.schema.json (checked by `opa check --schema`).
package dmgate.invariants

sensitive := {"S2", "S3"}

destructive := {"delete", "control"}

# Sinks the request did not approve of.
exposed := {"remote_other", "recipient_unnamed", "unknown"}

default sandbox := false

sandbox := data.dmgate.config.sandbox

hits contains {
	"effect": i, "id": "INV-1", "outcome": "deny",
	"reason": "S2+ data would leave toward a destination the request did not name",
} if {
	some i, e in input.effects
	e.op == "egress"
	e.max_sensitivity in sensitive
	e.targets_in_contract != "all"
}

hits contains {
	"effect": i, "id": "INV-2", "outcome": "deny",
	"reason": "an instruction found in untrusted content asks for a destructive, egress or sensitive operation",
} if {
	some i, e in input.effects
	e.provenance == "untrusted_instruction"
	risky_for_injection(e)
}

hits contains {
	"effect": i, "id": "INV-3", "outcome": "deny",
	"reason": "a protected resource would be deleted or controlled",
} if {
	some i, e in input.effects
	e.has_protected_target
	e.op in destructive
}

hits contains {
	"effect": i, "id": "INV-4", "outcome": "ask",
	"reason": "a protected resource would be written",
} if {
	some i, e in input.effects
	e.has_protected_target
	e.op == "write"
}

hits contains {
	"effect": i, "id": "INV-5", "outcome": "ask",
	"reason": "a destructive operation has targets that could not be resolved",
} if {
	some i, e in input.effects
	e.unresolved
	e.op in destructive
}

hits contains {
	"effect": i, "id": "INV-6", "outcome": "ask",
	"reason": "an opaque program cannot be judged without the sandbox",
} if {
	some i, e in input.effects
	e.opaque
	not sandbox
}

hits contains {
	"effect": i, "id": "INV-7", "outcome": "ask",
	"reason": "egress to an unapproved destination after sensitive data was read",
} if {
	some i, e in input.effects
	e.session_chain == "sensitive_read_then_egress"
	e.sink_class in exposed
	e.targets_in_contract != "all"
}

hits contains {
	"effect": i, "id": "INV-8", "outcome": "ask",
	"reason": "a secret would leave the platform",
} if {
	some i, e in input.effects
	e.op == "egress"
	e.max_sensitivity == "S3"
}

hits contains {
	"effect": i, "id": "INV-9", "outcome": "ask",
	"reason": "data of unknown sensitivity would go to an unapproved destination",
} if {
	some i, e in input.effects
	e.max_sensitivity == "unknown"
	e.sink_class in exposed
	e.op in {"read", "egress"}
}

risky_for_injection(e) if e.op in destructive

risky_for_injection(e) if e.op == "egress"

risky_for_injection(e) if e.max_sensitivity in sensitive
