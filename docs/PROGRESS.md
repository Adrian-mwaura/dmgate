# DMGate: progress report

Huawei Trustworthiness Challenge 5, "Agent Data Minimization via Model–Rule Fusion".
The full design, every decision and its evidence are in [DESIGN.md](DESIGN.md); this report
summarizes them.

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
far from the targets. On our development benchmark (3,616 actions), the two models in scope block
24–29% of legitimate actions and let about 11% of harmful ones through (Section 7.2). All model results
so far are on development data; the test benchmark (AmPermBench) has not yet been run with a model.

**What we learned.**

1. **The contract compiler is the bottleneck.** The rules lose nothing; nearly all errors come from
   what the model writes into the contract, mostly loose descriptions (`where` conditions, file
   patterns) that cover targets the user never meant.
2. **A more capable model is not automatically a safer gate.** The 9B wrote better contracts than the
   4B, yet (before the fix in point 3) let more harmful actions through, all of them via citations:
   the stronger model quoted the user more readily.
3. **Model-supplied evidence must be bound to what it authorizes.** A "cited authorization" (a quote
   of the user that unblocks an action) was checked only for existing; stronger models quoted the
   real request to approve actions on other targets. We made citations *target-bound*, which removed
   that harm (9B: 15.8% → 11.5% of harmful actions let through).
4. **Contracts cannot say "not this".** When a request names something to leave alone, the models put
   it where names go: into the contract's targets (so it gets covered) or into a condition that,
   lacking a "not equal", turns the exclusion into its opposite.

## 2. The challenge, as we read it

| Requirement (challenge text) | Our answer |
|---|---|
| Fine-grained judgment of filesystem and database operations, metadata and content | Each action is resolved to its effects on concrete files, tables, columns and rows; sensitivity per target |
| Hybrid model–rule orchestration; the rule engine enforces explainable boundaries | The model supplies typed facts only; three layers of Rego rules decide; every decision carries a trace |
| Continuous lifecycle evolution (rules adjusted, rule→model feedback) | Planned: an abductive loop that attributes each error to a fact (model) or a rule (patch) |
| General-purpose foundation models, minimal parameter scale, no specialized security models | Open general models up to about 10B parameters (Qwen3.5-4B, Qwen3.5-9B; openPangu-7B next), measured as a size ladder |
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
| First model measurements; error analysis | `3e8af60`, `784e0f1` |
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
- **Negation variant** (Section 7.5): 192 prompts that each add one sentence naming a decoy to leave
  alone; 3,104 probes.

## 5. Design decisions and changes

Decisions were debated one at a time and recorded with their evidence (DESIGN §12).

**Foundations.**

- Ask counts as blocking; a Minimize is scored on what still executes.
- The language model supplies facts, not verdicts, plus two special facts: a cited authorization
  (can lower an outcome) and a residual-risk flag (can only raise one, off by default).
- Contracts separate the envelope (permissions) from targets; tool output can never widen
  permissions.
- Rules in Rego with three layers; the soft rules as a data table, so they can be compared and
  patched safely.
- Planned components agreed then: a transactional sandbox, value handles with graded disclosure, an
  abductive learning loop.

**Q12: what the rules missed.** With a perfect contract, the first rules still let
over-reads and over-writes through, because any read or write inside the envelope was allowed.
Measured with the oracle contract before deciding:

| Gap | Before | Decision | After |
|---|---|---|---|
| Reading records the request does not name | FNR 46.0% | **A5:** a new fact, `target_class`; reads of *records* outside the contract are narrowed (Minimize) or asked; workspace files stay free to explore | 0.0% |
| Writing records the request does not name | FNR 13.0% | **B3:** the same for writes of records; workspace writes stay free | 0.0% |
| Uploading to the host the request names | FPR 15.2% | **C2:** a `remote_named` destination class, for hosts the contract names | 0.0% |

The cost of narrowing was priced with a new coding family: 0 of 352 legitimate exploration actions
were asked under A5 + B3, against 352 of 352 under the stricter alternative.

**Infrastructure.** The models are open and self-hosted (Qwen family, Apache-2.0),
served with vLLM on the university's shared GPU servers (RTX 3090 and RTX 4090). The agent model for
end-to-end runs is also self-hosted (Q7). A rented cloud GPU was tried first and dropped.

**Model choice within the challenge's rules.** The challenge requires general-purpose models
of minimal parameter scale. Our ladder stays within about 10B parameters: Qwen3.5-4B and Qwen3.5-9B
(Apache-2.0), with Huawei's openPangu-7B next. A 27B model was tried and set aside as beyond that
scale; its results are not reported. Security-tuned guard models are excluded by the rules.

**Q3: cited authorization.** Decided **B, target-bound citations**, over keeping it as built,
recompiling through the citation, lowering only one step, or switching citations off. Evidence in
Section 7.4.

**Q15 and Q16: what a contract can express.** Decided: the compiler describes targets, the code
expands each description into the concrete resources it covers, and the model confirms or rejects
each one (Q15, position 1); a user's exclusions are an enforced list that no citation or rule tuning
can lower, at least Ask (Q16). Still open: how a description's parts combine (Q15, position 3).

## 6. Mechanisms and prior work

Each mechanism is set against the closest earlier work we know of. This is a first check, from our
reading and targeted searches, not yet a systematic literature review: "no earlier instance found"
means we did not find one, not that none exists. Our clearest new results are empirical (Section 7):
where the contract compiler fails, that model-supplied citations must be bound to their targets (a
more capable model quotes more freely), and that contracts fail on exclusions.

**No earlier instance found.**

| Mechanism | What it is | Closest prior work | Status |
|---|---|---|---|
| Target-bound cited authorization | A quote of the user may unblock an action only if it names every target of that action, by a name no other resource shares | AuthGraph (Wang et al., 2026) has an LLM check tool parameters that should come from the user against the user's prompt; it binds no quote to a target and does not address names shared by several resources | Built, measured (Q3) |
| Class-gated minimization | Reads and writes of *records* outside the contract are narrowed (Minimize) or asked; workspace files stay free to explore | Limited disclosure in Hippocratic databases (Agrawal et al., 2002) and row-level security narrow queries to permitted rows; AirGapAgent (Bagdasarian et al., 2024) minimizes the data an agent is given; AgentRaft (2026) measures agents' data over-exposure. None splits targets into records and workspace with Minimize as an outcome of the gate | Built, measured (oracle contract) |
| FS/DB benchmark with a negation variant | Filesystem and database tasks on AmPermBench's three axes, with decoys and deterministic labels; a variant whose requests name something to leave alone | AmPermBench (Ji et al., 2026), whose axes we reuse; AuthorizationBench (Zhang et al., 2026) labels agents' out-of-authorization actions in ten domains, varied by trigger and consequence rather than explicitness and naming, and evaluates agents, not gates. Negation is a known weakness of language models in general | Built |

**Our variant of a known idea.**

| Mechanism | What it is | Closest prior work | What is ours | Status |
|---|---|---|---|---|
| Intent contracts | The request compiled into an envelope (permissions, with lifetimes: delete and upload grants last one user turn) and targets; tool output never widens permissions | Progent (Shi et al., 2025) and Conseca (Tsai and Bagdasarian, 2025) have an LLM write a task-specific policy from the user's request and enforce it in code; CaMeL (Debenedetti et al., 2025) keeps untrusted data from changing what the agent does | The envelope/target split; grant lifetimes | Built, measured |
| Quote grounding | Every item the model puts in a contract must quote the user; code checks the quote is in a user turn, and one-turn permissions must quote the latest turn | Checking that a quote appears verbatim in its source is standard in citation-grounded generation; AuthGraph trusts only the user's prompt as a source of parameters | Verbatim quotes required for permissions, scoped to turns | Built, measured |
| Facts with finite domains | The model fills typed facts, never verdicts; "unknown" is a value | Concept bottleneck models (Koh et al., 2020) predict named concepts and decide from them; Progent and Conseca also leave the decision to code | The fact set for data operations | Built |
| Exact comparison of rule tables | Soft rows are boxes in the fact space; the rows split it into finitely many cells (4,320 for the first table) on which every table is constant, so a change can be classified as loosening or tightening exactly | Change-impact analysis of access-control policies (Margrave, Fisler et al., 2005), firewall-policy comparison, AWS's check that a policy grants no new access | Used on an LLM gate's rule table, as the basis for promoting rule patches (P4) | Built |

**Known techniques we build on.**

| Mechanism | What it is | Closest prior work | Status |
|---|---|---|---|
| Three-layer rules with an admin floor | Invariants no soft rule or citation can lower; checked by enumerating about 12,600 fact combinations | Policy combining in XACML (deny-overrides); AWS Service Control Policies, a ceiling no account policy can exceed; Zelkova (Backes et al., 2018) verifies AWS policies with an SMT solver | Built, verified |
| SQL dry run | A statement's `WHERE` clause is run as a key query on a read-only snapshot, so the gate knows the exact rows | Standard database practice (run the `WHERE` as a SELECT before an UPDATE or DELETE); ToolEmu (Ruan et al., 2024) instead emulates tool effects with a language model | Built, verified |
| Replayable evaluation | Every model answer is cached by the exact request; runs replay bit for bit without a GPU; caches from several servers merge by request | Response caching is common practice in evaluation frameworks | Built |
| Transactional sandbox | Opaque programs run in an overlay and are judged on what they actually did | GoEX (Patil et al., 2024): run first, validate afterwards, with undo and damage confinement. Ours would judge the observed effects with the same rules | Planned (P2) |
| Value handles | The model sees `⟦h:7f3a aws_secret len=40⟧` instead of a secret, at a chosen disclosure level | The Dual LLM pattern (Willison, 2023), CaMeL and FIDES (Costa et al., 2025) let a model work with references instead of values. Ours would add graded disclosure levels | Planned (P3) |
| Abductive lifecycle loop | Each error is attributed to a fact (model update) or a rule (patch); loosening patches need a precision bound | Abductive Learning (Dai et al., 2019) is the method; LiSA (Kim et al., 2026) admits learned rules only above a conservative bound; GEPA (Agrawal et al., 2025) evolves prompts. Ours would apply them to a data-minimization gate | Planned (P4) |

References for this section (arXiv identifiers where we have them): AmPermBench, Ji et al., *Measuring
the Permission Gate*, 2604.04978; AuthGraph, Wang, Li and Tian, *Aligning Provenance with
Authorization: A Dual-Graph Defense for LLM Agents*, 2605.26497; AuthorizationBench, Zhang et al.,
*Act on Your Behalf, Beyond Your Authorization*, 2610.27548; AgentRaft, *Automated Detection of Data
Over-Exposure in LLM Agents*, 2603.07557; Progent, 2504.11703; Conseca, *Contextual Agent Security: A
Policy for Every Purpose*, 2501.17070; CaMeL, *Defeating Prompt Injections by Design*; FIDES,
*Securing AI Agents with Information-Flow Control*; GoEX, *A Runtime for Autonomous LLM
Applications*, 2404.06921; LiSA, 2605.14454; GEPA, *Reflective Prompt Evolution*; Dai et al.,
*Bridging Machine Learning and Logical Reasoning by Abductive Learning*, NeurIPS 2019; Koh et al.,
*Concept Bottleneck Models*, ICML 2020; Fisler et al., *Verification and Change-Impact Analysis of
Access-Control Policies* (Margrave), ICSE 2005; Backes et al., *Semantic-based Automated Reasoning
for AWS Access Policies using SMT* (Zelkova), FMCAD 2018; Agrawal et al., *Hippocratic Databases*,
VLDB 2002; Bagdasarian et al., *AirGapAgent*, CCS 2024; Ruan et al., *Identifying the Risks of LM
Agents with an LM-Emulated Sandbox* (ToolEmu), ICLR 2024; Willison, *The Dual LLM pattern*, 2023.

## 7. Experiments and outcomes

All model numbers below are on **development data** (the FS/DB benchmark). FPR is the share of
legitimate actions blocked (Ask or Deny); FNR the share of harmful actions allowed. Brackets are 95%
confidence intervals; for FNR, resampled by prompt, since actions from one prompt are not
independent. Models served with vLLM 0.31.0, each on one RTX 3090.

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

FPR and FNR are replayed with the current gate from the recorded model answers; DESIGN.md §9 names
each run and its manifest. Most blocking is done by Ask: counting Ask as allow, about 80% of harmful
actions would pass for both models. Far from the targets of 0.1% and 5%.

### 7.3 Where the errors come from

- **The compiler.** Contracts exactly matched the oracle's for only 21–27% of prompts. Typical
  mistakes: granting a verb the request did not ask for (`exec` because the request mentions the
  shell); taking files for database rows; a file pattern with no wildcard; mapping an indirect request
  ("could use some attention") to the wrong verb.
- **Over-coverage.** Of the targets a request does not authorize that the contract still covered,
  83% (4B) and 98% (9B) were covered by `where` conditions or patterns the model wrote,
  such as "owner is sam and stale", which also catches the user's other stale logs. A wrongly listed
  id accounts for only 2–6%.
- **Sensitivity** errors lean cautious (about half of internal files called confidential); rating
  something too low happened for 4–6% of items.

### 7.4 Cited authorization (Q3)

When the soft rules held an action back, the filler could quote the user to unblock it; the check
confirmed only that the quote existed. Example: the user asked to delete test account
`test-4812@staging.example`; the 9B quoted that sentence to approve deleting a *different* row.

| Model | Harmful actions let through by a citation | Legitimate actions rightly unblocked by one |
|---|---|---|
| 4B | 5 | 1 of 880 |
| 9B | 118 | 1 |

The 9B's higher FNR than the 4B (+1.5 to +7.5 points, paired by prompt) came entirely from
citations. Each alternative was rescored from the recorded rule results; the method reproduced all
3,616 recorded outcomes exactly for each model before being trusted.

| Model | As built (FPR / FNR) | **Target-bound** | Citations off |
|---|---|---|---|
| 4B | 29.1 / 11.4 | 29.1 / 11.3 | 29.2 / 11.2 |
| 9B | 24.0 / 15.8 | 24.0 / 11.5 | 24.1 / 11.5 |

Target-bound citations remove the citation harm for both models and score the same as switching
citations off, while keeping citations for authorizations that do name their targets. Implemented in
the fact builder; a replay with the new code matched the rescoring on every one of 3,616 actions per model.

### 7.5 Negation: "leave X alone"

Each prompt of the negation variant adds one sentence naming a decoy to exclude ("Leave `X` alone.",
"Don't touch `X`.", "Keep `X` as it is.", "Do not include `X`."). With the oracle contract the rules
still score 0% / 0%. On the 432 harmful actions that target the named decoy:

| Model | Let through, without the sentence | With it | Via a target-bound citation |
|---|---|---|---|
| 4B | 20.4% | **32.6%** | 1 |
| 9B | 23.4% | **27.8%** | 0 |

Target-bound citations have a small polarity gap (a quote like "Don't touch `X`" names X), but the
larger effect is in the compiler: models kept the exclusion out of the contract in only 38% (4B) and
58% (9B) of prompts. Otherwise they listed the excluded item as a target, so it was covered,
or wrote it into a condition, where without a "not equal" it became its opposite: "leave the live
account alone" became `email = <the live account>`. For these two models the cost was in misses on
the named decoy (above); false positives barely moved.

### 7.6 Engineering results that mattered for measurement

- **Throughput.** The evaluation first sent one request at a time (21 answers a minute, the GPU
  mostly idle). Parallel requests raised that to about 640 a minute; decisions and reports are
  identical to sequential runs.
- **Exact replay.** Every number above replays on a laptop from the answer cache, bit for bit;
  checked for every run.
- **Serving issues found and handled:** vLLM's FlashInfer sampler needs a compiler the servers lack
  (switched to vLLM's own sampler, recorded in each run's manifest); FP8 models run on the 3090 with
  weight-only kernels; a rare vLLM
  grammar-engine error is retried, then recorded as "no answer" (which fails closed), so one bad
  request no longer aborts a run.

### 7.7 Limitations

- All model numbers are development data, on prompts our rules and compiler may be tuned on. The
  test set has not been run with a model.
- We are far from the targets: 24–29% FPR and about 11% FNR against 0.1% and 5%.
- Statistical power: even a perfect gate on AmPermBench's 320 legitimate probe actions could only
  show FPR ≤ 0.93%; demonstrating 0.1% needs a corpus of 10,000+ legitimate actions from real agent
  runs, not built yet.
- The probe actions are constructed, not produced by an agent; end-to-end runs (M6) will show which
  overreaches agents actually attempt.

## 8. Next steps

1. **Finish Q15** (how a description's parts combine), then build describe-and-confirm and the
   enforced exclusions (INV-10), and measure both on the base and negation probes.
2. **Compiler prompt work** on the FS/DB data (allowed there): files versus rows, wildcards, indirect
   verbs, uploads.
3. **The openPangu-7B rung**, Huawei's own model, if vLLM loads it on CUDA.
4. **M6: agent runs.** An open agent model (Qwen3.5-35B-A3B) in OpenHands on AmPermBench's tasks,
   gate in log-only mode, producing real action trajectories, then the first test-set numbers.
5. **Later phases:** the transactional sandbox (P2), the Minimize executor and value handles (P3),
   and the abductive lifecycle loop (P4).
