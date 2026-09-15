# Workspace Configuration

`workspace.yaml` is the only framework configuration file in an experiment
workspace. Schema version 17 separates transport executors, semantic model
deployments, role profiles, experiment arms, comparisons, recorder projection,
recorder queue execution, and factorized COPRO search spaces.

## Top-level structure

| Key | Owns |
| --- | --- |
| `schema_version` | Exact workspace schema; legacy schemas are rejected. |
| `workspace` | Workspace id and optional external `data_root`. |
| `recording` | Selected recorder and queue plus projection, failure, artifact, and content policies. |
| `recorders` | External experiment-tracking destinations and credential references. |
| `recorder_queues` | Durable projection-intent queues, leases, retries, and coalescing. |
| `executors` | Endpoint, harness, or local-process transport configuration and endpoint routes. |
| `deployments` | Semantic model identity, immutable revision, and serving executor. |
| `models` | Role-specific profile referencing a deployment, with reasoning effort, parameters, and configured cost. |
| `stages` | Portable stage reuse policy and call/token/wall-time budgets; only non-arm stages bind executors globally. |
| `evaluation` | Repository, instructions, authoring sources, cases, splits, and judge contract. |
| `experiments` | Named baseline and optimization arms with explicit solver/optimizer/judge role bindings. |
| `comparisons` | Explicit baseline/optimization arm sets for compatible cross-model reports. |

```yaml
schema_version: 17

workspace:
  id: example-evaluation

recording:
  enabled: true
  recorder: local-mlflow
  queue: local-recorder-queue
  failure_policy: continue
  wait_on_run_exit: false
  artifact_policy: full
  content_policy: full
  projection:
    live: {enabled: true, minimum_interval_seconds: 15}
    stage_boundary: {tables: incremental, summaries: true}
    finalization: {reports: true, plotly: true, evidence: true}
    traces:
      optimization: {policy: winners_failures_and_sample, sample_rate: 0.05}
      validation: {policy: full}
      test: {policy: full}

recorders:
  local-mlflow:
    type: mlflow
    tracking_uri: http://127.0.0.1:5000
    experiment_name: ms-agent-eval/{workspace_id}
    authentication: {type: none}

recorder_queues:
  local-recorder-queue:
    type: filesystem
    path: operations/recorder-queue
    live_coalescing: true
    lease_seconds: 300
    max_attempts: 12

executors: {}
deployments: {}
models: {}
stages: {}

evaluation:
  repository: {}
  instructions: {}
  case_authoring: {}
  cases: {}
  splits: {}
  judge: {}

experiments: {}
comparisons: {}
```

## Important invariants

- Repository tags resolve to full commits before model execution.
- Every repository and imported skill selection uses exactly one of
  `skills.directory` or `skills.files`.
- API addresses and model ids are versioned here; only secret values come from
  environment variables.
- Every endpoint has a positive `max_concurrent_requests` value and at least
  one explicit `routes[]` entry naming the deployment it can serve and the
  exact model string sent to that endpoint.
- `reasoning_effort` is a first-class model field, not a generic parameter.
- Every model profile references one deployment. Every deployment requires an
  immutable `revision`; change it whenever weights,
  quantization, tokenizer, chat template, or hidden serving state changes.
- Every stage requires `reuse.policy`: `exact`, `refresh`, or solver-only
  `replicate` with `samples`.
- Solver, optimizer, and judge are selected inside each experiment arm even
  when deliberately assigned profiles backed by the same deployment.
- Every stage limits model calls and wall-clock time. Token usage is measured
  and reported, not used as a framework execution ceiling; endpoint context
  preflight is the request-fit boundary.
- An optimization experiment names its baseline with `based_on`, selects
  exactly one `optimizer.strategy`, declares that strategy's search space, and
  configures one fixed validation holdout. COPRO carries state across its
  configured search rounds over the complete optimization-training set.
- Generated ids, hashes, snapshots, and locks are outputs, not authored
  configuration.
- Recorder destinations are operational projections. Their configuration hash
  is intentionally excluded from experiment revisions and reusable work ids.

Use [Target sources](target-sources.md) for `evaluation.repository` and
authoring references, [Case authoring](case-authoring.md) for the versioned
corpus, [Experiment recording](../execution/experiment-recording.md) for MLflow,
and [Execution](../execution/index.md) for executor configuration.

## Recording and recorder destinations

`recording` selects one destination and queue and controls what the automatically
managed run consumer projects. `recorders` defines reusable destinations
independently from solver/judge endpoint executors. `recorder_queues` controls
durable scheduling and recovery:

```yaml
recording:
  enabled: true
  recorder: shared-mlflow
  queue: local-recorder-queue
  failure_policy: continue
  wait_on_run_exit: false
  artifact_policy: full         # or index_only
  content_policy: summary       # hashes_only, summary, or full
  projection:
    live: {enabled: true, minimum_interval_seconds: 15}
    stage_boundary: {tables: incremental, summaries: true}
    finalization: {reports: true, plotly: true, evidence: true}
    traces:
      optimization: {policy: winners_failures_and_sample, sample_rate: 0.05}
      validation: {policy: full}
      test: {policy: full}

recorders:
  shared-mlflow:
    type: mlflow
    tracking_uri: https://mlflow.example.internal
    experiment_name: agent-eval/{workspace_id}
    authentication:
      type: bearer
      token_env: MLFLOW_TRACKING_TOKEN

recorder_queues:
  local-recorder-queue:
    type: filesystem
    path: operations/recorder-queue
    live_coalescing: true
    lease_seconds: 300
    max_attempts: 12
```

The experiment-name template accepts `{workspace_id}` and `{experiment_id}`, but
the standard layout deliberately uses only `{workspace_id}` so every arm is
visible in one MLflow Experiment. URLs, policies, and secret environment-variable names are
versioned configuration; credential values are not. The experiment process
automatically manages a run-scoped consumer; the semantic coordinator itself
only appends pointer-only intents. `wait_on_run_exit: false` drains immediately
eligible work but does not wait through an unavailable destination's retry
backoff. Recorder failure remains visible and retryable but does not change the
semantic outcome of a run.

## Endpoint executor pools

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
          - {deployment: qwen-27b-local, model: qwen3.6:27b-coding}
      - id: llama-cpp-17-18080
        base_url: http://192.168.1.17:18080/v1
        max_concurrent_requests: 2
        routes:
          - {deployment: glm-local, model: glm-coding}
      - id: llama-server-10-11434
        base_url: http://192.168.1.10:11434/v1
        max_concurrent_requests: 4
        routes:
          - {deployment: qwen-27b-local, model: qwen3.6:27b-coding}

deployments:
  qwen-27b-local:
    executor: lan-inference
    model: qwen3.6:27b-coding
    revision: qwen3.6-27b-coding-local-2026-09-14
  glm-local:
    executor: lan-inference
    model: glm-coding
    revision: glm-coding-local-2026-09-14
```

The executor id names transport, not a model or a vague location such as
"remote." Endpoint ids and normalized URLs must be unique inside the pool. A
credential reference, when required, belongs on the endpoint as `api_key_env`.
Each route is an explicit eligibility edge from a deployment to that endpoint.
The exact request model may differ between equivalent replicas, but the
deployment revision is their shared semantic attestation.

Capacity applies to independent model requests, including requests nested inside
a single stage packet. The usable capacity is calculated per deployment. In the
example Qwen has six slots and GLM has two; an arm never sends Qwen to the
GLM-only endpoint. If one physical endpoint exposes several routes, all models
share its single `max_concurrent_requests` permit pool.

Pool topology and capacities are recorded as execution provenance but do not
change semantic work identity. `deployments.<id>.revision` does: it must change
for any output-affecting deployment change.

## Imported instruction bundles

An evaluated repository can assemble instructions from its own pinned source
and an independently versioned external catalog. Store the reviewed external
instruction files in the experiment and declare the import explicitly:

```yaml
evaluation:
  instructions:
    global:
      - agent_scaffold/AGENTS.md
    skills:
      directory: agent_scaffold/skills
    imports:
      - id: platform
        source: mcp
        note: >-
          Platform-owned skills captured from MCP and pinned by content hash.
        manifest: instructions/mainsequence-platform/manifest.yaml
        skills:
          directory: instructions/mainsequence-platform/skills
```

Imported skill ids are namespaced as `<import-id>/<manifest-unit-id>`. The
manifest must list exactly the selected files and record a SHA-256 content id
for each one. Validation rejects missing, extra, changed, symlinked, colliding,
or unmanifested files. Source metadata in the manifest participates in the
instruction-context and experiment revision identities.

Imports are never refreshed as a side effect of `validate`, `inspect`, or
`run`. Updating one requires an explicit authenticated capture and review. This
keeps an external mutable catalog from changing a benchmark silently.

## Model profiles, experiment roles, and reuse

```yaml
models:
  qwen-solver:
    deployment: qwen-27b-local
    reasoning_effort: high
    parameters:
      temperature: 0.2
      max_tokens: 4096

experiments:
  qwen-baseline:
    mode: evaluate
    roles: {solver: qwen-solver, judge: fixed-judge}
    # solver/runtime/repetitions omitted here
```

The deployment revision is an operator-controlled cache boundary, not a secret
and not a marketing model alias. Profile parameters remain separate because
temperature, reasoning effort, and token limits are role-specific and affect
work identity. `exact` is the normal evaluation policy.
Temporarily select `refresh` when a new attempt is required despite identical
inputs. For independent solver samples use:

```yaml
stages:
  solve:
    reuse:
      policy: replicate
      samples: 3
    budget: {model_calls: 1, wall_seconds: 600}
```

Replicates share one semantic `work_id` but receive separate attempt identities
and are never collapsed into a cache hit. Judge consensus repetitions remain a
different setting under `evaluation.judge.repetitions`. The default is one judge
vote. A value greater than one is opt-in because every additional repetition is
another model request for every candidate response, including optimization-search
responses.

## Optimization strategy

The experiment id is user-defined; `optimizer.strategy` determines what the
compiled program is allowed to change. The two accepted shapes are
intentionally incompatible.

Few-shot fitting searches demonstration count while keeping the solver prompt,
output prefix, and source skills fixed. It does not call an optimizer model:

```yaml
experiments:
  optimize-solver:
    mode: optimize
    based_on: baseline
    roles: {solver: solver, judge: judge}
    optimizer:
      strategy: labeled_few_shot
      search: {k: [1, 2, 4]}
    validation: {strategy: holdout, fraction: 0.2, seed: 0}
    budget:
      solver: {model_calls: 1000}
      judge: {model_calls: 3000}
      wall_seconds: 7200
```

COPRO keeps the compiled program zero-shot and searches exactly one instruction
layer. It requires a separate optimizer role and budget even when all roles
point to the same model. To freeze global instructions such as `AGENTS.md` and
optimize the selected skills:

```yaml
models:
  optimizer:
    deployment: qwen-27b-local
    reasoning_effort: none
    parameters: {temperature: 0.7, max_tokens: 4096}

experiments:
  optimize-solver:
    mode: optimize
    based_on: baseline
    roles:
      solver: solver
      optimizer: optimizer
      judge: judge
    optimizer:
      strategy: copro
      search_space:
        type: skills
        include: all
      search: {breadth: 3, depth: 2, init_temperature: 1.4}
      metric: {name: rubric_mean_score}
    validation: {strategy: holdout, fraction: 0.2, seed: 0}
    budget:
      optimizer: {model_calls: 100}
      solver: {model_calls: 5000}
      judge: {model_calls: 15000}
      wall_seconds: 14400
```

`search_space.include` is either `all` or an exact non-empty list of configured
skill ids; glob patterns are rejected. Global instructions, unselected skills,
and the DSPy signature remain byte-identical throughout this search.

To freeze every skill and optimize the configured global instruction bundle:

```yaml
optimizer:
  strategy: copro
  search_space:
    type: global_instructions
    files:
      - agent_scaffold/AGENTS.md
  search: {breadth: 3, depth: 2, init_temperature: 1.4}
  metric: {name: rubric_mean_score}
```

`search_space.files` is either `all` or an exact non-empty list of paths already
declared by `evaluation.instructions.global`. The selected files form one
ordered optimization target; every skill and the DSPy signature remain frozen.
There is no joint mode and no implicit default. The optimizer receives candidate
text and numeric score history, but never rubrics, expected answers, judge
feedback, test cases, or challenge cases. Preflight rejects unknown selectors,
insufficient grouped training/validation coverage, and model-call budgets below
the bounded search.

The strategy participates in the experiment revision and downstream work
identities. Changing it creates a new graph; it cannot reinterpret or resume an
older strategy in place.

## Cross-model arms and comparisons

Add a new experiment arm when changing the solver, optimizer, or judge. Do not
copy the whole workspace. Arms sharing the exact cases, split, runtime, scoring
protocol, and judge receive the same `comparison_group_id`; stage work is reused
where its input hashes and role model are unchanged.

```yaml
experiments:
  qwen-baseline:
    mode: evaluate
    roles: {solver: qwen-solver, judge: fixed-judge}
    # solver/runtime/repetitions omitted
  qwen-copro-with-glm-optimizer:
    mode: optimize
    based_on: qwen-baseline
    roles:
      solver: qwen-solver
      optimizer: glm-optimizer
      judge: fixed-judge
    # optimizer/validation/budget omitted

comparisons:
  solver-and-copro-models:
    baseline_experiments: [qwen-baseline]
    optimization_experiments: [qwen-copro-with-glm-optimizer]
    primary_metric: score.mean
    grouping: [solver, optimizer]
```

One experiment may belong to one named comparison in schema 17. A completed
valid arm automatically refreshes an immutable `model_comparison` projection in
the workspace-level MLflow Experiment. The manifest selects the latest completed
report for each `arm_id` and refuses to mix benchmark revisions, comparison
groups, or judge identities.

Schema 17 supports `score.mean` as the primary comparison metric. Grouping is a
unique subset of `solver` and `optimizer`; every optimization arm must name one
of the comparison's listed baselines through `based_on`; and every listed arm
must use the same judge profile.
