# ADR-011: Stateful Optimization and Semantic Work Reuse

- Status: Accepted
- Date: 2026-09-15
- Supersedes: ADR-007 sections that require independent K-fold searches and a
  separate final COPRO search
- Refines: ADR-003 work-identity rules and ADR-008 optimization reporting

## Context

The original optimization graph treated every cross-validation fold as an
independent search. With `K` folds it produced `K` unrelated winners, discarded
them as initialization evidence, and then started another search from the
original prompt and skills. That design multiplied optimizer, solver, and judge
calls while failing to carry useful prompt or skill improvements forward.

The graph also included orchestration coordinates such as fold, round,
candidate placement, producer packet, and run revision in identities for some
model calls. Two calls with the same model-visible request could therefore have
different `work_id` values and execute twice. Those coordinates describe where
a call occurred; they do not change what the model received.

## Decision

### One fixed validation holdout

An optimization experiment derives one immutable, group-safe validation
holdout from the permanent `train` split:

```yaml
validation:
  strategy: holdout
  fraction: 0.2
  seed: 0
```

The assignment is deterministic, stratified by skill when a skill owns at
least two leakage groups, and hashed into the experiment evidence. Singleton
skills remain in optimization training when possible. COPRO preflight is
stricter: every selected skill must have both optimization-training and
validation evidence. `test` and `challenge` remain unavailable until a winner
is frozen.

The term **validation** is used throughout the active interface. There is no
permanent validation split and no “development cases”; validation is derived
inside permanent train.

### COPRO is one stateful search

COPRO may divide optimization-training groups into deterministic sequential
partitions:

```yaml
optimizer:
  strategy: copro
  targets: {prompt: true, skills: all}
  search:
    breadth: 3
    depth: 2
    partitions: 2
    init_temperature: 1.4
  metric: {name: rubric_mean_score}
```

Partitions limit the fitting corpus seen at one time; they are not independent
cross-validation folds. The winner and scored proposal history from partition
`N` initialize partition `N+1`. Within each target and round, candidates are
scored on the same fixed validation holdout. After the last partition, the
coordinator materializes that winner once on all optimization-training cases.
It must not restart from the original bundle.

```mermaid
flowchart LR
    Train[Permanent train groups] --> Split[Fixed group-safe validation assignment]
    Split --> Fit[Optimization-training groups]
    Split --> Validation[Fixed validation groups]
    Fit --> P1[Partition 1 search]
    P1 -->|winner bundle + scored history| P2[Partition 2 search]
    P2 -->|final winner| FinalFit[Materialize once on all optimization training]
    Validation --> P1
    Validation --> P2
    FinalFit --> Frozen[Frozen optimized program]
    Frozen --> Test[Paired untouched test]
```

`labeled_few_shot` is simpler: each configured `k` plus `k=0` is fit once on
optimization training, scored on the fixed validation set, and selected by
mean validation score. The selected `k` is then materialized once for test.

### Work identity is semantic; attempt identity is operational

`work_id` answers: “Would this executor and model receive the same meaningful
request and produce an interchangeable result?” `attempt_id` answers: “Where
and when was that work requested in this run?”

The following belong only to the attempt envelope and provenance unless they
alter model-visible content:

- run and experiment revision identifiers;
- phase, partition, round, and candidate placement;
- producer packet and attempt identifiers;
- judge-calibration gate packet;
- DAG parentage used only to enforce execution order.

The following belong to semantic work identity:

- exact model-visible prompt/program/context and attachments;
- fitted state or candidate bundle content;
- response content judged and the exact judge view;
- model profile, deployment revision, parameters, executor adapter, and
  model-visible limits;
- output schema and semantic dependency artifact hashes.

For solver work, case hash alone is insufficient: the identity includes the
rendered global context, skill context or overlay, task, attachments, program
hash/state, model, parameters, and replicate slot. For judge work, identity
includes the candidate response hash, exact rubric/expected evidence, judge
program, repetitions, model, and parameters. It does not include which solver
packet produced identical response bytes. For optimizer proposals, identity
includes exactly the proposal fields the optimizer receives: target kind and
id, current target text and output prefix, scored history, rewrite
requirements, requested candidate count, optimizer program, and temperature.
The parent-bundle and fitting-corpus hashes remain orchestration metadata; they
affect downstream fit and scoring work when their actual content changes, but
must not make an otherwise identical proposal request a new work node.

When a semantic result is reused in another orchestration location, the
coordinator writes a new persisted result whose output metadata is rebound to
the current phase/partition/candidate and whose provenance points to the source
result. The semantic payload is not modified.

### Calibration is one gate

Judge calibration runs once per judge calibration/model/program identity and
gates the experiment. It is not repeated per partition or optimization round.
Changing only the gate packet does not invalidate unrelated judge calls whose
exact judge inputs are unchanged.

### Reporting

Reports expose:

- the fixed validation assignment hash and validation case count;
- sequential training partition count;
- proposal/selection history in execution order;
- the final selected bundle and compiled program;
- executed versus reused work by stage; and
- paired baseline-versus-optimized scores on untouched test cases.

The active table/artifact name is `optimization-validation`; “fold” and
“out-of-fold” are not used for current runs.

### Required workload preflight

Before a person starts a run, `preflight <experiment>` expands the complete
logical graph. It reports permanent and derived case sets, the one-time judge
gate, every optimizer target/round/candidate, per-partition validation work,
final test arms, deterministic fit/select/aggregate operations, model routes,
endpoint capacity, and fresh-run model calls by stage and role. A bare final
case count is not an adequate preflight.

The displayed call total is a conservative fresh-run upper bound. It does not
subtract speculative cache hits: exact semantic reuse can be decided only once
the corresponding stage packet and its `work_id` exist. Token-fit checks remain
separate because later optimizer and judge messages depend on outputs that do
not exist before execution. Interactive `run` prints a compact projection of
the same workload before the first model-driven stage.

## Configuration and migration

Workspace schema 15 replaces `cross_validation` with `validation` and adds
`optimizer.search.partitions` for COPRO. Newly authored schema-13/14 workspaces
remain rejected. A frozen schema-13/14 payload may be translated internally so
the current parser can inspect it: its legacy fold count maps to the proposed
partition count and its seed maps to a 20% fixed holdout. Resume is allowed
only if the translated experiment still reproduces the frozen lock identity.
Because optimization selection semantics changed, an unfinished legacy
optimization run normally requires a new schema-15 run; its earlier evidence
remains immutable and may still supply semantic cache hits. The framework must
not relabel a legacy run as if it had executed the new protocol.

## Consequences

- Optimization evidence now accumulates instead of being discarded at fold
  boundaries.
- Fixed validation reduces selection cost and makes candidate comparisons
  directly paired.
- Semantic cache reuse operates across partitions, rounds, runs, and experiment
  revisions when the actual request is identical.
- A validation holdout uses less repeated resampling than K-fold
  cross-validation. This is an intentional speed/iteration trade-off; final
  quality claims still come only from untouched test cases.
- Changing validation fraction/seed, optimizer history, bundle text, model,
  parameters, source/case content, or judge inputs invalidates only downstream
  semantic nodes.

## Required invariants

1. No test/challenge content enters proposal, fitting, validation, or selection.
2. Leakage groups never cross optimization-training and validation.
3. COPRO winners and score history carry into the next partition.
4. The final COPRO program is the last stateful winner; no reset search occurs.
5. Placement-only metadata cannot change `work_id`.
6. Reused results retain source provenance and current orchestration metadata.
7. Validation selection and final test results remain separately identifiable.
