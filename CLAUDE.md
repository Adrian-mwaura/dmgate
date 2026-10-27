# DMGate

Huawei Challenge 5: agent data minimization via model–rule fusion. The architecture, agreed
decisions, open questions and build plan live in `docs/DESIGN.md`; read it before changing anything.

## Working agreement

- Design changes are debated with the user one position at a time and agreed before code.
  Record outcomes in `docs/DESIGN.md` §12 (decision log); keep unresolved points in §11.
- The challenge is a research direction: prefer novel mechanisms, but report numbers strictly (§8).
- **Never simplify the project** to fit current experience. Keep the full design and bring the user
  up to speed. The user is fluent in Python and new to pytest, git, Docker, tree-sitter, Rego and
  vLLM: teach those from first principles when they come up.

## How we build (since 2026-10-26)

- **Claude builds, then teaches.** Claude writes the code and tests for each milestone, runs them,
  and commits on `main` (pushing to the user's private GitHub backup). Afterwards Claude walks the
  user through what was built: concepts first, then the code, with exercises the user runs.
- The user asks questions and can request changes at any point; design decisions still go through
  debate first (above).
- Claude maintains `docs/DESIGN.md` and this file.
- Reference prototype for P0 and M1: `C:\Users\adria\OneDrive\Desktop\Huawei Challenge` (read only).

## Rules for code

- Gates receive a `GateInput` only, never an `ActionRecord`: labels must not leak into decisions.
  The same holds for `oracle_contract` (built from ground truth): tests and teaching only.
- The rules read only schema facts (`dmgate.facts.schema`); every fact is always present, and
  `unknown` is a value, not a missing key.
- AmPermBench (and anything derived from it, including the probe set) is test-only: never tune
  prompts, rules or exemplars on it.
- `external/` holds untrusted third-party checkouts. Read their data files; do not import their
  code or run their scripts.
- Python 3.11+, stdlib first; add a dependency only when it earns its place.

## Commands (PowerShell, from the project folder)

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\dmgate-eval.exe --dataset ampermbench-probes --gate allow-all
.\.venv\Scripts\python.exe -m dmgate.explain --prompt CA-S2-B2-R0 --command "aws s3 rm s3://bench-artifacts/dev/alex/ --recursive"
.\.venv\Scripts\python.exe -m dmgate.facts.schema --write   # after changing the fact schema
```

AmPermBench data is read from `external/cc-auto-mode-measurement` (override with `AMPERMBENCH_DIR`):

```powershell
git clone --depth 1 https://github.com/yan5ui/cc-auto-mode-measurement external/cc-auto-mode-measurement
```
