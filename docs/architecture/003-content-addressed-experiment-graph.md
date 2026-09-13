# ADR-003: Content-Addressed Experiment Graph and Stage Reuse

- Status: Accepted
- Date: 2026-09-13
- Decision owners: MS Agent Eval maintainers
- Scope: Experiment identity, incremental execution, cross-run stage reuse,
  invalidation, provenance, and artifact retention
- Refines: [ADR-001: Portable Staged Execution](001-portable-staged-execution.md)
- Complements:
  [ADR-002: Isolated Codex CLI Harness Execution](002-isolated-codex-cli-execution.md)

## Context

ADR-001 made every operation an immutable packet/result lifecycle, but its
initial packet identity was scoped to one run. `StagePacket.packet_id` included
the random `run_id`, and the original coordinator's work-matching logic
required both packets to belong to that same run. Starting a second run of an
unchanged experiment therefore repeated author, solver, judge, optimization,
and aggregation work. Only a resume of the original run reused its persisted
stages.

The content-addressed blob store avoids storing identical bytes twice, and
repository snapshots are reused by their immutable identity. Neither behavior
is computation reuse: a model call can still be paid for and executed again
before producing bytes already present in storage.

The experiment lock has the opposite problem. It captures a strong set of
inputs, but includes the identity hash of the entire workspace. A change to an
unrelated experiment or unused executor can therefore change every experiment
lock. Runs are then placed in one flat UUID namespace, leaving no explicit
distinction between:

- another execution of the same experiment definition;
- a new immutable revision of an existing experiment family; and
- a partially changed experiment that could reuse most prior work.

The framework must model an experiment as an incremental, content-addressed
graph. An exact stage result should be reusable by any experiment revision that
contains the same work node. Changing one input should invalidate that node and
only its downstream dependents.

## Decision

Adopt a content-addressed directed acyclic graph (DAG) for experiment planning
and execution. Separate reusable work identity from execution attempts and run
membership.

```text
immutable sources ─────┐
authoring policy ──────┼──► author work ──► candidate case
capability catalog ────┤                         │
author deployment ─────┘                         ▼
                                           reviewed case
                                                 │
                    ┌────────────────────────────┴─────────────┐
                    ▼                                          ▼
            solver-view hash                           judge-view hash
                    │                                          │
solver program ─────┤                                          │
solver deployment ──┼──► solver work ──► candidate response ──┤
compiled state ─────┘                                          │
                                                               ▼
calibration result ─────────────────────────────────────► judge work
judge program and deployment ───────────────────────────►      │
                                                               ▼
                                                       evaluation result
                                                               │
                                                               ▼
                                                        aggregate work
                                                               │
                                                               ▼
                                                       experiment root
```

The graph topology and static experiment revision are planned before
execution. Nodes whose keys depend on stochastic upstream outputs are resolved
as those outputs become available. The coordinator classifies every resolved
node as a cache hit, cache miss, explicitly refreshed node, or requested
independent replicate. Only misses, refreshes, and replicates are executed.

## Terminology and identities

- **Experiment family**: a stable configured name such as `baseline` or
  `optimize-few-shot`.
- **Experiment revision**: one immutable effective graph rooted at an
  `experiment_revision_id`.
- **Work node**: a canonical description of one stage computation independent
  of a run.
- **Work ID**: hash of everything capable of changing the semantic output of a
  work node.
- **Attempt**: one execution of a work node under a specific resource and
  lease context.
- **Attempt ID**: identity of one execution or requested replicate.
- **Result ID**: content identity of one validated result and its provenance.
- **Run**: a user-requested materialization of an experiment revision. A run
  references reused and newly executed graph nodes; it does not own them.

The identities form this relationship:

```text
experiment family
    └── experiment revision = hash(graph root)
            ├── work node A = hash(canonical computation A)
            │       ├── attempt 1 ──► validated result
            │       └── attempt 2 ──► validated result
            └── work node B = hash(canonical computation B + result A)
                    └── attempt 1 ──► validated result
```

## Work identity

A work ID must exclude `run_id`, timestamps, leases, worker names, credentials,
and mutable filesystem paths. Its canonical identity contains:

```yaml
schema_version: 1
stage: solve
inputs:
  case_solver_view: sha256:...
  instruction_context: sha256:...
dependencies:
  calibration_result: sha256:...
  compiled_solver_state: sha256:...
program:
  id: dspy-solver
  content_hash: sha256:...
model_deployment: sha256:...
generation_parameters: sha256:...
execution_environment: sha256:...
instructions: sha256:...
output_contract: solver-response-v1
```

`work_id` is the canonical hash of this document. Dependency entries identify
the validated dependency result or its required output artifact, never a
run-scoped packet ID.

Budgets, leases, worker identity, start time, and attempt-local resource limits
belong to the attempt unless they alter model-visible behavior. Output-token
limits, tool availability, sandbox-visible files, instruction policy, and
other output-affecting runtime properties belong to the work identity through
generation parameters or the execution-environment hash.

## Packet and attempt separation

Portable stage packets remain the executor contract, but packet schema v2 must
carry both identities:

```yaml
work_id: sha256:...       # reusable computation
attempt_id: sha256:...    # this execution or replicate
run_id: ...               # membership and accounting only
```

The packet ID may identify the attempt envelope, but it must no longer be used
as the reusable computation key. A retry of failed infrastructure can retain
the work ID while receiving a new attempt ID. A requested independent sample
also retains the work ID's canonical inputs but uses an explicit replicate
slot and a new attempt identity.

## Stage-specific keys

### Author

Author work is keyed by:

- target and reference snapshot hashes;
- selected source evidence and skill instructions;
- case-authoring policy and capability-catalog hashes;
- campaign request;
- author DSPy program and output contract;
- author model deployment and generation parameters; and
- model-visible execution environment.

An exact validated candidate package can be reused. Human review and promotion
remain a separate boundary: a cached author output is never silently promoted
into the versioned case bank.

### Judge calibration

Calibration model work is keyed by fixture ids, candidate-response hashes,
judge-view projections of its cases, judge program, repetitions, judge
deployment, generation parameters, and execution environment. Private labels
and acceptable score ranges are not shown to the judge and do not force the
same scores to be regenerated; the coordinator recomputes the gate and its
calibration-corpus hash whenever compatible score output is attached. Only a
validated passing calibration result is published as reusable work. A failed
calibration remains run evidence, not a reusable success gate.

### Solver

Solver work is keyed by the case's solver view, permitted global and skill
context, attachments, solver program, compiled state when present, solver
deployment, generation parameters, and model-visible execution environment.

Rubrics, expected responses, expected artifacts, judge feedback, and other
hidden judging material must never enter the solver work key or cache lookup.

### Judge

Judge work is keyed by:

```text
hash(
    case judge-view hash,
    candidate response content hash,
    producing solver work and model-deployment hashes,
    judge calibration result hash,
    judge program hash,
    judge model deployment hash,
    judge generation parameters,
    repetitions,
    output contract,
    model-visible execution environment
)
```

The candidate response hash and producing solver work/model identity are
mandatory. Model name plus case identity is not sufficient: stochastic calls
from the same solver may produce different responses and therefore require
different judgments.

### Optimize

Optimization work is keyed by the ordered train-case projections, permitted
validation evidence, optimizer implementation and parameters, teacher and
student deployment identities where applicable, base solver program, seed,
and output contract. Test and challenge material is forbidden from both the
node inputs and its transitive dependency graph.

### Final evaluation and aggregate

The final-evaluation boundary is keyed by the published compiled state,
validation decisions, and held-out assignment hash. Aggregate work is keyed
by the ordered validated judgment result IDs, split assignments, benchmark or
pilot mode, and aggregation implementation/policy hash.

## Case projections

One monolithic case hash remains useful for complete package provenance, but
is too coarse for incremental reuse. Every reviewed case must expose these
canonical projections:

- `solver_view_hash`: prompt, permitted context and solver attachments;
- `judge_view_hash`: rubric, expected response, expected artifacts and judging
  context;
- `metadata_hash`: title, classification and reporting-only metadata; and
- `case_hash`: the complete reviewed package.

This preserves the information firewall while providing precise invalidation.
For example, changing a rubric invalidates judgments and aggregation without
discarding an otherwise identical solver response. Changing only a title does
not cause a model call.

## Model deployment identity

A model name or mutable alias is not an adequate cache boundary. The deployment
identity must include all available output-affecting properties:

- model name and immutable weights digest or deployment revision;
- quantization, tokenizer and chat-template revision;
- inference backend and relevant version;
- reasoning effort;
- temperature, seed, maximum output tokens and all decoding parameters;
- tool and model-visible runtime configuration; and
- provider-specific compatibility settings.

When an endpoint cannot expose an immutable model digest, configuration must
require a user-controlled `deployment_revision`. The operator must change it
whenever weights, quantization, serving template, or other hidden deployment
state changes. Results without a trustworthy deployment identity are not
eligible for cross-run reuse.

Credentials and authentication tokens never enter deployment identities.

## Reuse and replication policy

Every stage has an explicit policy. The initial policies are:

```yaml
reuse:
  policy: exact     # use an eligible validated result for the same work_id
```

```yaml
reuse:
  policy: refresh   # execute again even when an eligible result exists
```

```yaml
reuse:
  policy: replicate
  samples: 3        # request independent attempts for variance measurement
```

An exact cache hit is not a new statistical observation. A benchmark report
must distinguish a materialized result from a fresh replicate. Repetition
slots, configured seeds, and sampling intent must be explicit so memoization
cannot silently collapse a variance study.

By default, only results that are completed, schema-valid, stage-valid,
content-verified, and durably persisted are reusable. Failed, rejected,
partially written, untrusted, or unverifiable results are never cache hits.

## Planning and execution

The coordinator follows this lifecycle:

```text
compile effective graph
        │
        ▼
resolve exact work IDs
        │
        ├── eligible result exists ──► attach cache hit to run
        │
        └── no eligible result ──────► claim work lease
                                              │
                                              ▼
                                      execute and validate
                                              │
                                              ▼
                                      publish reusable result
        │
        ▼
materialize aggregate and run provenance
```

Claims are coordinated by work ID. Concurrent runs requesting the same miss
must use a single-flight lease: one worker executes while the others wait and
then verify and attach the published result. A failed or expired lease permits
a new attempt without changing the work ID.

The CLI displays the static experiment revision before execution and then
labels resolved nodes as `REUSED`, `RUNNING`, `WAITING`, `FAILED`, or
`COMPLETED`. It records the source result and source run for every reuse. The
terminal summary reports fresh and reused node counts separately.

## Experiment revision identity

An experiment revision is the Merkle root of the selected experiment's
immutable effective definition. It is known before stochastic work executes:

```text
experiment_revision_id = hash(
    selected experiment configuration,
    immutable source, case, split, and calibration identities,
    program, model-deployment, executor, and reuse identities,
    aggregation policy
)
```

Each run also records a `run_graph_id` over the work/result nodes it actually
materialized. This second identity distinguishes stochastic refreshes and
replicates within the same experiment revision without pretending that their
outputs were knowable when the revision was created.

The full workspace hash remains provenance but is not part of semantic
equivalence. Only the selected experiment's transitive configuration can
change its revision. Editing an unrelated experiment or unused executor does
not invalidate this graph.

The revision is displayed as `<experiment-family>@<short-hash>` while the full
digest remains authoritative. A second run of an identical graph becomes
another run of the same revision. A relevant change creates a new revision
that can still reference unchanged nodes from previous revisions.

## Immutable experiment bundle and resume

Creating an experiment revision persists a schema-v2 definition before any
stage executes. It contains the selected experiment configuration rather than
the entire mutable workspace, CAS references for every loaded case,
calibration, catalog, split, and authoring-policy file, the target and
authoring-source snapshot identities, reconstructed context, validation
result, case projections, judging material, model and execution identities,
output contracts, and split assignments.

Resume loads that stored bundle rather than recompiling future stages from the
current mutable workspace. It verifies every definition/file artifact and
external repository snapshot, materializes the local inputs in a disposable
directory, recompiles the lock, and requires the resulting revision hash to
match the run before continuing. The same rehydration rule applies to stage
preparation, automatic packet execution, harness submission, and terminal
report publication. An old run therefore remains resumable after the working
copy changes. Starting a new run from the current workspace compiles a new
revision while retaining reuse access to unchanged work nodes.

## Storage

Shared content remains global inside one external data root; experiment
namespaces contain references rather than copies:

```text
operations/
├── blobs/sha256/
├── snapshots/
├── locks/work/
└── manifests/
    ├── graph/
    │   ├── work/<work-id>/work.json
    │   ├── attempts/<attempt-id>.json
    │   └── results/<result-id>.json
    ├── experiments/
    │   └── <family>/<revision-hash>/
    │       ├── experiment.lock.json
    │       ├── bundle.json
    │       └── runs/<run-id>.json
    └── runs/
        └── <run-id>/run.json

summary/
└── runs/<run-id>/...
```

The exact result index may map one work ID to multiple eligible results when
replication is intentional. Selection is deterministic under the declared
reuse policy.

Garbage collection treats retained experiment graphs, runs, attempts, and
summaries as roots. Shared nodes and blobs are deleted only when no retained
root can reach them.

## Provenance and accounting

Every reused node records:

- work ID and selected result ID;
- original attempt, run, executor and model deployment;
- validation and artifact-verification status;
- reuse policy and selection decision; and
- the run that consumed the result.

Reports separate `executed_this_run` from `reused_this_run`. Token counts,
latency and configured cost remain attributed to the original execution;
current-run incremental usage is zero for a cache hit. Historical usage must
not be reported as newly consumed capacity.

## Invalidation examples

| Change | Reusable | Invalidated |
| --- | --- | --- |
| Judge model or reasoning changes | Author outputs and solver responses | Calibration, judgments, aggregate |
| Rubric or expected response changes | Author source evidence and solver responses | Judgment for that case, aggregate |
| Solver deployment changes | Cases and compatible judge calibration | Solver responses, dependent judgments, aggregate |
| Case prompt changes | Source snapshots and unrelated cases | Solver response, judgment, aggregate |
| Reporting title changes | All model results | Human summary only |
| Split assignment changes | Compatible solver and judge results | Final gate and aggregate |
| Author model changes | Source snapshots and policy | Candidate author output and anything based on newly promoted cases |
| DSPy compiled state changes | Cases and judge calibration | Optimized solver responses and downstream judgments |
| Aggregation implementation changes | All validated judgments | Aggregate only |

## Security and trust boundaries

Reuse must preserve ADR-001 information firewalls and ADR-002 isolation:

- solver cache lookup uses only solver-visible projections;
- judge lookup can consume the candidate output and judge-only material for
  its assigned case, but cannot access unrelated candidates or cases;
- author lookup cannot depend on solver or judge outputs;
- optimizer lookup rejects held-out transitive dependencies;
- only coordinator-validated result indexes are authoritative; and
- candidate repositories and model outputs cannot publish trusted cache index
  entries directly.

A content hash proves byte identity, not correctness or trust. Reuse requires
both content verification and validated coordinator provenance.

## Migration

This is a schema boundary, not a compatibility layer. New graph nodes, packets,
attempts, and results use new schemas and namespaces. Existing run-scoped
packets remain historical evidence but are not automatically treated as cache
entries because their reusable work identity cannot be proven from `packet_id`.

An offline migration may admit an old result only if it can reconstruct the
complete canonical work identity, verify every dependency and artifact, and
produce the new provenance record. Otherwise the old run remains readable and
the corresponding work executes once under the new graph. Runtime compatibility
shims are not introduced.

## Consequences

### Positive

- Unchanged model work is reused across runs and experiment revisions.
- A small workspace change recomputes only affected downstream nodes.
- Solver outputs can be judged by multiple judge deployments without rerunning
  the solver.
- Rubric changes do not unnecessarily regenerate candidate responses.
- Old revisions remain independently resumable after workspace edits.
- Experiment history becomes organized by family, immutable revision, and run.
- The CLI can show exact cache hits, misses, incremental calls, and provenance.

### Costs and risks

- Identity schemas and stage projections become security-critical APIs.
- Model deployment fingerprinting requires provider cooperation or disciplined
  operator-supplied revisions.
- Concurrent cache publication requires leases and atomic indexes.
- Replication and reuse policies must be explicit to avoid invalid statistics.
- Graph traversal, retention and garbage collection become more complex.

These costs are accepted because rerunning every stage is financially wasteful,
obscures experiment lineage, and prevents the framework from operating as an
incremental evaluation and training system.

## Rejected alternatives

- **Cache by model name and case ID**: unsafe because prompts, programs,
  parameters, deployments, rubrics and stochastic responses can differ.
- **Cache the entire experiment only**: cannot reuse unaffected stages after a
  partial change.
- **Use provider-side caching as the experiment cache**: provider caching does
  not establish portable result identity, validation, lineage, or retention.
- **Copy every artifact into every experiment directory**: wastes storage and
  creates conflicting ownership instead of a shared immutable graph.
- **Keep `run_id` in the work key**: preserves resume behavior but makes
  cross-run reuse impossible.
- **Always reuse when hashes match**: invalidates deliberate replication and
  variance measurement.
- **Always rerun model stages**: needlessly spends capacity and defeats the
  incremental graph requirement.
