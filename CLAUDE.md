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

- Claude supplies the code in small chunks: one function at a time for subtle logic, a whole
  section for routine code. Each chunk comes with: why it exists, the exact file and where it goes,
  the code, the key lines explained, and a check to run.
- **The user pastes the code** into the files, runs the checks and makes the commits.
- Claude does not write source, test or config files. When the user gives permission, Claude reads
  the pasted files and reports mistakes as findings; it fixes them only if the user asks.
- Claude maintains `docs/DESIGN.md` and this file.
- Reference prototype for P0 and M1: `C:\Users\adria\OneDrive\Desktop\Huawei Challenge` (read only).

## Rules for code

- Gates receive a `GateInput` only, never an `ActionRecord`: labels must not leak into decisions.
- AmPermBench (and anything derived from it, including the probe set) is test-only: never tune
  prompts, rules or exemplars on it.
- `external/` holds untrusted third-party checkouts. Read their data files; do not import their
  code or run their scripts.
- Python 3.11+, stdlib first; add a dependency only when it earns its place.

## Commands (PowerShell, from the project folder)

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```
