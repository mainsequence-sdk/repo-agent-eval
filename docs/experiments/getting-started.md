# Getting Started

This is the shortest complete workflow: configure a repository and its exact
skills, ask Codex to author grounded cases, solve them with one LLM, and score
them with a separate LLM judge.

## 1. Install Python 3.12+

```bash
uv sync --python 3.12 --extra mlflow
```

DSPy is installed with the base library. The optional MLflow extra supports the
default experiment recorder; pass `--no-recording` to `init` if that projection
is not wanted.

## 2. Create a workspace

The guided setup resolves the repository first, then lets you select global
instructions and either a complete skills directory or an exact list of
`SKILL.md` files. It configures author, solver, and judge stage assignments.

```bash
uv run --project /path/to/ms-agent-eval ms-agent-eval setup
```

For automation:

```bash
mkdir my-evaluation
cd my-evaluation

uv run --project /path/to/ms-agent-eval ms-agent-eval init \
  --id example-evaluation \
  --repo https://github.com/example/project \
  --ref v1.2.3 \
  --global-instructions AGENTS.md \
  --skills-directory .agents/skills \
  --cases cases \
  --api-base-url http://127.0.0.1:8000/v1 \
  --api-max-concurrent-requests 4 \
  --solver-model solver-model \
  --solver-reasoning-effort high \
  --judge-model judge-model \
  --judge-reasoning-effort none \
  --mlflow-tracking-uri http://127.0.0.1:5000
```

Use repeated `--skill-file path/to/SKILL.md` arguments instead of
`--skills-directory` for an exact list. The forms are mutually exclusive.

The command creates a schema-v14 `workspace.yaml`, `case-authoring.md`, a local
`cases/README.md`, a draft `cases/coverage.yaml` capability catalog, an empty
case bank, grouped split manifest, and judge-validation manifest. It does not
copy the target repository into the workspace. When `workspace.data_root` is
absent, generated data defaults to `~/ms_agent_eval/<workspace-id>`.

## 3. Configure the authoring evidence

The workspace points to the durable policy Codex must follow:

```yaml
evaluation:
  case_authoring:
    instructions: case-authoring.md
    sources:
      - id: reference-project
        role: project
        repository:
          url: https://github.com/example/reference-project
          ref: v2.0.0
        include:
          - README.md
          - docs/**
          - src/**
          - tests/**
```

Every target and reference URL/ref resolves to an immutable external snapshot.
Reference paths are exposed as
`references/<source-id>/<repository-path>`. Their combined source-set hash and
the exact authoring-policy and capability-catalog hashes become case provenance.

The `author` stage is the case-building model. For the MainSequence experiment
it is GPT-5.6 Sol through the artifact-exchange harness. Ask it to use the repository's
`case-authoring` skill, specify the
workspace, and state the campaign objective and number of cases per skill.
Codex first decomposes each complete selected skill into independently testable
capabilities in `cases/coverage.yaml`, then writes schema-v6 packages directly
under `cases/<skill>/<case>/`. A requested number is a lower bound. The campaign
must create at least three distinct cases per selected skill and expand until
every catalog capability has its configured number of primary cases. Before it
returns, the same authoring campaign also updates grouped splits and creates the
complete `judge-validation/` set. Judge validation is not a manual phase left to
the user after case creation.

Read the [capability coverage contract](../evaluation/capability-coverage.md) before marking
the catalog complete. It defines the catalog schema, per-case classification,
readiness calculation, diversity requirements, and revalidation rules.

For diagnostic cases containing a real faulty test, false premise, broken
configuration, contradiction, or misleading symptom, the rubric includes
`error_identification` at 35–50% of the normalized score. Ordinary cases do not
receive artificial diagnostic points.

To execute a model-driven stage automatically through a subscription-backed
Codex CLI container, provision and configure the separate
[Codex CLI harness](../execution/codex-cli.md). That adapter accepts no OpenAI API
key and never falls back to API-billed inference.

## 4. Configure deployments, role profiles, and experiment arms

API locations, semantic deployments, and role profiles are separate durable
configuration. Optimizer, solver, and judge are selected by each experiment arm
even when they share one deployment:

```yaml
executors:
  inference-pool:
    type: endpoint
    adapter: openai_compatible
    endpoints:
      - id: primary
        base_url: http://127.0.0.1:8000/v1
        max_concurrent_requests: 4
        routes:
          - {deployment: qwen-local, model: qwen3.6:27b-coding}
deployments:
  qwen-local:
    executor: inference-pool
    model: qwen3.6:27b-coding
    revision: qwen3.6-27b-coding-local-v1
models:
  optimizer:
    deployment: qwen-local
    reasoning_effort: none
    parameters: {temperature: 0.7, max_tokens: 4096}
  shared-runtime:
    deployment: qwen-local
    reasoning_effort: medium
experiments:
  baseline:
    mode: evaluate
    roles: {solver: shared-runtime, judge: shared-runtime}
    # solver/runtime/repetitions omitted
  optimize-solver:
    mode: optimize
    based_on: baseline
    roles:
      solver: shared-runtime
      optimizer: optimizer
      judge: shared-runtime
    # optimizer/cross_validation/budget omitted
```

If the API requires a credential, set `api_key_env: OPENAI_API_KEY` on the
specific endpoint item and put only the secret in the ignored local `.env` file:

```dotenv
OPENAI_API_KEY=your-secret
```

Model selection and API URLs belong in `workspace.yaml`, not environment
variables.

`reasoning_effort` is an optional first-class model setting. Choose it in the
setup wizard or set it to `none`, `minimal`, `low`, `medium`, `high`, `xhigh`,
`max`, or `ultra` when supported by that model and executor. Omit it to use the
provider/model default. Do not place it inside `parameters`.

## 5. Configure and verify experiment recording

`init` enables MLflow recording at `http://127.0.0.1:5000` by default. Keep its
database and artifact directory outside Git, start the server, then run the
write/read probe:

```bash
uv run --extra mlflow ms-agent-eval recorder doctor \
  --workspace workspace.yaml
```

The experiment still runs when MLflow is unavailable; recorder state remains
pending for a later `recorder sync`. Read
[Experiment recording](../execution/experiment-recording.md) for server setup,
privacy policies, authentication, status, and backfill.

## 6. Review the authored judge validation

As part of case authoring, Codex must populate
`judge-validation/manifest.yaml` and its candidate files with controlled,
reference-labelled responses covering `strong`, `partial`, `incorrect`,
`contradictory`, and `adversarial`. Every fixture references an existing
reviewed training case. Review
the fixtures with the cases and rubrics; do not replace them with real solver
outputs or judge feedback. The framework checks that the judge preserves the
expected ordinal quality ordering before any solver request.

```yaml
schema_version: 3
fixtures:
  - id: strong-example
    case: case-id
    candidate: strong.md
    label: strong
```

## 7. Validate and inspect

```bash
uv run --project /path/to/ms-agent-eval ms-agent-eval validate \
  --workspace workspace.yaml

uv run --project /path/to/ms-agent-eval ms-agent-eval inspect \
  --workspace workspace.yaml
```

Inspection reports source commits/snapshots, exact skills, authoring policy and
source-set hashes, case/split coverage, solver/judge identities, judge validation,
runtime, projected calls, and experiment lock hashes. An incomplete result
lists the remaining readiness blockers without making model calls.

To exercise an incomplete bank without weakening scored-run validation, run a
diagnostic pilot:

```bash
uv run --project /path/to/ms-agent-eval ms-agent-eval run baseline --pilot \
  --workspace workspace.yaml
```

An interactive terminal shows each case as it is solved and judged, including
case identity, progress, elapsed time, and continuous score. Add `--json` for one
machine-readable result document; redirected output uses JSON automatically.

The pilot executes judge validation and the train solver and judge
packets. It excludes test/challenge cases, rejects optimization experiments,
uses one diagnostic judge vote per judge-validation fixture and case, stores
per-case diagnostics under the external data root, records `benchmark_valid:
false`, and emits no aggregate benchmark metrics. Use `--pilot` again when
resuming or operating a pilot's stage packets.

## 8. Evaluate, then optimize separately

```bash
uv run --project /path/to/ms-agent-eval ms-agent-eval run baseline \
  --workspace workspace.yaml

uv run --project /path/to/ms-agent-eval ms-agent-eval run optimize-solver \
  --workspace workspace.yaml
```

The baseline sends each case only to the solver, then sends the candidate plus
the hidden rubric/reference material to the judge. `optimize-solver` is a named
experiment, not a fixed algorithm. It must select exactly one strategy:

```yaml
# Demonstration search: fixed prompt and skills, no optimizer-model calls.
optimizer:
  strategy: labeled_few_shot
  search: {k: [1, 2, 4]}
```

or:

```yaml
# Prompt/skill search: zero demonstrations and explicit optimizer proposals.
optimizer:
  strategy: copro
  targets: {prompt: true, skills: all}
  search: {breadth: 3, depth: 2, init_temperature: 1.4}
  metric: {name: rubric_mean_score}
```

Both strategies use grouped K-fold cross-validation only within train, compile
the selected configuration on all training cases, and then evaluate the
untouched test set. COPRO evaluates both frozen original and optimized programs
on identical test cases and creates explicit, reusable optimizer, candidate
fit, solve, judge, and selection packets. All generated outputs stay under the
external data root.
