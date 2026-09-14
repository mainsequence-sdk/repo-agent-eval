# ADR-007: Mutually Exclusive DSPy Optimization Strategies

- Status: Accepted and implemented
- Date: 2026-09-14
- Decision owners: MS Agent Eval maintainers
- Scope: optimization experiment configuration, DSPy optimizer selection,
  prompt and skill search, few-shot fitting, leakage control, budgets, stage
  identity, compiled artifacts, and reporting
- Extends:
  [ADR-003: Content-Addressed Experiment Graph and Stage Reuse](003-content-addressed-experiment-graph.md)
- Preserves:
  [ADR-001: Portable Staged Execution](001-portable-staged-execution.md)

## Context

The current optimization implementation supports only DSPy's
`LabeledFewShot`. The schema rejects every other optimizer and interprets:

```yaml
optimizer:
  name: LabeledFewShot
  search:
    k: [1, 2, 4]
```

as a search over the number of demonstrations attached to a solver prompt.
For each fold, DSPy selects `k` complete training examples and stores them in
the compiled `Predict` state. The framework compares the unfitted baseline and
the configured `k` values by grouped cross-validation.

That is a legitimate optimization method, but it is not instruction search.
It does not rewrite the solver instruction, its output prefix, or the skill
content. It must therefore not be presented as the only or canonical meaning
of prompt optimization.

The framework also must not automatically run both few-shot fitting and prompt
search. They have different semantics, cost profiles, model inputs, artifacts,
and invalidation rules. The workspace author must choose the strategy for an
optimization experiment. Running that experiment executes only the chosen
strategy.

The required second strategy is COPRO-based search. In pinned DSPy 3.2.1,
`COPRO` proposes alternative predictor instructions and output-field prefixes,
evaluates the candidates with a metric, retains the strongest candidate, and
uses the scored attempts to propose another breadth/depth round. Stock COPRO
optimizes predictor signatures; it does not rewrite a dynamic
`skill_context` input. Optimizing skills therefore requires an explicit,
auditable extension around the COPRO instruction-proposal mechanism.

## Decision

Every `mode: optimize` experiment declares exactly one optimization strategy:

```text
optimization experiment
          │
          └── optimizer.strategy
                    │
          ┌─────────┴─────────┐
          │                   │
          ▼                   ▼
 labeled_few_shot           copro
 demonstrations only       prompt and skill search
          │                   │
          └─────────┬─────────┘
                    ▼
       one compiled solver artifact
                    ▼
        untouched test evaluation
```

For COPRO, the final evaluation freezes both the original and optimized
programs and scores them on identical untouched test cases. The paired-report
contract is defined by [ADR-008](008-paired-copro-evaluation-and-formal-reporting.md).

The strategy is a discriminated configuration union. There is no implicit
default and no composite strategy in this ADR.

An experiment configured with `labeled_few_shot`:

- searches only demonstration configuration;
- does not rewrite instructions or skills; and
- never executes COPRO proposal calls.

An experiment configured with `copro`:

- searches solver instructions and configured skill content;
- keeps the compiled solver zero-shot unless a future ADR explicitly composes
  strategies; and
- never attaches `LabeledFewShot` demonstrations.

Defining multiple optimization experiments in one workspace is allowed for an
explicit comparison, but running one experiment never causes another
experiment or strategy to run.

## The strategies optimize different things

The word *optimization* does not identify one interchangeable operation here.
Each strategy changes a different part of the model-visible request.

| Strategy | Variables searched | Kept fixed | Question answered |
| --- | --- | --- | --- |
| `labeled_few_shot` | Which reviewed training demonstrations are attached and how many are attached | Solver instruction, output prefix, source skill text, model weights | Does showing the solver solved examples improve its answers? |
| `copro` | Solver instruction, response-field prefix, and configured skill-text overlays | Zero demonstrations, signature field names/types, source evidence, model weights | Which wording and organization of the prompt and skills produces better answers? |

Neither strategy changes the LLM's weights. They compile different
model-visible programs around the same fixed model deployment.

### What `labeled_few_shot` actually changes

The baseline model-visible request is conceptually:

```text
[fixed solver instruction]
[fixed global context]
[fixed source skill]
[current task]
```

A labeled-few-shot candidate changes it to:

```text
[the same fixed solver instruction]

[reviewed training task 1]
[expected response 1]

[reviewed training task 2]
[expected response 2]

[the same fixed global context]
[the same fixed source skill]
[current task]
```

For `k=2`, the candidate contains two solved training cases. For `k=4`, it
contains four. The current instruction and skill bytes are not candidates and
cannot win or lose independently. The resulting compiled state is a
demonstration-bearing DSPy `Predict` program.

This is **demonstration optimization**. It optimizes what examples the model
sees before the new task. It is not instruction or skill optimization.

### What `copro` actually changes

The COPRO strategy starts with:

```text
[original solver instruction]
[original source skill]
[current task]
```

It generates and evaluates alternatives such as:

```text
Candidate A
  solver instruction: [rewritten instruction A]
  skill context:      [original source skill]
  demonstrations:    none

Candidate B
  solver instruction: [rewritten instruction B]
  skill context:      [rewritten skill candidate B]
  demonstrations:    none

Candidate C
  solver instruction: [rewritten instruction C]
  skill context:      [rewritten skill candidate C]
  demonstrations:    none
```

For every candidate, the configured solver answers permitted fitting cases and
the configured judge scores those answers against their locked rubrics. COPRO
uses the scores—not the hidden expected answers, rubric text, or judge
feedback—to propose the next breadth/depth round. The winning compiled state
contains the selected instruction, response prefix, and skill overlays, with
an empty demonstration list.

This is **prompt-and-skill optimization**. It optimizes the words that instruct
the model and the words that describe the selected SDK capability. It does not
optimize the number of solved examples.

### Why they are separate strategies

The strategies answer different experimental questions:

```text
labeled_few_shot:
  "Keep our instructions and skills unchanged. Does adding solved examples help?"

copro:
  "Keep the solver zero-shot. Which rewritten instructions and skills work best?"
```

Consequently, their candidate identities, model calls, budgets, compiled
state, invalidation, and reports are different. A workspace strategy choice is
not an implementation toggle between equivalent algorithms; it selects which
part of the solver program the experiment is allowed to change.

## Workspace contract

### Few-shot strategy

```yaml
experiments:
  optimize-solver:
    mode: optimize
    based_on: baseline
    optimizer:
      strategy: labeled_few_shot
      search:
        k: [1, 2, 4]
    cross_validation:
      folds: 5
      seed: 0
    budget:
      solver: {model_calls: 1000, tokens: 4000000}
      judge: {model_calls: 3000, tokens: 6000000}
      wall_seconds: 7200
```

`k=0` remains an automatically included baseline candidate. `k` must contain
unique positive integers. This strategy accepts no COPRO fields.

### COPRO prompt-and-skill strategy

```yaml
models:
  optimizer:
    name: qwen3.6:27b-coding
    deployment_revision: qwen3.6-27b-coding-local-2026-09-13
    reasoning_effort: none
    parameters:
      temperature: 0.7
      max_tokens: 4096
      timeout: 600

stages:
  optimize:
    executor: lan-inference
    model: optimizer
    reuse: {policy: exact}
    budget: {model_calls: 100, tokens: 400000, wall_seconds: 3600}

experiments:
  optimize-solver:
    mode: optimize
    based_on: baseline
    optimizer:
      strategy: copro
      targets:
        prompt: true
        skills: all
      search:
        breadth: 10
        depth: 3
        init_temperature: 1.4
      metric:
        name: rubric_mean_score
    cross_validation:
      folds: 5
      seed: 0
    budget:
      optimizer: {model_calls: 100, tokens: 400000}
      solver: {model_calls: 5000, tokens: 20000000}
      judge: {model_calls: 15000, tokens: 30000000}
      wall_seconds: 14400
```

The numbers above illustrate the schema and are not framework defaults. The
preflight command must calculate the planned upper bound before execution.

`targets.skills` accepts exactly one of:

```yaml
skills: all
```

or:

```yaml
skills:
  - data_publishing/meta_tables
  - data_publishing/time_index_table_updates
```

An empty list, an unknown skill, glob syntax, and a simultaneous `all` plus
list representation are invalid. `targets.prompt` must be a boolean. At least
one target must be enabled.

The `copro` strategy accepts no `k` field. The `labeled_few_shot` strategy
accepts no `targets`, `breadth`, `depth`, `init_temperature`, optimizer model,
or optimizer budget. Unknown and cross-strategy fields are configuration
errors rather than silently ignored options.

### Stable experiment interface

The CLI does not gain separate hard-coded commands for each optimizer:

```bash
uv run ms-agent-eval run optimize-solver \
  --workspace experiments/mainsequence-sdk/workspace.yaml
```

The selected experiment revision determines the strategy. The terminal must
state it before any model call:

```text
Optimization strategy: copro
Targets: solver prompt + 11 skills
Search: breadth 10 × depth 3
```

## What COPRO optimizes

### Solver prompt

The initial prompt candidate is the exact instruction from the base DSPy
solver signature. For the current solver, that starts as:

```text
Answer a task using only the locked repository instruction context.
```

COPRO proposes alternative instruction text and response-field prefixes. The
original instruction and prefix are always candidates, so search can retain
the baseline if generated alternatives are worse.

The solver signature field names and types remain fixed. Search cannot add
hidden inputs, remove `task`, change the output schema, or make judge-only
material visible to the solver.

### Skill content

Every selected source skill is an independent optimization target. The
framework presents its locked text to the COPRO instruction-proposal mechanism
as an optimizable instruction artifact. Proposed skill variants are then
supplied as `skill_context` when solving cases owned by that skill.

Skill search is per skill:

```text
locked skill text
      │
      ├── original candidate
      ├── COPRO candidate 1
      ├── COPRO candidate 2
      └── ...
              │
              ▼
 solve only training cases owned by that skill
              │
              ▼
       rubric judge scores
              │
              ▼
      selected skill candidate
```

The framework does not overwrite the source repository, imported snapshot, or
versioned experiment skill. A selected variant is a compiled experiment
artifact. Its source skill hash, candidate text hash, proposal provenance, and
scores remain recorded.

Skills not selected by `targets.skills` retain their original locked content.
If a selected skill has insufficient grouped training coverage for the
configured cross-validation, preflight fails before optimizer calls begin.

### Joint program

Prompt and skill candidates cannot be selected only in isolation because they
interact. The COPRO strategy therefore uses deterministic alternating search:

1. Evaluate the original prompt with original skills.
2. Search the global solver instruction while skills remain at their current
   selected versions.
3. Search each selected skill using the current selected solver instruction.
4. Evaluate the assembled prompt-and-skill bundle across the complete fitting
   corpus.
5. Use the scored candidates as the next depth round's proposal context.
6. Retain the best complete bundle, with the original bundle always eligible.

This avoids an unbounded Cartesian product while still scoring the assembled
program. Target order is canonical: prompt first, then skills sorted by stable
skill identifier. Equal scores prefer the previous candidate, then the
shorter normalized instruction, then the candidate content hash.

## Search and evaluation graph

Both strategies use the same outer data-safety lifecycle but materialize
different internal work nodes.

### Common outer lifecycle

```text
lock revision
     │
validate judge
     │
assign grouped folds within train
     │
fit/search independently on K-1 folds
     │
solve and judge the held-out fold
     │
aggregate out-of-fold diagnostics
     │
final fit/search on all train cases
     │
freeze compiled program
     │
solve and judge selected program on untouched test
     │
     └─ COPRO also solves and judges the frozen original for paired deltas
     │
aggregate and report
```

For `labeled_few_shot`, fit nodes compile deterministic demonstration state
for each configured `k` candidate.

For `copro`, the graph contains explicit proposal, candidate-program,
candidate-solve, candidate-judge, round-selection, and final-assembly work.
COPRO must not run as one opaque, uncheckpointed call. Every proposal and
candidate evaluation must be resumable and reusable under ADR-001 and ADR-003.

### Stage representation

Add `optimize` as a canonical model-driven stage. Its packets represent one
proposal operation for one target and round:

```yaml
stage: optimize
work_spec:
  strategy: copro
  operation: propose
  round: 1
  target:
    kind: skill
    id: data_publishing/time_index_table_updates
  parent_candidates:
    - sha256:...
  breadth: 10
  init_temperature: 1.4
```

Candidate evaluation continues to use ordinary `solve` and `judge` packets.
`select` packets choose round and final candidates deterministically. `fit`
packages the selected state. This preserves executor portability and allows
the endpoint or harness execution contract to be used for optimizer proposals
without hiding nested model calls inside a local process.

`labeled_few_shot` never creates `optimize` work. Its `fit` work remains a
deterministic local compilation.

## Metric contract

`rubric_mean_score` is computed from the configured LLM judge's validated,
structured criterion scores. The prompt optimizer never acts as its own judge.

For each candidate:

1. The solver receives only the candidate instruction, permitted context, and
   case solver view.
2. The judge receives the candidate response, locked rubric, and expected
   evidence through the judge view.
3. The framework validates the judge output and computes the rubric-weighted
   score.
4. Candidate selection consumes the numeric score and declared hard-failure
   state.

The optimizer proposal model may receive candidate instructions, aggregate
scores, and non-sensitive search statistics. It must not receive:

- expected responses or expected artifacts;
- rubrics or criterion anchors;
- judge feedback text;
- test or challenge inputs;
- test or challenge scores; or
- hidden case metadata.

This is stricter than passing an arbitrary Python metric directly into
`dspy.COPRO.compile`. The framework owns the metric boundary and exposes only
the minimum score signal needed for the next proposal round.

## Split and leakage rules

Permanent `test` and `challenge` cases are unavailable to both strategies
during fitting, search, selection, and final compilation.

Within `train`:

- leakage groups, not individual cases, are assigned to folds;
- a fold's optimizer proposals and candidate selection may use only its K-1
  fitting folds;
- its out-of-fold score uses only the held-out fold;
- skill candidates use only cases whose `case.skill` matches that skill;
- global prompt candidates may use all permitted fitting cases; and
- the final compiled program is produced from all `train` cases only after
  out-of-fold diagnostics complete.

The untouched test set evaluates the already frozen compiled program once. A
test score never changes the selected instruction, skill variant, search
parameter, or demonstration count.

## Budget and preflight

COPRO consumes three independent model budgets:

- `optimizer`: generates instruction and skill candidates;
- `solver`: answers fitting cases for candidate evaluation; and
- `judge`: scores candidate answers.

The budget ledger records the model role on every call. Calls cannot be hidden
inside DSPy. Exhausting any role budget stops new calls, persists completed
candidate evidence, and leaves the experiment resumable.

Preflight must report at least:

- selected strategy and targets;
- training cases and leakage groups available per target;
- folds and fitting/held-out sizes;
- maximum candidate proposals implied by breadth and depth;
- estimated solver and judge evaluations;
- configured per-role call, token, wall-time, and endpoint-concurrency limits;
- whether each selected skill has sufficient fold coverage;
- estimated maximum request context for candidate prompts; and
- a blocking error when the declared budgets cannot authorize the planned
  search upper bound.

The estimate must be conservative. An unavailable tokenizer produces the
warning/error behavior defined by ADR-005; it does not permit the framework to
pretend that an oversized prompt fits.

## Identity, caching, and invalidation

The experiment revision includes:

- optimizer strategy and normalized strategy configuration;
- base solver program hash;
- selected target skill identifiers and locked source hashes;
- fold policy and seed;
- metric and aggregation policy hashes;
- optimizer, solver, and judge model deployment identities;
- their output-affecting parameters;
- optimizer implementation and DSPy version; and
- stage executor/environment identities.

COPRO proposal work is keyed by:

```text
hash(
    strategy and implementation version,
    round and target,
    base or parent candidate hashes,
    optimizer model deployment and parameters,
    breadth and temperature,
    permitted fitting-corpus identity,
    output contract,
    execution environment
)
```

Candidate solver work is keyed by the candidate prompt hash, effective skill
hash, case solver-view hash, solver deployment, generation parameters, and
runtime identity. Candidate judge work follows ADR-003 and additionally
depends on the producing candidate identities.

Changing only the judge cannot reuse old judgments but may reuse candidate
proposals and solver responses. Changing only one source skill invalidates
that skill's candidate search, candidate solves that consume it, joint bundle
selection, and downstream test work; unrelated skill proposal work remains
eligible for reuse.

Changing from `labeled_few_shot` to `copro` creates a new experiment revision
and optimization graph. It never mutates or silently resumes the old strategy's
compiled state.

## Compiled program artifact

Every successful optimization run produces one strategy-tagged, state-only
compiled artifact.

For `labeled_few_shot`:

```text
compiled-program/
├── manifest.json
└── dspy-state.json        # selected demonstrations
```

For `copro`:

```text
compiled-program/
├── manifest.json
├── dspy-state.json        # selected instruction and output prefix
├── solver-instruction.md
├── skills/
│   └── <skill-id>.md      # only optimized skill overlays
├── candidate-lineage.json
└── search-summary.json
```

The manifest records:

- strategy and normalized configuration;
- base program, global context, and source skill hashes;
- selected instruction, prefix, and skill-overlay hashes;
- whether demonstrations are present;
- fitting corpus and fold-assignment hashes;
- optimizer, solver, and judge identities;
- DSPy and framework versions;
- selected candidate scores and deterministic tie-break evidence; and
- every constituent artifact reference.

For a COPRO artifact, `demonstrations` must be empty. For a
`labeled_few_shot` artifact, prompt and skill overlays must be absent. The
loader validates these invariants before execution.

## Reporting and recording

Human reports and recorder projections must identify the optimization strategy
before presenting scores.

Few-shot reports include:

- every `k` candidate;
- selected demonstrations and their case IDs;
- out-of-fold score per `k`; and
- the selected `k`.

COPRO reports include:

- initial and selected solver instruction;
- selected output prefix;
- source and selected hash for every optimized skill;
- breadth/depth rounds and candidate counts;
- per-round candidate score distributions;
- selected candidate lineage;
- optimizer, solver, and judge usage separately;
- out-of-fold diagnostic score; and
- untouched test score.

MLflow receives candidate and round tables, optimization-score graphs, compiled
artifacts, model usage, and trace links through ADR-006. Recorder state remains
an operational projection and does not affect optimization identity.

## Failure and resume semantics

An optimizer endpoint failure is a failed or retryable `optimize` attempt, not
a failed solver answer. Completed proposals, candidate solver responses, and
judgments remain reusable.

On resume, the coordinator reconstructs the selected strategy from the frozen
experiment revision and schedules only unresolved graph nodes. It never reads
the current workspace strategy in place of the frozen revision.

If COPRO produces malformed, duplicate, empty, or policy-violating candidates,
the framework records and rejects those candidates. The original prompt/skill
candidate remains available. Search fails only when no valid candidate bundle
can be selected or an explicit budget/coverage requirement is violated.

## Migration

The current optimizer schema is replaced without compatibility shims.

Current:

```yaml
optimizer:
  name: LabeledFewShot
  search:
    k: [1, 2, 4]
```

Required:

```yaml
optimizer:
  strategy: labeled_few_shot
  search:
    k: [1, 2, 4]
```

The workspace schema version must be incremented. Old optimizer payloads fail
with an actionable migration message. Existing immutable run artifacts remain
readable through their frozen run schema, but no runtime compatibility branch
accepts the removed workspace shape.

The MainSequence workspace may choose either strategy when the implementation
lands. This ADR does not require both optimization experiments to exist and
does not automatically migrate it to COPRO.

## Implementation requirements

1. Replace `OptimizerConfiguration` with a discriminated strategy union.
2. Add strict `LabeledFewShotConfiguration` and `CoproConfiguration` models.
3. Add the optimizer model role, budget, resolution, provenance, and reporting.
4. Add `optimize` to the portable stage contract and workspace stage schema.
5. Implement COPRO-compatible instruction and prefix proposal packets.
6. Implement per-skill candidate proposal, validation, and overlay artifacts.
7. Build explicit candidate solve/judge/select nodes rather than one opaque
   `COPRO.compile` call.
8. Enforce zero demonstrations in COPRO compiled state.
9. Enforce train-only search and group-aware outer-fold evaluation.
10. Add conservative search-cost and skill-coverage preflight.
11. Extend content-addressed identity and selective invalidation tests.
12. Extend terminal progress, reports, MLflow projection, and CLI documentation.
13. Migrate templates and example workspaces with no compatibility shims.

## Implemented design

Schema version 13 implements the discriminated optimizer union and rejects the
removed `optimizer.name` form. `labeled_few_shot` remains deterministic local
DSPy fitting and never resolves or calls the optimizer role for its experiment
lock. `copro` uses a typed DSPy proposal program on the portable `optimize`
stage, then materializes every candidate as ordinary fit, solve, judge, and
select work. It does not call stock `COPRO.compile` as an opaque nested loop.

The coordinator runs the alternating search independently inside each outer
training fold, records one held-out score per training case, completes a final
search on all train cases, and then loads the untouched test split. The final
search is explicitly dependent on the persisted cross-validation summary.
COPRO packaging contains no expected responses or rubrics and validates that
the saved DSPy state has zero demonstrations.

The final compiled manifest links the state JSON, selected solver instruction,
output prefix, skill overlays, candidate lineage, search summary, source skill
hashes, fold assignment, model identities, implementation version, and DSPy
version. Reports and MLflow projections expose the strategy, rounds, final
artifacts, and optimizer/solver/judge usage separately.

Run-local packet and per-packet transition indexes make stage lookup linear in
the number of reported packets and constant-time for new work identities. This
is necessary because a prompt-plus-all-skills COPRO graph can contain tens of
thousands of candidate solve/judge nodes. Earlier immutable runs remain
readable through their existing transition ledger.

## Acceptance criteria

The decision is implemented only when:

- an optimization experiment accepts exactly one supported strategy;
- running `labeled_few_shot` performs no optimizer proposal calls;
- running `copro` stores no demonstrations and executes no few-shot fitting;
- COPRO produces, evaluates, selects, freezes, reloads, and executes rewritten
  solver instructions;
- selected skills can be optimized independently and are stored as overlays
  without changing source files;
- unselected skills remain byte-identical to their locked sources;
- the original prompt and skill are always valid search candidates;
- test and challenge information has no transitive path to search or selection;
- every optimizer, solver, and judge call has a distinct role, budget charge,
  immutable input hash, model provenance, and reusable result;
- interruption and resume do not repeat completed candidate work;
- changing one skill invalidates only its affected search and downstream work;
- reports make the strategy and exact selected artifacts unambiguous;
- preflight blocks insufficient skill coverage and insufficient hard budgets;
- schema migration rejects the removed optimizer shape; and
- unit, leakage, fault-injection, resume, concurrency, and end-to-end tests pass
  on Python 3.12 or newer.

## Consequences

Positive consequences:

- the workspace explicitly states what kind of optimization will run;
- prompt-and-skill search becomes a first-class, auditable experiment;
- few-shot fitting remains available without being confused with prompt search;
- costly search work can resume and reuse individual candidate evaluations;
- optimized skills are reviewable artifacts rather than silent source changes;
  and
- untouched test data remains a final estimate rather than optimizer feedback.

Tradeoffs:

- COPRO search can require orders of magnitude more solver and judge calls than
  few-shot fitting;
- per-skill search requires enough grouped training cases for every selected
  skill;
- the staged COPRO implementation is more complex than calling
  `dspy.COPRO.compile` as one local function; and
- alternating prompt/skill search is bounded and auditable but does not explore
  the complete Cartesian product.

## Rejected alternatives

### Always run few-shot and COPRO

Rejected. It duplicates work, conflates two different interventions, obscures
cost, and makes the selected compiled program ambiguous.

### Treat COPRO as another `k` candidate

Rejected. `k` changes demonstrations; COPRO changes instructions and prefixes.
Their state, budgets, identities, and reports are not interchangeable.

### Let COPRO consume `skill_context` without skill search

Rejected as insufficient. Stock COPRO would optimize only how the predictor is
instructed to use the dynamic skill input; it would not optimize the skill text
itself.

### Let the optimizer read judge feedback or expected answers

Rejected. That creates a hidden-answer path and makes optimization results
invalid as evidence of generalization.

### Mutate imported or repository-owned skills

Rejected. Optimized variants belong to the compiled experiment artifact. The
locked source remains the immutable baseline and provenance root.

### Run stock COPRO as one opaque fit call

Rejected. A monolithic call hides candidate-level costs, prevents granular
resume and reuse, and weakens provenance and leakage audits.

### Infer a default optimizer

Rejected. An omitted strategy is ambiguous and must fail validation before any
model call.
