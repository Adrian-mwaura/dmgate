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
`control` covers lifecycle operations on live resources (cancel a job, restart a service). Database
effects (M4) use the kinds `table`, `column` and `row` (ids `customers`, `customers.email`,
`customers/42`); a SELECT or UPDATE is a row effect that only selects records plus a column effect that
carries the data, so rules can narrow rows and columns separately. An effect with any unresolved
selector, or an opaque program, is never exact. Implemented in `dmgate.effects`.

### 5.3 Fact schema

All facts the rules read are declared in a JSON schema with **finite domains**; the fact builder
buckets numbers. There is one fact record per effect, and every fact is always present: where the
builder cannot decide, the value is an explicit `unknown`, never a missing key. Implemented in
`dmgate.facts.schema`; the JSON Schema for OPA is committed as `schema/facts.schema.json`. Set as of
M2 (extended only through review; the M2 extensions are in Q10):

| Fact | Domain | Source |
|---|---|---|
| `op` | effect verbs | effect resolver |
| `opaque` | bool | effect resolver (an unparsed command is opaque) |
| `unresolved` | bool | effect resolver (M2) |
| `max_sensitivity` | S0–S3 / unknown | catalogs + detectors (LLM for uncatalogued targets); for writes and egress, also the data flowing in |
| `targets_in_contract` | all / some / none | set comparison against contract; unresolved selectors never count as covered |
| `has_protected_target` | bool | registry + catalog |
| `envelope_permits` | bool | contract envelope; unknown sensitivity counts as S3 |
| `provenance` | user / trusted_structure / delegated / observed / agent_generated / untrusted_instruction | context matching (+ LLM for "is this observed span an instruction?") |
| `sink_class` | none / context / local / remote_allowlisted / remote_other / recipient_named / recipient_unnamed / unknown | resolver data flows + egress allowlist |
| `volume` | small / large / bulk / unknown | target count vs contract expectation |
| `session_chain` | none / sensitive_read_then_egress | session ledger + earlier effects of the action |
| `security_relevant_change` | none / auth / ci / permissions / network / deps / unknown | path rules; LLM on the diff |
| `cited_authorization` | none / verified | LLM quote + deterministic check |
| `residual_risk_flag` | none / sensitive / destructive / egress / untrusted_provenance | LLM, escalation only |

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
- **Representation (M2, `dmgate.contract`):** a grant is (verb, kinds, sensitivity ceiling, egress
  sink classes, source, turn, lifetime). A target spec names one kind by ids, path patterns, or a
  selector of attribute conditions (`eq` / `in` / `contains`) evaluated against the registry, plus
  the expected count ("my stale branch" is 1). The compiler emits selectors and code evaluates them,
  so the LLM never lists resource ids itself. Write grants last the session because the sandbox
  makes local writes reversible (Q10). `Contract.advance` applies lifetimes between turns.
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
  Implemented in M4 (`dmgate.effects.sql`, sqlglot 30.22): the dry-run turns FROM/JOIN/WHERE into a
  SELECT of primary keys against a read-only in-memory copy of the database snapshot in
  `environment["databases"]` (`dmgate.effects.database`; production would use a read-only replica).
  `*` expands from the schema; aggregates read the rows they summarize; set operations, CTEs and
  unknown tables or columns stay unresolved; PRAGMA, ATTACH and unparsable SQL are opaque. Reached
  from the shell (`sqlite3`, including `.tables` and `.dump`; `psql -c`; `mysql -e`) and from SQL tool
  calls. Snapshots also feed the registry: tables, columns (with schema sensitivity and security
  labels) and rows (with their values, for contract selectors). File globs now expand against the
  registry's files with bash rules.
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

Implemented in M2 (`dmgate.facts`):

- **One record per effect.** The rules decide each effect; the action's outcome is the join (max)
  of its effects' outcomes. A command that does not parse becomes one opaque effect.
- **Unknown vs open.** `unknown` is a value the rules must handle. An *open* fact also lists the
  candidate values the fact filler may pick, with evidence: uncatalogued targets (sensitivity, never
  below a known floor), a target only an observation named (observed vs untrusted_instruction), a
  code change no path rule knows (security relevance). Unknowns that only running the program can
  settle (opaque code, unresolved selectors) stay closed for the sandbox. Cited authorization and
  the residual-risk flag are open on demand, when the rules' result calls for them.
- **Fills.** The filler's answers go back into `build_facts` as fills, which must answer an open
  fact within its candidates. Derived facts are recomputed (the envelope, a later egress's payload),
  and a cited quote is checked verbatim (whitespace-normalized, at least 8 characters) against
  trusted user turns. The model can lower trust (observed → untrusted_instruction), never raise it.
- **Catalogs** (`dmgate.facts.catalog`): sensitivity from the registry attribute, then admin path
  rules, then built-in path rules (first match, exceptions first: `.env.example` before `.env`),
  then a per-kind default. Protected: registry attribute, git internals, system paths, and the root,
  home and working directory themselves. Security relevance by path (CI, deps, auth, network) and
  permission commands. Secret detectors (AWS keys, private keys, GitHub/Slack/API tokens, JWTs,
  bearer headers) mark literal secrets written into a command as S3 payload.
- **Provenance** is matched by name: a target counts as named by a segment when one of its names
  (id, path, basename, registry name/key) stands alone in it. Trusted user turns give `user`, other
  trusted segments `trusted_structure`, a document the user pointed to `delegated`, other untrusted
  segments `observed`, nothing (or only the agent's own words) `agent_generated`. Per effect, the
  least trusted target counts.
- **Data flows** come from the resolver (pipes, redirects, uploads; `cmd < file` included) and are
  closed transitively. A read's sink is where its data ends up; a write or egress carries the
  sensitivity of everything flowing in.
- **Session ledger** (`dmgate.facts.ledger`): reads of S2+ data, recorded only for actions that ran.
  Handles join it in P3.

The M2 choices that go beyond the agreed design are listed for review in Q10.

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

Implemented in M3 (`policy/`, `dmgate.rules`; OPA 1.21.2):

- **Invariants** (`policy/rego/invariants.rego`): INV-1 S2+ egress to an unnamed destination → Deny;
  INV-2 untrusted instruction asking for destructive, egress or sensitive work → Deny; INV-3 delete or
  control of a protected resource → Deny; INV-4 write to a protected resource → Ask; INV-5 destructive
  op with unresolved targets → Ask; INV-6 opaque program while the sandbox is off → Ask; INV-7 egress
  to an unapproved sink after a sensitive read → Ask; INV-8 a secret leaving the platform → Ask;
  INV-9 data of unknown sensitivity toward an unapproved sink → Ask.
- **Soft table** (`policy/soft_rules.json`, pushed into OPA as data): each row is a box (fact → allowed
  values). An effect's soft outcome is the most restrictive matching row, Ask when none matches. So
  adding a row can only loosen where nothing matched before and its outcome is below Ask; everything
  else tightens. `dmgate.rules.soft` validates rows against the schema (a misspelled fact would
  silently never match in Rego) and compares tables exactly: the rows split the fact space into
  finitely many cells (4,320 for the M3 table, out of 38.7M points) on which every table is constant.
- **Composition** (`policy/rego/decision.rego`): per effect, `final = max(floor, soft', risk)`, where
  `soft'` is Allow when the citation is verified and `risk` is Ask when the residual-risk flag is
  live and corroborated by a risk fact (shadow mode by default). Action outcome = max over effects.
- **Checks:** `opa check --strict --schema schema/facts.schema.json` (a misspelled fact is a type
  error), `opa fmt`, Rego unit tests (`opa test`), the floor enumeration (≈12.6k points: even an
  allow-everything soft table with verified citations ends exactly at `max(floor, risk)`), and a
  differential test that Rego and Python agree on every cell of the shipped table.
- **Gate** (`dmgate.rules.gate.RuleGate`): effects → facts → OPA → Decision with reasons and a trace
  (policy version, contract, facts, per-effect rule results). A Minimize's residual is the covered
  targets of minimized effects plus the targets of allowed ones; None (a scoring failure) if an
  allowed state-changing effect has unresolved or opaque parts. About 1 ms per decision.

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
  - **v0 (M4, `dmgate.datasets.fsdb`, dataset `fsdb-dev-probes`):** six families: fs-cleanup (rm),
    fs-read (cat), fs-share (curl/scp upload), db-delete, db-update, db-query (SQL via `sqlite3` or an
    SQL tool). Each is AmPermBench's 4 × 4 × 2 matrix, `Prompt(S,B,R) = S_template[S](B_slot[B],
    R_slot[R])`, with R0 a staging lane (internal data) and R1 a production lane (customer data): 192
    prompts. Same candidate structure in every family and lane: T1-T3 the user may mean; decoys P1
    (own, still active), P2 (other category, same ticket), P3 (colleague's), P4 (shared, protected),
    P5 (release, protected, R1 only); fillers F1-F2. B0/B1 authorize T1, B2/B3 T1-T3; read and update
    families also authorize exactly the needed column. 3,104 probes (528 benign): each candidate,
    the authorized batch, the batch plus each decoy, plus globs, SQL predicates, extra columns, `*`,
    all rows and another host. Labels are judged within each prompt's universe of targets.
  - Validation: every S×B×R cell once; neighbours differ in one slot only; the authorized set equals
    the request's meaning re-derived from the environment (registry and database); oracle sets are
    disjoint and protected decoys are protected; the resolvers recover all 3,104 probe targets
    exactly; with the oracle contract, coverage matches every label.
  - It is development data: rules, prompts and exemplars may be tuned on it.
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

Status: **P0 and M1 done (2026-10-26), M2, M3 and M4 done (2026-10-27).** P0 and M1 were rebuilt in
this repository from the first prototype (`C:\Users\adria\OneDrive\Desktop\Huawei Challenge`, kept as
reference only). P0: record schema, gate interface, scoring, exact and clustered CIs, reports,
harness CLI, AmPermBench probe set. M1: effect resolver. M2: contract model, fact schema, catalogs,
session ledger, fact builder; `explain` prints the facts. M3: invariants, soft table, composition in
Rego on an OPA sidecar, box geometry, rule gate. M4: SQL resolver with dry-run, database snapshots,
file globs, the FS/DB dev benchmark v0. 247 tests.

M3 bounds on the AmPermBench probe set (stand-in contracts until M5; neither is a result):

| Gate | Contract | FPR | FNR |
|---|---|---|---|
| `rules-profile-only` | admin profile, no request grants | 100% (320/320 benign asked) | 0% |
| `rules-oracle-contract` | profile + oracle's authorized set | 0% | 0% |

The gap between the two is the contract compiler's job (M5).

M4 on the FS/DB dev probes, rules as of M3 (dev data, so these numbers drive rule work):

| Gate | FPR | FNR | Where the errors are |
|---|---|---|---|
| `rules-profile-only` | 74.2% | 19.3% | every destructive or out-of-ceiling action asked; db-update writes allowed |
| `rules-oracle-contract` | 15.2% | 46.0% | fs-read 92% FN, db-query 94% FN, db-update 78% FN, fs-share 100% FP |

With a perfect contract, the M3 rules lose nothing on deletes (fs-cleanup, db-delete: 0% / 0%) but
miss over-reads and over-writes: reads and writes inside the envelope are allowed whatever their
targets, and a read feeding a requested upload counts as exposed. These are Q12.

### P1 milestones

| # | Milestone | Validation | Needs from user |
|---|---|---|---|
| M1 ✅ | Effect model, resource registry, shell resolver (tree-sitter-bash) | Resolved targets equal ground truth on all 2,560 probe commands (parser correctness, not tuning) | — |
| M2 ✅ | Fact schema (JSON schema, finite domains) and deterministic fact builder; contract is an input | Unit tests per fact; with the oracle's contract, `targets_in_contract` reproduces all 2,560 probe labels (plumbing, not tuning) | — |
| M3 ✅ | Rule engine: invariants, soft-rule table, composition in Rego; OPA sidecar; schema check | Enumeration test that no soft row can go below an invariant floor; Rego/Python agreement on every cell | OK to download the OPA binary (given) |
| M4 ✅ | FS/DB dev benchmark v0 on the S×B×R axes, deterministic oracles; SQL resolver (sqlglot) with dry-run | Oracle self-consistency tests; resolvers recover all 3,104 probe targets | — |
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
| Q12 | Rule gaps the FS/DB dev benchmark found (needs debate before rule changes): (a) **read minimization**: SR-READ allows any read inside the envelope, so reading a colleague's tickets, other customers' rows or unneeded columns passes. Should reads of targets outside the contract be narrowed (Minimize to covered rows/columns/files) or asked, and for all data or only S2+ (leaving coding agents free to explore S0/S1 workspace files)? (b) **write targets**: SW-WRITE allows any write inside the envelope (chosen for reversible workspace files), so updating other people's orders or extra columns passes. Needs a way to tell workspace files from records, e.g. a `target_class` fact or contract targets that cover the workspace for coding tasks. (c) **named destinations**: a read feeding an upload to a host the request names has sink `remote_other`, so SR-EXPOSED asks (all fs-share benign FP). Proposal: a `remote_named` sink class, mirroring `recipient_named` | M4 |
| Q11 | Accepted for now 2026-10-27 (revisit during testing). M3 choices made while building: (a) soft outcome = most restrictive matching row, Ask when none matches; (b) a verified citation lowers the soft layer to Allow (not one step); (c) the residual-risk escalation sits beside the floor, so a citation cannot lower it; (d) the nine invariants and twenty soft rows as written; (e) opaque programs are Ask until the sandbox exists (INV-6), then the soft layer allows them for judgment in the sandbox; (f) Minimize narrows within one effect only: an action of separate commands where one is unrequested (`rm a && rm x`) is asked about whole. Dropping such sub-commands belongs with the Minimize executor (P3) | M3 |
| Q10 | Accepted for now 2026-10-27 (revisit during testing). M2 choices made while building: (a) one fact record per effect, action outcome = join; (b) schema extensions: `unresolved` fact, `unknown` values for sensitivity / sink / volume / security change, `none` sink, residual-risk categories; (c) write grants last the session; (d) unknown sensitivity counts as S3 in the envelope check; (e) `session_chain` counts only reads known to be S2+; (f) `volume` bucket edges (3 / 20, ×4 of the expectation) are placeholders until M4; (g) the built-in path catalog; (h) AmPermBench catalog says its resources are S1, and its admin profile allows the git remote `origin` | M2 |

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
| 2026-10-27 | M2 built (user asked to skip ahead from the lessons). Its own choices are open for review as Q10 |
| 2026-10-27 | Q10 accepted for now (revisit during testing). M3 started; OPA binary download approved |
| 2026-10-27 | M3 built on OPA 1.21.2 (checksum-verified, in `tools/`, not committed). Its own choices are open for review as Q11 |
| 2026-10-27 | Q11 accepted for now. M4 built (sqlglot 30.22 added). Bug fixes found by the dev benchmark: INV-7 now spares destinations the request names (its stated intent); the FS/DB oracle contract takes its ceiling from files and columns, not row selections. Rule gaps open as Q12 |

## 13. References

- [1] Anthropic, Claude Code auto mode (engineering post).
- [2] Ji et al., *Measuring the Permission Gate: A Stress-Test Evaluation of Claude Code's Auto Mode*, arXiv 2604.04978. Code: github.com/yan5ui/cc-auto-mode-measurement.
- [4] GEPA: reflective prompt evolution.
- [6] Kim et al., *LiSA: Lifelong Safety Adaptation via Conservative Policy Induction*, arXiv 2605.14454.
- Dai, Xu, Yu, Zhou, *Bridging Machine Learning and Logical Reasoning by Abductive Learning*, NeurIPS 2019.
- CaMeL (capabilities for agent data flow); FIDES (information-flow control for agents); AgentDojo.
- OpenHands SDK security docs: docs.openhands.dev/sdk/arch/security.
