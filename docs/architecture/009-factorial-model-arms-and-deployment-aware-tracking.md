# ADR-009: Factorial Model Arms and Deployment-Aware Global Tracking

> Workspace schema 15 and the current validation/search lifecycle are defined
> by [ADR-011](011-stateful-optimization-and-semantic-reuse.md). Schema 14 below
> records the configuration introduced by this decision.

- Status: Accepted and implemented
- Date: 2026-09-14
- Decision owners: MS Agent Eval maintainers
- Scope: model deployments, endpoint admission, per-experiment role bindings,
  cross-model experiment arms, comparable benchmark identity, and global MLflow
  tracking
- Extends:
  [ADR-003: Content-Addressed Experiment Graph](003-content-addressed-experiment-graph.md),
  [ADR-007: Configurable DSPy Optimization Strategies](007-configurable-dspy-optimization-strategies.md),
  and
  [ADR-008: Paired COPRO Evaluation](008-paired-copro-evaluation-and-formal-reporting.md)
- Refines:
  [ADR-004: Pooled Endpoint Concurrency](004-pooled-endpoint-concurrency.md)
  and
  [ADR-006: Experiment Recorder Projection](006-experiment-recorder-projection.md)

## Context

The current workspace binds one model and executor globally to each model-driven
stage:

```yaml
stages:
  optimize: {executor: lan-inference, model: optimizer}
  solve: {executor: lan-inference, model: solver}
  judge: {executor: lan-inference, model: judge}
```

That shape can run one solver, one optimizer, and one judge configuration. It
cannot represent independent experimental factors such as:

- Qwen solving with a Qwen optimizer;
- Qwen solving with a GLM optimizer;
- GLM solving with a Qwen optimizer; and
- GLM solving with a GLM optimizer.

Copying the entire workspace for every combination is not an acceptable model.
It duplicates the repository, cases, splits, rubrics, executor inventory, and
recorder configuration; encourages drift; and obscures which graph nodes can be
reused. A change to only the optimizer must not invalidate an unchanged solver
baseline.

The recorder projection has a related grouping problem. The current default
MLflow experiment name is:

```text
ms-agent-eval/{workspace_id}/{experiment_id}
```

This separates every baseline and optimization arm into another MLflow
Experiment, making a workspace-level model leaderboard or optimizer comparison
difficult. At the same time, putting every run into one unqualified comparison
would be invalid when cases, splits, rubrics, instructions, or scoring policy
change.

Endpoint routing also has an enforcement gap. An endpoint executor currently
declares physical replicas and capacities but does not declare which model
deployment each endpoint serves:

```yaml
executors:
  lan-inference:
    type: endpoint
    adapter: openai_compatible
    endpoints:
      - id: llama-cpp-17-18081
        base_url: http://192.168.1.17:18081/v1
        max_concurrent_requests: 2
```

ADR-004 requires every endpoint selected for one request profile to serve an
equivalent deployment, but the current router sends any model attached to the
executor to any endpoint in that executor. The rule is therefore an operator
promise rather than a validated routing constraint. This becomes incorrect as
soon as one endpoint serves only Qwen and another serves only GLM.

## Decision

MS Agent Eval will separate five identities that are currently conflated:

1. **Benchmark family**: the stable workspace, such as `mainsequence-sdk`.
2. **Benchmark revision**: the immutable evaluation corpus and protocol.
3. **Model deployment**: one semantic set of weights and serving behavior.
4. **Experiment arm**: one solver, optional optimizer, judge, and strategy
   combination evaluated against a benchmark revision.
5. **Execution run**: one requested materialization of an arm's graph.

Physical endpoints will explicitly advertise deployment routes. Model profiles
will select a deployment and add role-specific generation behavior. Experiments
will bind profiles independently to solver, optimizer, and judge roles. MLflow
will use one Experiment per benchmark family and will group comparable arms by
immutable revision tags and explicit comparison manifests.

```text
                    benchmark family: mainsequence-sdk
                                  │
                 benchmark revision: protocol-abc123
                                  │
                   Qwen baseline solver responses
                                  │
                  ┌───────────────┴────────────────┐
                  │                                │
          Qwen optimizer branch             GLM optimizer branch
                  │                                │
        Qwen candidate responses           Qwen candidate responses
                  │                                │
                  └──────── fixed judge ───────────┘
                                  │
                       cross-arm comparison
                                  │
                       global MLflow Experiment
```

The solver and optimizer are independent roles. The solver is the target whose
behavior is measured. The optimizer proposes prompt, prefix, or skill changes
for that target. They may use the same deployment, but the framework will not
require it.

## Deployment-aware endpoint routing

### Deployment inventory

Workspace schema 14 introduces top-level `deployments`. A deployment owns
semantic model identity and identifies the executor through which it is
available. Endpoint entries declare concrete routes for the deployments they
actually serve.

```yaml
executors:
  lan-inference:
    type: endpoint
    adapter: openai_compatible
    endpoints:
      - id: llama-cpp-17-18081
        base_url: http://192.168.1.17:18081/v1
        max_concurrent_requests: 2
        routes:
          - deployment: qwen-27b-local
            model: qwen3.6:27b-coding

      - id: llama-cpp-17-18080
        base_url: http://192.168.1.17:18080/v1
        max_concurrent_requests: 2
        routes:
          - deployment: glm-local
            model: glm-coding

      - id: llama-server-10-11434
        base_url: http://192.168.1.10:11434/v1
        max_concurrent_requests: 4
        routes:
          - deployment: qwen-27b-local
            model: qwen3.6:27b-coding

deployments:
  qwen-27b-local:
    executor: lan-inference
    model: qwen3.6:27b-coding
    revision: qwen3.6-27b-coding-q4-k-m-2026-09-14

  glm-local:
    executor: lan-inference
    model: glm-coding
    revision: glm-coding-q4-k-m-2026-09-14
```

`routes[].model` is the exact model identifier sent to that endpoint. This
permits two equivalent replicas to expose the same deployment through different
server aliases without weakening the deployment identity. An endpoint serving
several models declares several routes. Its physical
`max_concurrent_requests` remains shared across all routes; capacity is never
duplicated per model.

Harness deployments use the same semantic boundary without endpoint routes:

```yaml
executors:
  local-codex:
    type: harness
    adapter: codex_sdk

deployments:
  codex-author:
    executor: local-codex
    revision: gpt-5.6-sol-codex-subscription-2026-09-14
    model: gpt-5.6-sol
```

The exact harness fields remain governed by ADR-001 and ADR-002. The important
rule is that a model profile resolves through a declared deployment rather than
combining an arbitrary model string with an unrelated executor.

### Model profiles

Model profiles contain role-specific inference behavior and reference a
deployment. Model name and deployment revision no longer appear redundantly on
every role profile.

```yaml
models:
  qwen-solver:
    deployment: qwen-27b-local
    reasoning_effort: none
    parameters:
      temperature: 0.2
      max_tokens: 4096
      timeout: 600

  qwen-optimizer:
    deployment: qwen-27b-local
    reasoning_effort: none
    parameters:
      temperature: 0.7
      max_tokens: 4096
      timeout: 600

  glm-optimizer:
    deployment: glm-local
    reasoning_effort: none
    parameters:
      temperature: 0.7
      max_tokens: 4096
      timeout: 600

  fixed-judge:
    deployment: qwen-27b-local
    reasoning_effort: none
    parameters:
      temperature: 0.0
      max_tokens: 2048
      timeout: 300
```

Different profiles may share one deployment. Their temperature, reasoning
effort, output limit, and other output-affecting fields remain distinct parts of
model-call and work identity.

### Admission and validation

For a model call, the coordinator resolves:

```text
experiment role
    → model profile
        → deployment
            → executor
                → endpoints with an exact route for that deployment
```

Only matching endpoints are eligible for the request. Effective concurrency is:

```text
sum(capacity of matching endpoint routes currently available)
```

The following configuration conditions fail before solver, optimizer, or judge
generation:

- a profile references an unknown deployment;
- the deployment references an incompatible or unknown executor;
- an endpoint executor has no route for the required deployment;
- two routes on one endpoint duplicate the same deployment;
- an optimization arm names a baseline outside its comparison; or
- arms in one named comparison use different judge profiles.

The explicit workspace route is the operator's attestation. Setup can inspect
`/models` when an endpoint exposes it, but runtime routing does not treat a
mutable server model listing as proof of weights. The framework cannot
cryptographically prove weights, quantization, tokenizer, chat template, or
backend equivalence. Operators must use different deployment revisions when
any output-affecting serving property differs.

In particular, two servers running the same public model name are not
automatically replicas. If llama.cpp and another llama server use different
quantization, templates, tokenizer behavior, or inference semantics, they must
be separate deployments and therefore separate experimental conditions.

Endpoint id, URL, capacity, and equivalent replica membership remain
attempt-level routing properties. Adding or resizing a genuinely equivalent
replica does not invalidate semantic work. Changing a deployment revision or a
profile's output-affecting parameters does.

## Experiment-level role bindings

Model-driven stage bindings move from global `stages` entries into each
experiment. Stage configuration retains execution policy, reuse policy, and
budgets. Process-only stages may retain an explicit process executor.

```yaml
stages:
  validate_judge:
    reuse: {policy: exact}
    budget: {model_calls: 100, tokens: 250000, wall_seconds: 1800}
  optimize:
    reuse: {policy: exact}
    budget: {model_calls: 1, tokens: 32000, wall_seconds: 600}
  solve:
    reuse: {policy: exact}
    budget: {model_calls: 1, tokens: 32000, wall_seconds: 600}
  judge:
    reuse: {policy: exact}
    budget: {model_calls: 3, tokens: 64000, wall_seconds: 600}
  aggregate:
    executor: local-process
    reuse: {policy: exact}
    budget: {model_calls: 0, tokens: 0, wall_seconds: 300}

experiments:
  qwen-baseline:
    mode: evaluate
    roles:
      solver: qwen-solver
      judge: fixed-judge
    solver: {program: dspy}
    runtime: {profile: python-3.12}
    repetitions: 1

  qwen-with-qwen-copro:
    mode: optimize
    based_on: qwen-baseline
    roles:
      solver: qwen-solver
      optimizer: qwen-optimizer
      judge: fixed-judge
    optimizer:
      strategy: copro
      # Existing COPRO search, validation, and budget fields remain here.

  qwen-with-glm-copro:
    mode: optimize
    based_on: qwen-baseline
    roles:
      solver: qwen-solver
      optimizer: glm-optimizer
      judge: fixed-judge
    optimizer:
      strategy: copro
```

An evaluate arm must bind `solver` and `judge`. A COPRO arm must bind `solver`,
`optimizer`, and `judge`. Judge validation automatically uses the bound judge;
candidate and final-test generation automatically use the bound solver. No
model-driven stage may fall back silently to a global model.

The case-authoring campaign selects its author profile separately because case
authoring produces reviewed benchmark inputs rather than a solver comparison
arm.

## Comparison sets and factorial studies

Comparisons are explicit versioned intent. The framework will not infer a
scientific comparison merely because two runs happen to exist.

```yaml
comparisons:
  copro-optimizer-models:
    baseline_experiments:
      - qwen-baseline
      - glm-baseline
    optimization_experiments:
      - qwen-with-qwen-copro
      - qwen-with-glm-copro
      - glm-with-qwen-copro
      - glm-with-glm-copro
    primary_metric: score.mean
    grouping:
      - solver
      - optimizer
```

Schema 14 supports `score.mean` as the comparison metric. Grouping dimensions
must be a unique subset of `solver` and `optimizer`. Every optimization arm's
`based_on` experiment must be included in the comparison's baseline list, and
all listed arms must use one fixed judge profile.

Explicit arms are preferred to implicit Cartesian expansion. They make omitted
conditions, budgets, strategies, and model revisions reviewable in Git. A CLI
may later scaffold the Cartesian product, but it must write the expanded arms
before execution.

Changing only the optimizer from Qwen to GLM has this invalidation boundary:

```text
repository, cases, splits, baseline program ───────────────── reused
Qwen baseline solver responses ────────────────────────────── reused
fixed-judge calibration ───────────────────────────────────── reused
optimizer proposals ───────────────────────────────────────── new
Qwen responses to new candidate programs ──────────────────── new
judgments of new responses ────────────────────────────────── new
selected compiled program and paired final test ───────────── new
cross-arm comparison projection ───────────────────────────── new
```

If two optimizer branches independently produce byte-identical effective
programs, downstream solver and judge nodes may converge and reuse by exact work
identity. Reuse is based on content, not on an assumption that model names imply
equivalent outputs.

## Comparable benchmark identity

The existing `experiment_revision_id` remains the root hash of one complete
arm, including its solver, optimizer, judge, strategy, and resolved dynamic
outputs. It is too specific to group different models.

A new `benchmark_revision_id` identifies the frozen evaluation problem:

```text
hash(
    target and reference repository snapshots,
    global and skill instruction inputs,
    reviewed case solver/judge projections,
    case-bank coverage manifest,
    train/development/test/challenge splits,
    rubric and judge-calibration corpus,
    solver/judge information-view policy,
    scoring and aggregation implementation,
    benchmark-versus-pilot policy,
    runtime-visible evaluation contract,
    benchmark identity schema version
)
```

Solver and optimizer deployments are deliberately excluded. They are the
factors being compared. Endpoint topology, recorder configuration, timestamps,
run ids, and credentials are also excluded.

Judge scores are comparable only when the measurement instrument is fixed. A
`comparison_group_id` therefore adds:

```text
hash(
    benchmark_revision_id,
    judge profile and deployment identity,
    judge program and repetitions,
    validated judge-calibration result,
    comparison policy version
)
```

Changing the judge does not rewrite the benchmark corpus, but it creates a new
comparison group. Results from different groups remain visible globally and are
never silently averaged together.

An `arm_id` identifies the treatment:

```text
hash(
    comparison_group_id,
    solver profile,
    optimizer profile or null,
    optimization strategy and search configuration,
    solver repetitions,
    effective initial program and skill inputs
)
```

These grouping identities do not replace stage work ids. Work ids stay
stage-specific so an optimizer change cannot contaminate the baseline solver
key.

## Global MLflow mapping

The default MLflow Experiment becomes one stable benchmark family:

```yaml
recorders:
  local-mlflow:
    type: mlflow
    tracking_uri: http://192.168.1.17:5000
    experiment_name: ms-agent-eval/{workspace_id}
```

This refines ADR-006, which included `{experiment_id}` in the default name.
`experiment_id` becomes a run tag rather than an MLflow Experiment boundary.
Recorder configuration remains operational and still does not affect benchmark
or work identity.

Every execution run records at least:

```text
run_kind = arm_execution
workspace_id
experiment_id
experiment_revision_id
benchmark_revision_id
comparison_group_id
arm_id
solver.profile / deployment / revision
optimizer.profile / deployment / revision or none
judge.profile / deployment / revision
optimization.strategy
program.initial_hash
program.compiled_hash when available
benchmark_valid
```

MLflow parent/child nesting continues to represent stages inside one execution
run. Cross-invocation comparison grouping uses immutable tags and a comparison
manifest rather than requiring a long-lived active parent run.

After a requested arm completes, the recorder creates an immutable derived
comparison run from the latest completed report for each compatible arm:

```text
run_kind = model_comparison
comparison_id = copro-optimizer-models
comparison_group_id = ...
input_arm_run_ids = artifact, not a high-cardinality tag
```

The comparison manifest pins every local run id, experiment revision, arm id,
aggregate result id, run-graph id, and report hash used. A new input result
creates a new comparison-manifest hash and an auditable projection revision; it
does not mutate the scientific meaning of an earlier comparison.

### Global model charts

The comparison run publishes recorder-neutral tables plus interactive Plotly
HTML and static SVG companions for:

- baseline score by solver model;
- optimized score by solver and optimizer model;
- paired optimizer lift ordered by signed delta;
- model-by-skill score heatmap;
- performance by exact expected-response token band;
- quality-versus-latency and quality-versus-token Pareto views.

The companion MLflow tables retain sample counts and all model identities;
heatmaps preserve missing cells rather than imputing them. Comparison-run tags
carry the benchmark revision, comparison group, and manifest hash. Optimized
absolute score is attributed to the solver/program pair. Optimizer effectiveness
is reported as paired lift, not as the solver's raw score.

Runs with different `benchmark_revision_id` values may appear in the same
global MLflow Experiment for history, but they cannot enter the same comparison
manifest. The framework does not compare only the accidental intersection of
cases unless a future explicitly named common-case protocol defines that
analysis.

## Identity and cache consequences

Node keys include only the model roles that can affect that node:

| Node | Required model identity |
| --- | --- |
| Judge validation | Judge only |
| Baseline solve | Solver only |
| COPRO proposal | Optimizer only, plus its scored search history |
| Candidate solve | Solver plus effective candidate program |
| Candidate judgment | Judge plus producing solver/result identity |
| Final paired solve | Solver plus original or compiled program |
| Final judgment | Judge plus final solver/result identity |
| Arm aggregate | Exact upstream judgment results and arm policy |
| Cross-arm comparison | Exact arm aggregate results and comparison policy |

Consequently:

- changing only optimizer temperature does not invalidate baseline responses;
- changing only solver deployment does not invalidate judge calibration;
- changing only judge deployment reuses solver responses but rejudges them;
- changing endpoint capacity or adding an equivalent eligible replica does not
  invalidate semantic work;
- changing a deployment revision invalidates calls using that deployment; and
- changing cases or scoring protocol creates a new benchmark revision and the
  precise downstream invalidation required by ADR-003.

## Failure and provenance

Every model call records both semantic and physical routing identity:

- role and model-profile id;
- deployment id and revision;
- route model sent to the server;
- executor, endpoint id, and sanitized URL identity;
- configured capacity and observed queue/request timing;
- generation parameters and reasoning effort; and
- work, attempt, result, arm, comparison-group, benchmark-revision, and run ids.

A route mismatch is an integrity or configuration failure, not a generic
connection failure and not a reason to try an endpoint serving another
deployment. Already completed exact work remains reusable after the route is
fixed.

## Implemented migration

This decision uses workspace schema 14. There is no compatibility
shim that treats every endpoint as serving every model; that behavior is the
gap this ADR closes.

The implementation:

1. adds deployment and route configuration models and validates the complete
   profile-to-deployment-to-endpoint chain.
2. filters endpoint leasing to deployment-eligible routes while sharing the
   physical endpoint permit counter.
3. moves semantic model name/revision from role profiles into deployments and
   preserves role-specific generation parameters in model profiles.
4. moves solver, optimizer, and judge profile selection from global stages into
   experiment `roles`.
5. updates stage planning and work keys so each node includes only relevant role
   identities.
6. adds benchmark revision, comparison group, comparison, and arm identities to
   locks, packets, results, summaries, and recorder descriptors.
7. changes the MLflow default to one Experiment per workspace and projects all
   grouping identities as tags.
8. adds immutable comparison manifests, global model tables, and Plotly/SVG chart
   artifacts.
9. migrates templates and both example workspaces without legacy fields; and
10. covers routing, invalidation, MLflow grouping, and comparison projection in
    the regression suite.

## Acceptance criteria

- An endpoint receives requests only for deployments listed in one of its
  routes.
- Qwen and GLM endpoints may coexist in one executor without cross-routing.
- A multi-model endpoint shares one physical capacity limit across all served
  deployments.
- A required deployment with no eligible endpoint fails during validation or
  preflight before any generation request.
- Two model profiles with different temperatures can share one deployment while
  retaining different semantic work identities.
- Qwen-solver/Qwen-optimizer and Qwen-solver/GLM-optimizer arms share exact Qwen
  baseline work.
- Changing only the optimizer invalidates proposal-dependent descendants, not
  the baseline or fixed-judge calibration.
- Changing only the judge reuses solver responses and produces new judgments.
- All arms for one workspace appear in one MLflow Experiment.
- A comparison manifest rejects arms with different benchmark revisions or
  comparison groups.
- MLflow exposes baseline solver performance, optimized performance, paired
  optimizer lift, model-by-skill performance, token-band performance, and
  quality/efficiency charts.
- Historical benchmark revisions remain globally visible without being merged
  into current metrics.
- Endpoint and recorder configuration remain free of plaintext secrets.

## Alternatives considered

### Put one model name directly on an executor

Rejected. An executor is a scheduling and transport resource. One physical
endpoint may legitimately serve several deployments, and several role profiles
may share one deployment with different generation parameters.

### Put a model name on the endpoint without a deployment identity

Rejected. A mutable model alias does not identify weights, quantization,
tokenizer, template, or backend behavior and is insufficient for exact reuse.

### Continue treating every endpoint in a pool as equivalent

Rejected. It allows Qwen requests to reach a GLM-only server and makes mixed
model pools unsafe by construction.

### Duplicate the workspace for every model combination

Rejected. It creates configuration drift, obscures shared benchmark identity,
and makes correct partial graph reuse harder to reason about.

### Require solver and optimizer to be the same model

Rejected. The optimizer is a search controller and the solver is the target of
the search. Cross-model prompt and skill optimization is a valid and important
experimental condition.

### Create one MLflow Experiment per arm

Rejected. It prevents a natural global model comparison and duplicates the
benchmark-family hierarchy. Arm and revision identity belong in run tags and
comparison manifests.

### Average all runs sharing a workspace id

Rejected. Scores from changed cases, splits, rubrics, judges, or aggregation
policies are not automatically comparable.

## Consequences

Positive consequences:

- endpoint routing is safe for heterogeneous model-serving infrastructure;
- solver, optimizer, and judge choices become independently configurable;
- the Merkle graph reuses unaffected work across factorial model arms;
- MLflow provides one global history and defensible within-revision model
  comparisons; and
- charts distinguish solver capability from optimizer lift.

Costs and risks:

- the workspace schema and planning code become more explicit;
- deployment revisions remain an operator attestation where servers cannot
  expose verifiable metadata;
- factorial studies can become expensive, so only explicitly requested arms run;
- comparison projections need durable manifests and idempotent refresh; and
- existing MLflow projections remain in their historical experiment paths
  unless deliberately backfilled into the new global layout.

## Non-goals

This ADR does not implement automatic model discovery, autoscaling, speculative
execution, hidden retries, arbitrary comparison across changed case banks, or
automatic execution of every Cartesian model combination. It does not make
MLflow authoritative and does not weaken the canonical local artifact graph.
