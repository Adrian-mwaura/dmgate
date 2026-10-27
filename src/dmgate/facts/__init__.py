"""Facts: the typed, finite-domain description of an action that the rules read (DESIGN.md §5.3, §6.4).

- `dmgate.facts.schema`: the fact schema and its JSON Schema export.
- `dmgate.facts.catalog`: sensitivity, protection and security-relevance rules, secret detectors.
- `dmgate.facts.ledger`: what a session has already read.
- `dmgate.facts.builder`: effects + contract + catalogs + ledger -> one fact record per effect.
"""
