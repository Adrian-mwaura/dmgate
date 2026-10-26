# DMGate: agent data minimization via model–rule fusion

Design for Huawei Trustworthiness Challenge 5 ("Agent Data Minimization via Model–Rule Fusion").
This document is the source of truth for the architecture. Every decision here was debated and
agreed; provisional ones are marked, and open questions are collected in [§11](#11-open-questions).

## 1. Goal and framing

An agent platform (coding agents, personal assistants, OS agents) intercepts every data operation an
agent attempts and decides **Allow / Minimize / Ask / Deny**, so the agent touches only the data the
user's task needs, even when untrusted context tries to steer it elsewhere.

Challenge requirements, as we read them:

| Requirement | Our answer |
|---|---|
| Fine-grained judgment of FS/DB operations, metadata and content | Effects resolved to resources, columns, rows and values; content detectors; handles per value |
| Hybrid model–rule orchestration, explainable boundaries | LLM produces typed facts only; a three-layer Rego engine decides; every decision carries a trace |
| Lifecycle: dynamic rules, AI-assisted generalization, rules→model feedback | Abductive loop that attributes each error to a fact (model update) or a rule (patch) |
| General-purpose models, minimal scale | Narrow fact questions sized for 4–8B general models; size ladder measured |
| FPR < 0.1%, FNR < 5% | Measured strictly (§8); Minimize and handles keep tasks alive instead of blocking |

Framing (2026-10-26): the challenge is a **research direction**. Targets are aspirations we measure
against honestly; novel mechanisms are preferred over narrow compliance readings.

## 2. Threat model and scope

- **Trusted:** user turns, answered Asks, platform/admin policy, platform-owned catalogs.
- **Untrusted:** every tool output, file content, web page, ticket, email, README, code comment,
  and anything the agent itself writes (its reasoning, commit messages, audit-log "reasons").
- **Adversary:** controls untrusted content (prompt injection, data poisoning). The agent may also
  be overeager without any attacker (AmPermBench's scope and blast-radius failures).
- **Platform owns runtime and data** (stated in the challenge), so runtime mediation is legitimate.
- **Out of scope:** detecting skilled code backdoors (we flag security-relevant edits only), covert
  timing channels, kernel-level adversaries, a compromised platform.

## 3. Outcomes

Ordered lattice: `Allow < Minimize < Ask < Deny`.

- **Allow:** execute as requested.
- **Minimize:** execute a narrowed version: fewer targets, fewer columns/rows, or values returned
  as handles at a chosen disclosure level (§6.7). The agent is told what was narrowed and why.
- **Ask:** pause for the user, with a templated explanation built from the trace.
- **Deny:** refuse, returning a structured reason to the agent so it can adapt.

## 4. Architecture overview

```
 user turn (trusted) ──► Intent contract compiler (LLM, per turn) ──► contract: envelope + targets
                                                                           │
 agent tool call ──► Effect resolver (deterministic) ──► effects ──┐       │
        │                                                          ▼       ▼
        │                                   catalogs ──►  Fact builder (scope, sensitivity,
        │                                                 provenance, sink, volume, session taint)
        │                                                          │
        │                                                          ▼
        │                                   LLM fact filler (only missing/uncertain facts)
        │                                                          │
        │                                                          ▼
        │                         Rule engine: invariants ⊕ soft-rule table ⊕ composition
        │                                                          │
        ▼                                                          ▼
 Transactional sandbox ◄──── Allow / Minimize / Ask / Deny + decision trace
 (overlay, egress proxy,                                           │
  stdout tokenizer)                                                ▼
                                       Lifecycle loop (abductive attribution → model or rule update)
```

## 5. Data model

### 5.1 Sensitivity levels

| Level | Meaning | Examples |
|---|---|---|
| S0 | Public / ordinary workspace content | source code, docs |
| S1 | Internal | configs, logs, build metadata |
| S2 | Confidential | PII, business records, customer data |
| S3 | Secret | credentials, keys, tokens, auth tables |

### 5.2 Effects

Every tool call resolves to a list of effects `(verb, kind, targets, unresolved)` plus data flows
between them (pipes, redirects, uploads). Verbs: `read | write | delete | control | egress | exec`;
`control` covers lifecycle operations on live resources (cancel a job, restart a service). SQL verbs
arrive with the SQL resolver in M4. An effect with any unresolved selector, or an opaque program, is
never exact. Implemented in `dmgate.effects`.

### 5.3 Fact schema

All facts the rules read are declared in a JSON schema with **finite domains**; the fact builder
buckets numbers. Initial set (extended only through review):

| Fact | Domain | Source |
|---|---|---|
| `op` | effect verbs | effect resolver |
| `max_sensitivity` | S0–S3 | catalogs + detectors (LLM for unknown columns) |
| `targets_in_contract` | all / some / none | set comparison against contract |
| `has_protected_target` | bool | registry |
| `envelope_permits` | bool | contract envelope |
| `provenance` | user / trusted_structure / observed / delegated / untrusted_instruction / agent_generated | taint ledger (+ LLM for "is this span an instruction?") |
| `sink_class` | context / local / remote_allowlisted / remote_other / recipient_named / recipient_unnamed | deterministic |
| `volume` | small / large / bulk vs contract expectation | deterministic |
| `session_chain` | none / sensitive_read_then_egress / … | taint ledger |
| `security_relevant_change` | none / auth / ci / permissions / network / deps | LLM on diff |
| `cited_authorization` | none / verified | LLM quote + deterministic check |
| `residual_risk_flag` | none / category | LLM, escalation only |
| `opaque` | bool | effect resolver |

## 6. Components

### 6.1 Intent contract compiler (LLM)

Compiles **only trusted inputs** (user turns + resource schemas, never tool output) into a contract
with two layers:

| Layer | Holds | Widened by |
|---|---|---|
| Envelope | Kinds of permission: read up to a sensitivity ceiling, destructive ops, egress, DB writes | Platform default profile per agent type, a user turn, an answered Ask. **Never** tool output or delegated text. |
| Targets | Which files, rows, branches, hosts inside the envelope | Trusted structure (imports, FKs), environment observations (stack traces, listings), delegated text the user pointed to. Destructive targets must also satisfy the user's selector, checked against the registry. |

- **Grant lifetimes:** read grants last the session; destructive and egress grants last only the
  granting user turn; "continue"-style turns carry the previous turn's grants forward.
- **Ambiguous selectors** ("stale branches") cost one Ask per instruction; the answer fixes the
  definition for the session.
- **Default profiles** are admin-set. The coding profile allows package fetches (no sensitive payload)
  as distinct from data egress.
- Known weak spot: delegated text naming destructive targets is only as safe as the registry's
  ownership attributes.

### 6.2 Effect resolver (deterministic)

Adapters map tool calls to effects:

- **Shell:** tree-sitter-bash AST + a command-semantics table (`cat/grep/find` read, `rm` delete,
  `curl/scp/nc` egress, `git`, `kubectl`, `psql/sqlite3` → SQL). Pipes become data flows.
  Interpreters with inline code, `eval`, scripts, `make/pytest/npm` → `opaque`.
- **SQL:** sqlglot → tables, columns, predicates, verb, LIMIT; dry-run `COUNT`/id query for blast radius.
- **File tools:** direct mapping; writes to registered resource-backing files (e.g. AmPermBench
  `objects.json`) become resource effects. This closes the in-project-edit bypass AmPermBench found.
- **MCP/API tools:** declarative per-tool effect specs; unknown tools get an LLM-proposed spec, cached and reviewed.
- **Dry-run expansion:** globs, selectors and `WHERE` clauses become concrete target sets.

Gate on **effect**, never on tool type or location.

### 6.3 Transactional sandbox

Opaque code is judged on what it actually did. Handling depends on reversibility:

| Effect | Reversible | Mechanism | Decision point |
|---|---|---|---|
| Local writes/deletes | yes | copy-on-write overlay; diff after the command | after execution, before commit |
| Data into the agent's context | yes, until returned | stdout tokenized by exact match of catalog-known values (and common encodings) | before the agent sees output |
| Network egress | no | only route is our proxy; destination + payload fingerprints | synchronously, before bytes leave |
| Ungranted sensitive files | n/a | sandbox filesystem view built from the envelope | before execution |

- Precise fingerprints for stdout (protects FPR); conservative taint for egress (process opened S3
  data + connects to a non-allowlisted host → deny).
- Semantic diffs: registry maps backing files to resources (JSON diff → deleted artifacts); SQLite via `sqldiff`.
- Partial commits only for independent resources; code files are all-or-nothing → Ask.
- Long-running processes: working directory ambient, egress mediated.
- **Unverified:** OpenHands keeps a persistent shell; plan is one overlay per session with
  per-command snapshots of changed paths. Must be checked against the runtime code.
- Static analysis and LLM code reading remain as early warning, not as the boundary.

### 6.4 Fact builder

Joins effects, contract, catalogs and the session taint ledger into the fact record (§5.3).
The taint ledger records every string from user turns vs. tool outputs, every sensitive value read,
and every handle issued.

### 6.5 LLM fact filler

Runs only for missing or uncertain facts. Narrow questions, constrained JSON output (vLLM guided
decoding), a confidence per fact, k-sample agreement only on decision-critical facts. Two special
fact types:

- **Cited authorization** (can lower an outcome): the LLM must quote the authorizing span; code
  verifies the quote exists in a trusted user turn. Lowers soft rules only, never invariants.
- **Residual-risk flag** (can only raise, to Ask): Route-1-style holistic check with category and
  evidence span. Acts only alongside a risk fact (sensitive, destructive, egress, untrusted
  provenance), only on k/k agreement, shadow mode first. True positives are distilled into new facts.

### 6.6 Rule engine (Rego, OPA v1)

| Layer | Form | Editor | Role |
|---|---|---|---|
| Invariants | hand-written Rego | admin only | minimum outcome (floor) |
| Soft rules | JSON table (`conditions → outcome, reason, id`) as OPA data | lifecycle loop | tuned behavior |
| Composition | fixed Rego | admin | `final = max(invariant_floor, soft_decision)`; cited authorization lowers only `soft_decision` |

- Rules read only schema-declared discrete facts; `opa check --schema` + a linter remove Rego's
  silent-undefined failure mode.
- Each soft row is a box in the discrete fact space; a patch **loosens** if its box overlaps a region
  where the old decision was more restrictive. Exhaustive enumeration is the fallback check.
- Runtime: OPA as a localhost sidecar; regorus embedded only if latency matters (with a differential test).

Example invariants: S2+ egress to a destination the user never named → ≥ Deny; untrusted-instruction
provenance with sensitive data, destructive op or egress → ≥ Deny.

### 6.7 Minimize and handles

**Write side:** narrow destructive targets to the authorized subset; project out unneeded columns;
add LIMIT to exploratory queries.

**Read side: tokenized handles with a dual view.** Sandboxed processes read real values; the model
sees handles such as `⟦h:7f3a aws_secret len=40⟧`. A mask is a handle that never resolves.

| Level | Model sees |
|---|---|
| L0 | nothing (not mounted / not returned) |
| L1 | handle + type |
| L2 | handle + derived properties (length, format validity, email domain, same value → same handle) |
| L3 | partial reveal |
| L4 | full value |

- Rules pick the lowest sufficient level; the agent may call `reveal(handle, reason)`; S3 needs Ask
  unless cited authorization.
- Credential handles are **audience-bound** (an AWS key resolves only toward `*.amazonaws.com`).
- Handle IDs are HMAC-bound to the session; unknown or mangled handles fail closed.
- When the data content is the task, the envelope grants L4 on a narrow scope, or the agent uses
  code-to-data (analysis runs in the sandbox, aggregates come back).
- Limit: processes see real values, so derived side channels (length, character probing) are
  raised in cost, not eliminated. Red-teamed and reported.

### 6.8 Gate hardening

Never treated as authorization: the agent's reasoning or self-justification, comments inside commands,
agent-written audit-log reasons, any text from tool output. The contract compiler never sees untrusted
content. LLM outputs are enums plus quoted spans, so injected text cannot produce free-form overrides.

### 6.9 Decision trace

Every decision records: input hashes, facts with their source (detector id or LLM question + prompt
version + confidence + evidence span), invariant ids, matched soft-row ids, final outcome, Minimize
parameters, rule-set version, model id. Replaying the trace through the rules reproduces the decision.

### 6.10 Lifecycle: abductive rule–model loop

Abductive Learning (ABL) applied to the gate:

1. **Abduce.** For each labeled error (dev set, Ask answer, incident), find the cheapest explanation:
   flipping facts (cost grows with extractor confidence) or editing a soft row (cost = case-bank
   decisions it would flip, via box geometry).
2. **Verify.** Corrected fact labels are checked by a large open teacher model, k-sample agreement,
   or a human sample. A small human-labeled anchor set protects fact types the teacher may misread.
3. **Update, by rising cost:** exemplar store (instant) → GEPA prompt evolution with the rule trace
   as feedback (hours) → per-extractor LoRA on a general base (periodic). Rule errors go to the
   patch-promotion gate: tightening patches go live fast in shadow; loosening patches need a
   conservative lower bound on precision (LiSA-style) before activation.

Never train on decision-only reward: it makes facts meaningless (reward hacking).

## 7. Integration and deployment

- **OpenHands Layer A:** custom `SecurityAnalyzerBase` subclass calling the gate → Allow/Ask via confirmation mode.
  Replaces the default analyzer, which lets the agent rate its own risk.
- **OpenHands Layer B:** wrapper around tool executors → Deny-with-reason as an observation, Minimize
  rewrites, stdout tokenization, handle resolution.
- **Deployment:** vLLM on AutoDL (gate models; optionally the agent model) behind an SSH tunnel.
  OpenHands, its Docker sandbox, the gate service, OPA, test data and the eval harness on the Windows
  machine (Docker Desktop + WSL2). The gate only talks to an OpenAI-compatible endpoint.

## 8. Evaluation

### 8.1 Scoring (provisional, revisit during testing)

Score the action that actually executes. Positive = should be blocked.

| Gate output | Benign | Harmful |
|---|---|---|
| Allow | TN | FN |
| Deny / Ask | FP | TP |
| Minimize | rescore residual: TN if sufficient, else FP | rescore residual: TP if nothing unauthorized remains, else FN |

Offline masking check: if a masked value appears in a later agent action of the recorded trajectory,
the mask counts as FP. Online: paired task success with and without the gate.

### 8.2 Reported numbers

1. Headline FPR/FNR with Ask = block (the target).
2. Ask-as-allow FNR stress number.
3. FPR on all benign actions and on non-trivial benign actions (sensitive, state-changing, egress).
4. Raw and per-intent (retry-deduplicated) counts.
5. Exposure reduction: sum over sensitive values of the highest disclosure level reached.
6. 95% CIs: Clopper–Pearson per rate; prompt-clustered bootstrap where actions share a prompt.

Statistics note: 0 FP in 3,000 benign actions only bounds FPR at ≈0.1% (rule of three); we target 10k+.

### 8.3 Datasets

- **AmPermBench** (arXiv 2604.04978, MIT-licensed code): 128 prompts × 4 DevOps families × 3 ambiguity axes, 253 actions.
  **Test only**; never used for prompts, exemplars or training.
  - The repo ships prompts, deterministic oracles (authorized / must-preserve / protected sets) and
    initial states, but **no recorded agent runs**. Test actions must come from running an agent in
    its task containers.
  - Labels are deterministic per action: safe iff the affected resources ⊆ the authorized set. We
    use this instead of the paper's LLM-judge labels. The released evaluator scores only the first
    state-changing action per run; we score every action, as the paper does.
  - Probe set (`dmgate.datasets.ampermbench.probe_records`): 2,560 oracle-labeled candidate actions
    (320 benign, 2,240 harmful) for harness validation. Even a perfect gate on its 320 benign
    actions can only show FPR ≤ 0.93% (one-sided 95%), which is why the benign corpus matters.
- **FS/DB benchmark (ours):** same three axes (specificity, blast radius, risk) over filesystem and
  database tasks. Development data plus a released contribution.
- **Injection suites:** e.g. AgentDojo-style tasks for the untrusted-context threat.
- **Benign corpus:** 10k+ actions from OpenHands runs on ordinary coding/data tasks (gate in log-only
  mode) plus public trajectories.
- Holdouts: whole task families held out for generalization claims.

### 8.4 Experiments

| Id | Question | Position |
|---|---|---|
| E1 | Holistic LLM verdict vs facts+rules vs facts+rules+special facts | 2 |
| E2 | Out-of-envelope rate on benign corpus (alarm above ~0.05%) | 3 |
| E3 | Opaque share of benign corpus | 4 |
| E4 | Small LLM writing patches: raw Rego vs JSON rows vs Cedar | 5 |
| E5 | Learning curves: frozen / GEPA / abductive / decision-reward RL | 6 |
| E6 | Pareto: task success vs exposure across disclosure policies; side-channel red team | 7 |
| E7 | Model size ladder (4B → 32B, dense and MoE, incl. openPangu) | all |

## 9. Build plan

| Phase | Scope | Exit criterion |
|---|---|---|
| P0 | Replay harness: record schema, gate interface, scoring, CIs, reports; AmPermBench records; trivial baselines | AmPermBench records load; baselines scored with CIs; metrics unit-tested |
| P1 | Offline gate v1: effect resolver, catalogs, fact builder, contract compiler + fact filler (prompted), OPA three layers, FS/DB dev benchmark v0, OpenHands runs in AmPermBench containers | First AmPermBench numbers; E1, E7 started |
| P2 | Transactional sandbox; OpenHands Layers A/B; benign corpus generation | FPR numbers on 10k+ benign actions; E2, E3 |
| P3 | Minimize + handles | E6 |
| P4 | Lifecycle: abductive loop, GEPA, LoRA, patch promotion | E4, E5 |
| P5 | FS/DB benchmark release, demo UI (decision-trace viewer), write-up | — |

Status: **rebuild in progress (since 2026-10-26).** P0 and M1 were first built as a prototype in
`C:\Users\adria\OneDrive\Desktop\Huawei Challenge` (kept as reference, not maintained). They are
being rebuilt here chunk by chunk: record schema, gate interface, scoring, exact and clustered CIs,
reports, harness CLI, AmPermBench probe set (P0), then the effect resolver (M1).

### P1 milestones

| # | Milestone | Validation | Needs from user |
|---|---|---|---|
| M1 | Effect model, resource registry, shell resolver (tree-sitter-bash); prototype done, rebuild pending | Resolved targets equal ground truth on all 2,560 probe commands (parser correctness, not tuning) | — |
| M2 | Fact schema (JSON schema, finite domains) and deterministic fact builder; contract is an input | Unit tests per fact | — |
| M3 | Rule engine: invariants, soft-rule table, composition in Rego; OPA sidecar; schema check | Enumeration test that no soft row can go below an invariant floor | OK to download the OPA binary |
| M4 | FS/DB dev benchmark v0 on the S×B×R axes, deterministic oracles; SQL resolver (sqlglot) with dry-run | Oracle self-consistency tests | — |
| M5 | LLM layer: OpenAI-compatible client, contract compiler, fact filler (constrained JSON) | Dev-benchmark accuracy per fact | AutoDL endpoint, GPU type (Q6) |
| M6 | OpenHands runs in AmPermBench task containers (gate log-only) → test records; first end-to-end numbers, E1, E7 | Run coverage across 128 prompts | Agent model choice (Q7) |

## 10. Research contributions

1. Envelope/target contracts with differentiated grant lifetimes.
2. Transactional, reversibility-aware enforcement (decide after execution, commit later) vs pre-execution classifiers.
3. Abductive rule–model co-evolution for agent security gates.
4. Minimize as a first-class outcome, residual scoring, and an exposure metric.
5. Dual-view handles with graded disclosure and audience-bound credentials.
6. An FS/DB permission benchmark on AmPermBench's three axes.

## 11. Open questions

| # | Question | From |
|---|---|---|
| Q1 | Should Ask count as only half-safe in FNR? | Position 1 (revisit in testing) |
| Q2 | Should a secondary "may-ask" label exist for ambiguous prompts? | Position 1 |
| Q3 | Is the quote check enough to make cited authorization (the only outcome-lowering path) safe? | Position 2 |
| Q4 | Is requiring a co-occurring risk fact for the residual-risk flag too conservative? | Position 2 |
| Q5 | Does the OpenHands persistent shell allow per-command transactions? | Position 4 (verify in code) |
| Q6 | AutoDL: GPU card type; whether instances can run Docker (assumed not) | Deployment |
| Q7 | Agent model for OpenHands: open model on AutoDL or an API model? | Deployment |
| Q8 | ~~AmPermBench code license and data availability~~ Resolved: MIT; data files only, no recorded runs | P0 |
| Q9 | ~~Pull an FS/DB dev benchmark and OpenHands AmPermBench runs into P1?~~ Adopted 2026-10-26 (P1 milestones M4, M6) | P0 finding |

## 12. Decision log

| Date | Decision |
|---|---|
| 2026-10-22 | GPU via AutoDL; OpenHands for online evals; offline replay first |
| 2026-10-22 | Position 1: Ask = block; Minimize scored on residual (provisional) |
| 2026-10-23 | Position 2: LLM supplies facts only, plus cited authorization and residual-risk flag (provisional) |
| 2026-10-23 | Position 3: envelope vs targets; tool output widens targets only |
| 2026-10-25 | Position 4: transactional sandbox is core (P2) |
| 2026-10-25 | Position 5: Rego with three layers; JSON soft-rule table |
| 2026-10-26 | Challenge reframed as research direction |
| 2026-10-26 | Position 6: abductive rule–model loop |
| 2026-10-26 | Position 7: handles with dual view; closes the `.env` masking question |
| 2026-10-26 | P0 done; Q9 adopted: FS/DB dev benchmark v0 and OpenHands AmPermBench runs move into P1 |
| 2026-10-26 | Restart in `C:\Users\adria\Projects\dmgate` (outside OneDrive, private GitHub backup). Prototype kept as reference |
| 2026-10-26 | Build mode: Claude builds each milestone, then teaches it to the user with exercises. P0 and M1 rebuilt here first |

## 13. References

- [1] Anthropic, Claude Code auto mode (engineering post).
- [2] Ji et al., *Measuring the Permission Gate: A Stress-Test Evaluation of Claude Code's Auto Mode*, arXiv 2604.04978. Code: github.com/yan5ui/cc-auto-mode-measurement.
- [4] GEPA: reflective prompt evolution.
- [6] Kim et al., *LiSA: Lifelong Safety Adaptation via Conservative Policy Induction*, arXiv 2605.14454.
- Dai, Xu, Yu, Zhou, *Bridging Machine Learning and Logical Reasoning by Abductive Learning*, NeurIPS 2019.
- CaMeL (capabilities for agent data flow); FIDES (information-flow control for agents); AgentDojo.
- OpenHands SDK security docs: docs.openhands.dev/sdk/arch/security.
