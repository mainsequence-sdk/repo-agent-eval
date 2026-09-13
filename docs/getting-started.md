# Getting Started

This is the shortest complete workflow: configure a repository and its exact
skills, ask Codex to author grounded cases, solve them with one LLM, and score
them with a separate LLM judge.

## 1. Install Python 3.12+

```bash
uv sync --python 3.12
```

DSPy is installed with the base library.

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
  --solver-model solver-model \
  --solver-reasoning-effort high \
  --judge-model judge-model \
  --judge-reasoning-effort none
```

Use repeated `--skill-file path/to/SKILL.md` arguments instead of
`--skills-directory` for an exact list. The forms are mutually exclusive.

The command creates a schema-v8 `workspace.yaml`, `case-authoring.md`, a local
`cases/README.md`, a draft `cases/coverage.yaml` capability catalog, an empty
case bank, grouped split manifest, and judge-calibration manifest. It does not
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
capabilities in `cases/coverage.yaml`, then writes schema-v5 packages directly
under `cases/<skill>/<case>/`. A requested number is a lower bound. The campaign
must create at least three distinct cases per selected skill and expand until
every catalog capability has its configured number of primary cases. Before it
returns, the same authoring campaign also updates grouped splits and creates the
complete `judge-calibration/` corpus. Calibration is not a manual phase left to
the user after case creation.

Read the [capability coverage contract](capability-coverage.md) before marking
the catalog complete. It defines the catalog schema, per-case classification,
readiness calculation, diversity requirements, and revalidation rules.

For diagnostic cases containing a real faulty test, false premise, broken
configuration, contradiction, or misleading symptom, the rubric includes
`error_identification` at 35–50% of the normalized score. Ordinary cases do not
receive artificial diagnostic points.

To execute a model-driven stage automatically through a subscription-backed
Codex CLI container, provision and configure the separate
[Codex CLI harness](codex-cli-harness.md). That adapter accepts no OpenAI API
key and never falls back to API-billed inference.

## 4. Configure stage executors and models

Model names, OpenAI-compatible API locations, and stage bindings are durable
workspace configuration. Solver and judge remain separate roles even when they
use the same configured model:

```yaml
executors:
  remote-models:
    type: endpoint
    adapter: openai_compatible
    base_url: http://127.0.0.1:8000/v1
models:
  shared-runtime:
    name: qwen3.6:27b-coding
    reasoning_effort: medium
stages:
  solve: {executor: remote-models, model: shared-runtime, budget: {model_calls: 1, tokens: 32000, wall_seconds: 600}}
  judge: {executor: remote-models, model: shared-runtime, budget: {model_calls: 3, tokens: 64000, wall_seconds: 600}}
```

If the API requires a credential, set `api_key_env: OPENAI_API_KEY` on the
endpoint executor and put only the secret in the ignored local `.env` file:

```dotenv
OPENAI_API_KEY=your-secret
```

Model selection and API URLs belong in `workspace.yaml`, not environment
variables.

`reasoning_effort` is an optional first-class model setting. Choose it in the
setup wizard or set it to `none`, `minimal`, `low`, `medium`, `high`, `xhigh`,
`max`, or `ultra` when supported by that model and executor. Omit it to use the
provider/model default. Do not place it inside `parameters`.

## 5. Review the authored judge calibration

As part of case authoring, Codex must populate
`judge-calibration/manifest.yaml` and its candidate files with controlled,
reference-labelled responses covering `strong`, `partial`, `incorrect`,
`contradictory`, and `adversarial`. Every fixture references an existing
promoted train/development case and declares its accepted score range. Review
the fixtures with the cases and rubrics; do not replace them with real solver
outputs or judge feedback. The LLM judge must pass calibration before any solver
request.

```yaml
schema_version: 2
fixtures:
  - id: strong-example
    case: case-id
    candidate: strong.md
    label: strong
    score_range: [0.9, 1.0]
```

## 6. Validate and inspect

```bash
uv run --project /path/to/ms-agent-eval ms-agent-eval validate \
  --workspace workspace.yaml

uv run --project /path/to/ms-agent-eval ms-agent-eval inspect \
  --workspace workspace.yaml
```

Inspection reports source commits/snapshots, exact skills, authoring policy and
source-set hashes, case/split coverage, solver/judge identities, calibration,
runtime, projected calls, and experiment lock hashes. An incomplete result
lists the remaining readiness blockers without making model calls.

To exercise an incomplete bank without weakening scored-run validation, run a
diagnostic pilot:

```bash
uv run --project /path/to/ms-agent-eval ms-agent-eval run baseline --pilot \
  --workspace workspace.yaml
```

The pilot executes judge calibration and the train/development solver and judge
packets. It excludes test/challenge cases, rejects optimization experiments,
uses one diagnostic judge vote per calibration fixture and case, stores
per-case diagnostics under the external data root, records `benchmark_valid:
false`, and emits no aggregate benchmark metrics. Use `--pilot` again when
resuming or operating a pilot's stage packets.

## 7. Evaluate, then optimize separately

```bash
uv run --project /path/to/ms-agent-eval ms-agent-eval run baseline \
  --workspace workspace.yaml

uv run --project /path/to/ms-agent-eval ms-agent-eval run optimize-few-shot \
  --workspace workspace.yaml
```

The baseline sends each case only to the solver, then sends the candidate plus
the hidden rubric/reference material to the judge. Optimization sees train and
development cases only, publishes state-only DSPy JSON, and only then loads the
untouched test set for final evaluation. All generated outputs stay under the
external data root.
