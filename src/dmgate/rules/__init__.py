"""The rule engine (DESIGN.md §6.6): three layers, evaluated by OPA.

- `policy/rego/invariants.rego`: admin floors (layer 1).
- `policy/soft_rules.json`: the soft-rule table (layer 2); `dmgate.rules.soft` loads, validates and
  analyzes it as boxes in the fact space.
- `policy/rego/decision.rego`: the fixed composition (layer 3).
- `dmgate.rules.opa`: runs OPA as a localhost sidecar.
- `dmgate.rules.gate`: the gate that joins effects, facts and the policy into a Decision.
"""
