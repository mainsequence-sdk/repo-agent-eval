# Model Arms and Cross-Model Comparisons

A model comparison is a set of named experiment arms in one workspace. Do not
copy `workspace.yaml` for Qwen, GLM, or another model: the repository snapshot,
case bank, splits, judge protocol, and MLflow history must remain one shared
benchmark family.

## Configuration layers

```text
endpoint route ── serves ──> deployment ── used by ──> model profile
                                                        │
                                                        ▼
                                                experiment role
```

- An executor describes transport and physical concurrency.
- A deployment describes one semantic served model and immutable revision.
- An endpoint route says that a deployment is actually available at that URL
  and gives the exact model string sent in the OpenAI-compatible request.
- A model profile adds role-specific reasoning, temperature, token, timeout,
  and cost settings.
- An experiment arm binds profiles to solver, optimizer, and judge roles.

One endpoint can expose several routes, but its physical
`max_concurrent_requests` is shared. A stage uses only endpoints that advertise
its deployment.

## Add Qwen and GLM to one workspace

```yaml
executors:
  lan-inference:
    type: endpoint
    adapter: openai_compatible
    endpoints:
      - id: qwen-a
        base_url: http://192.168.1.17:18081/v1
        max_concurrent_requests: 2
        routes:
          - {deployment: qwen-27b-local, model: qwen3.6:27b-coding}
      - id: glm-a
        base_url: http://192.168.1.17:18080/v1
        max_concurrent_requests: 2
        routes:
          - {deployment: glm-local, model: glm-coding}
      - id: qwen-b
        base_url: http://192.168.1.10:11434/v1
        max_concurrent_requests: 4
        routes:
          - {deployment: qwen-27b-local, model: qwen3.6:27b-coding}

deployments:
  qwen-27b-local:
    executor: lan-inference
    model: qwen3.6:27b-coding
    revision: qwen3.6-27b-coding-q4-k-m-2026-09-14
  glm-local:
    executor: lan-inference
    model: glm-coding
    revision: glm-coding-q4-k-m-2026-09-14

models:
  qwen-solver:
    deployment: qwen-27b-local
    reasoning_effort: none
    parameters: {temperature: 0.2, max_tokens: 4096, timeout: 600}
  qwen-optimizer:
    deployment: qwen-27b-local
    reasoning_effort: none
    parameters: {temperature: 0.7, max_tokens: 4096, timeout: 600}
  glm-solver:
    deployment: glm-local
    reasoning_effort: none
    parameters: {temperature: 0.2, max_tokens: 4096, timeout: 600}
  glm-optimizer:
    deployment: glm-local
    reasoning_effort: none
    parameters: {temperature: 0.7, max_tokens: 4096, timeout: 600}
  fixed-judge:
    deployment: qwen-27b-local
    reasoning_effort: none
    parameters: {temperature: 0.0, max_tokens: 2048, timeout: 300}
```

The two Qwen endpoints provide six usable Qwen slots. The GLM endpoint provides
two GLM slots. The executor's eight physical slots are never treated as eight
eligible slots for either model.

## Declare arms rather than duplicate workspaces

```yaml
experiments:
  qwen-baseline:
    mode: evaluate
    roles: {solver: qwen-solver, judge: fixed-judge}
    solver: # canonical DSPy solver signature
    runtime: {type: response_only, python: "3.12"}
    repetitions: 1

  glm-baseline:
    mode: evaluate
    roles: {solver: glm-solver, judge: fixed-judge}
    solver: # same canonical DSPy solver signature
    runtime: {type: response_only, python: "3.12"}
    repetitions: 1

  qwen-copro-qwen-optimizer:
    mode: optimize
    based_on: qwen-baseline
    roles:
      solver: qwen-solver
      optimizer: qwen-optimizer
      judge: fixed-judge
    optimizer: # COPRO configuration
    validation: {strategy: holdout, fraction: 0.2, seed: 0}
    budget: # role budgets

  qwen-copro-glm-optimizer:
    mode: optimize
    based_on: qwen-baseline
    roles:
      solver: qwen-solver
      optimizer: glm-optimizer
      judge: fixed-judge
    optimizer: # identical COPRO search protocol
    validation: {strategy: holdout, fraction: 0.2, seed: 0}
    budget: # role budgets

comparisons:
  model-arms:
    baseline_experiments: [qwen-baseline, glm-baseline]
    optimization_experiments:
      - qwen-copro-qwen-optimizer
      - qwen-copro-glm-optimizer
    primary_metric: score.mean
    grouping: [solver, optimizer]
```

An optimization arm must keep the same solver and judge profiles as its
`based_on` baseline. The optimizer may be different. This isolates the question
“which model proposes better instructions?” from “which model solves the case?”
The named comparison must include that `based_on` experiment in
`baseline_experiments`, all listed arms must use one fixed judge profile, and
schema 15 currently supports `score.mean` as its primary metric. Grouping is a
unique subset of `solver` and `optimizer`.

## Execute arms

```bash
uv run ms-agent-eval run qwen-baseline --workspace workspace.yaml
uv run ms-agent-eval run glm-baseline --workspace workspace.yaml
uv run ms-agent-eval run qwen-copro-qwen-optimizer --workspace workspace.yaml
uv run ms-agent-eval run qwen-copro-glm-optimizer --workspace workspace.yaml
```

Each command builds a new run, but not necessarily new model work. Exact stage
keys reuse unchanged results:

- switching only the optimizer reuses the Qwen baseline solves and fixed-judge
  calibration;
- switching only the solver reuses fixed-judge calibration but creates new
  responses and downstream judgments;
- switching only the judge reuses solver responses and re-runs calibration and
  judgment; and
- changing endpoint capacity or adding an equivalent route changes execution
  provenance, not semantic results.

`inspect` prints `benchmark_revision_id`, `comparison_group_id`, `comparison_id`,
and `arm_id` before execution:

```bash
uv run ms-agent-eval inspect --workspace workspace.yaml
```

## MLflow layout and assets

Use one MLflow Experiment per workspace:

```yaml
recorders:
  shared-mlflow:
    type: mlflow
    tracking_uri: http://192.168.1.17:5000
    experiment_name: ms-agent-eval/{workspace_id}
    authentication: {type: none}
```

Every normal run is tagged `agent_eval.run_kind=arm_execution`. After each valid
completed arm, the recorder automatically writes a separate immutable
`model_comparison` run for the latest completed report of every compatible
`arm_id`. Its manifest pins the exact local run, experiment revision, aggregate
result, run graph, arm, and report hashes.

Comparison tables:

- `model-arm-comparison`
- `model-skill-comparison`
- `model-token-band-comparison`

Interactive Plotly HTML and static SVG companions are stored under
`comparison/graphs/` for baseline score, optimized score, paired optimizer lift,
model-by-skill performance, token-band performance, quality versus latency, and
quality versus tokens. Runs with different benchmark revisions or fixed judges
remain visible in the global history but are never merged into one comparison.

See [ADR-009](../architecture/009-factorial-model-arms-and-deployment-aware-tracking.md)
for identity and invalidation rationale.
