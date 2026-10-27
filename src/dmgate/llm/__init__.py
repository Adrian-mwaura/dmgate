"""The LLM layer (DESIGN.md §6.1, §6.5): the model is a sensor, never the judge.

- `dmgate.llm.client`: an OpenAI-compatible client with constrained JSON output (vLLM structured
  outputs), plus a disk cache for reproducible evaluation and a scripted client for tests.
- `dmgate.llm.compiler`: user turns + a description of the platform's resources -> a contract.
- `dmgate.llm.filler`: answers the facts the builder left open, only within their candidates.
- `dmgate.llm.evaluate`: per-fact accuracy on the FS/DB dev benchmark (`dmgate-llm-eval`).

Every model output is an enum or a quoted span, and code checks every quote, so untrusted text
can at most move a fact within the values the builder allowed.
"""
