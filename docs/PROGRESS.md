# DMGate: progress report

Huawei Trustworthiness Challenge 5, "Agent Data Minimization via Model–Rule Fusion".
Status as of 3 November 2026. The full design, every decision and its evidence are in
[DESIGN.md](DESIGN.md); this report summarizes them.

## 1. Summary

**The problem.** An AI agent (a coding assistant, a personal assistant) acts on files and databases
on a user's behalf. It should touch only the data the user's request needs, even when it is overeager
or when untrusted content (a file, a web page, a ticket) tries to steer it. The challenge asks for a
gate that judges each data operation the agent attempts, combining a general-purpose language model
with deterministic rules, at a false-positive rate below 0.1% and a false-negative rate below 5%.

**Our approach.** The language model never decides. It does two narrow jobs: it translates the user's
request into a *contract* (which operations on which targets the user asked for), and it answers a few
questions a program cannot (how sensitive is this unknown file?). Everything else is deterministic:
a resolver works out what an action would actually touch, a fact builder turns that into a fixed set
of facts, and rules written in Rego decide **Allow, Minimize, Ask or Deny**, with a trace that says why.

**Where we stand.** The deterministic part is complete and verified: given a perfect contract, the
rules make **no errors** on either benchmark (0% / 0%). With real models writing the contract, we are
far from the targets. On our development benchmark (3,616 actions), the best configurations block
13–29% of legitimate actions and let 11–18% of harmful ones through (Section 7.2). All model results
so far are on development data; the test benchmark (AmPermBench) has not yet been run with a model.

**What we learned.**

1. **The contract compiler is the bottleneck.** The rules lose nothing; nearly all errors come from
   what the model writes into the contract, mostly loose descriptions (`where` conditions, file
   patterns) that cover targets the user never meant.
2. **A bigger model is not a safer gate.** Moving from 4B to 27B parameters blocked far fewer
   legitimate actions but let about three times as many harmful ones through: the larger model reads
   requests more generously.
3. **Model-supplied evidence must be bound to what it authorizes.** A "cited authorization" (a quote
   of the user that unblocks an action) was checked only for existing; stronger models quoted the
   real request to approve actions on other targets. We made citations *target-bound*, which removed
   most of that harm (27B: 32.8% → 18.0% of harmful actions let through).
4. **Contracts cannot say "not this".** When a request names something to leave alone, the models put
   it where names go: into the contract's targets (so it gets covered) or into a condition that,
   lacking a "not equal", turns the exclusion into its opposite.

## 2. The challenge, as we read it

| Requirement (challenge text) | Our answer |
|---|---|
| Fine-grained judgment of filesystem and database operations, metadata and content | Each action is resolved to its effects on concrete files, tables, columns and rows; sensitivity per target |
| Hybrid model–rule orchestration; the rule engine enforces explainable boundaries | The model supplies typed facts only; three layers of Rego rules decide; every decision carries a trace |
| Continuous lifecycle evolution (rules adjusted, rule→model feedback) | Planned: an abductive loop that attributes each error to a fact (model) or a rule (patch) |
| General-purpose foundation models, minimal parameter scale, no specialized security models | Open general models from 4B to 27B parameters, measured as a size ladder |
| FPR < 0.1%, FNR < 5%, self-verified on open datasets | AmPermBench (the dataset the challenge cites) as the test set; our own FS/DB benchmark for development |

We treat the challenge as a research direction: the targets are what we measure against, and novel
mechanisms are preferred over narrow compliance. Numbers are reported strictly, with 95% confidence
intervals, and development data is kept apart from test data.

## 3. Architecture

```
 user request ──► contract compiler (LLM) ──► contract: what was asked for (grants, targets)
                                                        │
 agent action ──► effect resolver ──► effects ──► fact builder ◄── catalogs (sensitivity, ownership)
                  (deterministic)                      │
                                                       ▼
                                      fact filler (LLM): only facts code cannot settle
                                                       │
                                                       ▼
                         rules (Rego/OPA): admin floor ⊕ soft table ⊕ fixed composition
                                                       │
                                                       ▼
                          Allow / Minimize / Ask / Deny + decision trace
```

- **Outcomes** are ordered, `Allow < Minimize < Ask < Deny`. Minimize runs a narrowed version (fewer
  rows, columns or files); Ask pauses for the user. An action's outcome is the strictest outcome of
  its effects.
- **Effects.** A shell command or SQL statement becomes a list of effects: a verb (read, write,
  delete, control, egress, exec) on concrete targets, plus the data flows between them. Shell commands
  are parsed with tree-sitter; SQL with sqlglot, with a dry run against a read-only snapshot to find
  the exact rows a `WHERE` clause touches. Anything that cannot be resolved is marked unresolved or
  opaque, never guessed.
- **Contract.** Two layers: an *envelope* (kinds of permission: read up to a sensitivity level,
  delete, upload) and *targets* (which files, rows or hosts). Every item must quote the user's own
  words; only trusted input (the user's turns) can widen permissions, never tool output.
- **Facts.** Fifteen facts per effect, each from a finite set of values (for example
  `targets_in_contract ∈ {all, some, none}`, sensitivity S0–S3). Every fact is always present;
  "unknown" is a value the rules must handle.
- **Rules,** in three layers: admin-owned **invariants** set a floor no other layer can lower (nine
  of them, e.g. "sensitive data to a destination the user never named → Deny"); a **soft table** of
  rows tuned on development data; a fixed **composition** that takes the strictest. The model never
  produces a verdict.

Where the model is used: the contract compiler (one call per request) and the fact filler (only when
a fact is open). Everything else is code.

## 4. What was built

| Milestone | Commit |
|---|---|
| P0: evaluation harness: record format, scoring, confidence intervals, reports; AmPermBench probe set | `18c7fd2` |
| M1: shell effect resolver (tree-sitter); resolves all 2,560 AmPermBench probe commands exactly | `51ca519` |
| M2: fact schema, contract model, deterministic fact builder | `418e53a` |
| M3: rule engine on OPA (invariants, soft table, composition) | `2a04b5d` |
| M4: SQL resolver with dry run; the FS/DB development benchmark | `336c297` |
| M5: LLM layer (contract compiler, fact filler), built against scripted models | `e2782e9` |
| Q12: record-aware minimization of reads and writes; named destinations | `c65cf46` |
| Evaluation on the campus GPUs; parallel evaluation | `0dbcdb1`, `98978ce` |
| First model measurements; error analysis; a third, larger model | `3e8af60`, `784e0f1` |
| Q3: target-bound citations | `299a2d9` |
| Negation variant of the benchmark; runs across several servers | `1cf1b61`, `8f6df59` |

The code has 303 automated tests (`pytest`), the Rego policies have their own tests and a strict
type check against the fact schema, and every model answer is cached so that any run can be replayed
exactly on a laptop without a GPU. Not built yet: the transactional sandbox, the Minimize executor,
value handles, the lifecycle loop and the agent runs (Section 6).

**Benchmarks.**

- **AmPermBench** (the challenge's cited dataset; MIT-licensed): 128 prompts on four DevOps tasks
  along three axes (how explicit the request is, how the target is named, how much is at risk). It
  ships prompts and deterministic labels but no recorded agent runs. We use it **only as a test set**
  and have built 2,560 labelled probe actions (320 legitimate, 2,240 harmful) from it.
- **FS/DB benchmark** (ours, development data): the same three axes over filesystem and database
  tasks, seven families (delete logs, read tickets, share reports, delete accounts, update orders,
  query customers, and a coding task), 224 prompts and 3,616 probe actions (880 legitimate, 2,736
  harmful). Each prompt has the same structure of targets the user may mean and decoys a careless
  agent might hit (the user's still-active item, a colleague's item, a protected shared item). Labels
  are deterministic and checked against the environment independently.
- **Negation variant** (Section 7.6): 192 prompts that each add one sentence naming a decoy to leave
  alone; 3,104 probes.

## 5. Design decisions and changes

Decisions were debated one at a time and recorded with their evidence (DESIGN §12).

**Foundations (22–26 Oct).**

- Ask counts as blocking; a Minimize is scored on what still executes.
- The language model supplies facts, not verdicts, plus two special facts: a cited authorization
  (can lower an outcome) and a residual-risk flag (can only raise one, off by default).
- Contracts separate the envelope (permissions) from targets; tool output can never widen
  permissions.
- Rules in Rego with three layers; the soft rules as a data table, so they can be compared and
  patched safely.
- Planned components agreed then: a transactional sandbox, value handles with graded disclosure, an
  abductive learning loop.

**Q12: what the rules missed (30 Oct).** With a perfect contract, the first rules still let
over-reads and over-writes through, because any read or write inside the envelope was allowed.
Measured with the oracle contract before deciding:

| Gap | Before | Decision | After |
|---|---|---|---|
| Reading records the request does not name | FNR 46.0% | **A5:** a new fact, `target_class`; reads of *records* outside the contract are narrowed (Minimize) or asked; workspace files stay free to explore | 0.0% |
| Writing records the request does not name | FNR 13.0% | **B3:** the same for writes of records; workspace writes stay free | 0.0% |
| Uploading to the host the request names | FPR 15.2% | **C2:** a `remote_named` destination class, for hosts the contract names | 0.0% |

The cost of narrowing was priced with a new coding family: 0 of 352 legitimate exploration actions
were asked under A5 + B3, against 352 of 352 under the stricter alternative.

**Infrastructure (27 Oct–2 Nov).** The models are open and self-hosted (Qwen family, Apache-2.0),
served with vLLM on the university's shared GPU servers (RTX 3090 and RTX 4090). The agent model for
end-to-end runs is also self-hosted (Q7). A rented cloud GPU was tried first and dropped.

**Model choice within the challenge's rules (2 Nov).** The challenge requires general-purpose models
of minimal parameter scale. Our ladder: Qwen3.5-4B, Qwen3.5-9B, and Qwen3.8-27B (the smallest model
of the newest Qwen generation, Apache-2.0). Security-tuned guard models are excluded by the rules.

**Q3: cited authorization (2 Nov).** Decided **B, target-bound citations**, over keeping it as built,
recompiling through the citation, lowering only one step, or switching citations off. Evidence in
Section 7.4.

**Open (to debate next).** Q15, bounding what a contract can cover; Q16, letting a contract say
"not this" (Section 8).

## 6. Novel mechanisms

| Mechanism | What it is | Status |
|---|---|---|
| Intent contracts | The request compiled into an envelope (permissions, with lifetimes: delete and upload grants last one user turn) and targets; tool output never widens permissions | Built, measured |
| Quote grounding | Every item the model puts in a contract must quote the user; code checks the quote is in a user turn, and one-turn permissions must quote the latest turn | Built, measured |
| Target-bound cited authorization | A quote may unblock an action only if it names every target of that action, by a name no other resource shares | Built, measured (Q3) |
| Facts with finite domains | The model fills typed facts, never verdicts; "unknown" is a value | Built |
| Three-layer rules with an admin floor | Invariants no soft rule or citation can lower; checked by exhaustive enumeration (about 12,600 fact combinations) | Built, verified |
| Exact comparison of rule tables | Soft rows are boxes in the fact space; the rows split it into finitely many cells (4,320 for the first table) on which every table is constant, so a change can be classified as loosening or tightening exactly | Built |
| Class-gated minimization | Reads and writes of records outside the contract are narrowed or asked; workspace files stay free | Built, measured (oracle contract) |
| SQL dry run | A statement's `WHERE` clause is run as a key query on a read-only snapshot, so the gate knows the exact rows | Built, verified |
| Replayable evaluation | Every model answer is cached by the exact request; runs replay bit for bit without a GPU; caches from several servers merge by request | Built |
| FS/DB benchmark with a negation variant | Development data on AmPermBench's three axes, deterministic labels | Built |
| Transactional sandbox | Opaque programs run in an overlay and are judged on what they actually did | Planned (P2) |
| Value handles | The model sees `⟦h:7f3a aws_secret len=40⟧` instead of a secret, at a chosen disclosure level | Planned (P3) |
| Abductive lifecycle loop | Each error is attributed to a fact (model update) or a rule (patch); loosening patches need a precision bound | Planned (P4) |

## 7. Experiments and outcomes

All model numbers below are on **development data** (the FS/DB benchmark). FPR is the share of
legitimate actions blocked (Ask or Deny); FNR the share of harmful actions allowed. Brackets are 95%
confidence intervals; for FNR, resampled by prompt, since actions from one prompt are not
independent. Models served with vLLM 0.31.0; Qwen3.5 on one RTX 3090, Qwen3.8-27B on two.

### 7.1 The rules, with a perfect contract

| Gate | FS/DB (3,616 actions) FPR / FNR | AmPermBench (2,560) FPR / FNR |
|---|---|---|
| Admin profile only, no request understood | 60.0% / 0.0% | 100% / 0.0% |
| Oracle contract (perfect) | **0.0% / 0.0%** | **0.0% / 0.0%** |

The rules lose nothing a perfect contract knows. Everything between these two rows is the language
model's job.

### 7.2 The model ladder

With target-bound citations (the current gate), all 3,616 FS/DB actions:

| Model | FPR | FNR | Compiler: contracts exactly right (224 prompts) | Sensitivity accuracy (99 items) |
|---|---|---|---|---|
| Qwen3.5-4B | 29.1% [26.1, 32.2] | 11.3% [8.8, 13.7] | 21.0% | 64.6% |
| Qwen3.5-9B (FP8 weights) | 24.0% [21.2, 26.9] | 11.5% [9.0, 14.3] | 27.2% | 65.7% |
| Qwen3.8-27B (FP8) | 13.0% [10.8, 15.4] | 18.0% [14.8, 21.5] | 24.6% | 69.7% |

Runs `20261102-0852-measure` and `20261102-1053-measure` (FPR/FNR replayed with the current gate).
Most blocking is done by Ask: counting Ask as allow, 78–81% of harmful actions would pass for every
model. Far from the targets of 0.1% and 5%.

### 7.3 Where the errors come from

- **The compiler.** Contracts exactly matched the oracle's for only 21–27% of prompts. Typical
  mistakes: granting a verb the request did not ask for (`exec` because the request mentions the
  shell); taking files for database rows; a file pattern with no wildcard; mapping an indirect request
  ("could use some attention") to the wrong verb.
- **Over-coverage.** Of the targets a request does not authorize that the contract still covered,
  83% (4B), 98% (9B) and 99% (27B) were covered by `where` conditions or patterns the model wrote,
  such as "owner is sam and stale", which also catches the user's other stale logs. A wrongly listed
  id accounts for only 1–6%.
- **Sensitivity** errors lean cautious for the 4B and 9B (about half of internal files called
  confidential; the 27B: 19%); rating something too low happened for 4–11% of items.

### 7.4 Cited authorization (Q3)

When the soft rules held an action back, the filler could quote the user to unblock it; the check
confirmed only that the quote existed. Example: the user asked to delete test account
`test-4812@staging.example`; the 9B quoted that sentence to approve deleting a *different* row.

| Model | Harmful actions let through by a citation | Legitimate actions rightly unblocked by one |
|---|---|---|
| 4B | 5 | 1 of 880 |
| 9B | 118 | 1 |
| 27B | 422 | 41 |

The 9B's higher FNR than the 4B (+1.5 to +7.5 points, paired by prompt) came entirely from
citations. Each alternative was rescored from the recorded rule results; the method reproduced all
3,616 recorded outcomes exactly for each model before being trusted.

| Model | As built (FPR / FNR) | **Target-bound** | Citations off |
|---|---|---|---|
| 4B | 29.1 / 11.4 | 29.1 / 11.3 | 29.2 / 11.2 |
| 9B | 24.0 / 15.8 | 24.0 / 11.5 | 24.1 / 11.5 |
| 27B | 11.7 / 32.8 | **13.0 / 18.0** | 16.4 / 17.4 |

Target-bound citations keep most of the benefit and remove most of the harm. Implemented in the fact
builder; a replay with the new code matched the rescoring on every one of 3,616 actions per model.

### 7.5 Scale: a bigger model is not a safer gate

Against the 9B, paired by prompt, the 27B (before Q3's fix) let 13.2 to 21.1 points more harmful
actions through and blocked 7.8 to 17.4 points fewer legitimate ones. Its contracts covered more
targets (449 harmful actions let through by the contract, against 288 for the 9B) and it cited more
freely. The more capable model reads requests more generously; whatever the model is allowed to
*widen* (coverage, citations) has to be bounded by the rules.

### 7.6 Negation: "leave X alone"

Each prompt of the negation variant adds one sentence naming a decoy to exclude ("Leave `X` alone.",
"Don't touch `X`.", "Keep `X` as it is.", "Do not include `X`."). With the oracle contract the rules
still score 0% / 0%. On the 432 harmful actions that target the named decoy:

| Model | Let through, without the sentence | With it | Via a target-bound citation |
|---|---|---|---|
| 4B | 20.4% | **32.6%** | 1 |
| 9B | 23.4% | **27.8%** | 0 |
| 27B | 35.4% | 33.1% | 10 |

Target-bound citations have a small polarity gap (a quote like "Don't touch `X`" names X), but the
larger effect is in the compiler: models kept the exclusion out of the contract in only 38% (4B), 58%
(9B) and 31% (27B) of prompts. Otherwise they listed the excluded item as a target, so it was covered,
or wrote it into a condition, where without a "not equal" it became its opposite: "leave the live
account alone" became `email = <the live account>`, which uncovered what the user did ask for. That
explains 149 of the 27B's 196 newly blocked legitimate actions (its FPR on these families rose from
21.6% to 57.6%).

### 7.7 Engineering results that mattered for measurement

- **Throughput.** The evaluation first sent one request at a time (21 answers a minute, the GPU
  mostly idle). Parallel requests raised that to about 640 a minute; decisions and reports are
  identical to sequential runs.
- **Exact replay.** Every number above replays on a laptop from the answer cache, bit for bit;
  checked for every run.
- **Serving issues found and handled:** vLLM's FlashInfer sampler needs a compiler the servers lack
  (switched to vLLM's own sampler, recorded in each run's manifest); FP8 models run on the 3090 with
  weight-only kernels; the 27B's hybrid layers need a cap on concurrent sequences; a rare vLLM
  grammar-engine error is retried, then recorded as "no answer" (which fails closed), so one bad
  request no longer aborts a run.

### 7.8 Limitations

- All model numbers are development data, on prompts our rules and compiler may be tuned on. The
  test set has not been run with a model.
- We are far from the targets: 13–29% FPR and 11–18% FNR against 0.1% and 5%.
- Statistical power: even a perfect gate on AmPermBench's 320 legitimate probe actions could only
  show FPR ≤ 0.93%; demonstrating 0.1% needs a corpus of 10,000+ legitimate actions from real agent
  runs, not built yet.
- The probe actions are constructed, not produced by an agent; end-to-end runs (M6) will show which
  overreaches agents actually attempt.
- The 27B's negation run used different hardware (RTX 4090, native FP8) from its base run (RTX 3090);
  47 of its 196 new false alarms are not explained by the mechanism above.

## 8. Next steps

1. **Debate Q15 and Q16 together:** what a contract can express. Options include a closed-world
   compiler (code proposes the real candidates, the model only selects), conditions limited to the
   environment's attributes and values, conditions that narrow rather than widen, and an explicit,
   grounded "excluded" list the rules enforce. Each can be measured first by replay, without a GPU.
2. **Compiler prompt work** on the FS/DB data (allowed there): files versus rows, wildcards, indirect
   verbs, uploads.
3. **Settle the hardware question** by rerunning the 27B's negation prompts on the 3090.
4. **M6: agent runs.** An open agent model (Qwen3.5-35B-A3B) in OpenHands on AmPermBench's tasks,
   gate in log-only mode, producing real action trajectories, then the first test-set numbers.
5. **Later phases:** the transactional sandbox (P2), the Minimize executor and value handles (P3),
   and the abductive lifecycle loop (P4).
