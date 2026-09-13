# MS Agent Eval

MS Agent Eval is a Python 3.12+ DSPy evaluation framework for arbitrary source
repositories. A repository URL/ref and its instruction paths are configuration;
the evaluated project is never a dependency of this library.

The framework has no built-in target repository. Each experiment workspace
independently selects its repository, immutable revision, instruction sources,
cases, rubrics, and models.

## Technology at a glance

| Technology | How it is used |
|---|---|
| Python 3.12+ and Rich | Framework implementation and a live `ms-agent-eval` terminal interface with explicit JSON automation output. |
| Portable stages | Authoring, judge validation, fitting, solving, judging, model selection, and aggregation use one immutable packet/result lifecycle. |
| Codex skills / CLI | The configured author model creates grounded cases; the isolated CLI adapter can execute model-driven stages with subscription authentication in disposable containers. |
| DSPy 3.2 | The runtime prompt-programming layer. Typed DSPy signatures define the solver and judge; `LabeledFewShot` compiles optimized solver state. |
| OpenAI-compatible APIs | Endpoint transport for model-driven DSPy stages. Solver and judge remain separate roles but may use the same model. |
| YAML / PyYAML | Human-readable workspace configuration, case metadata, rubrics, split assignments, and judge-validation manifests. |
| Git | Resolves the target and optional reference repositories from tags or commits into immutable snapshots with recorded provenance. |
| Docker | Digest-pinned isolation for automatic Codex CLI stages, with stage-specific read-only views and a restricted control-plane proxy. |
| SHA-256 + JSON artifacts | Identifies snapshots, programs, inputs, calls, and results so runs are reproducible and auditable. |
| External filesystem storage | Keeps snapshots, model-call evidence, compiled DSPy programs, runs, and reports under `~/ms_agent_eval/<workspace-id>` instead of committing them. |
| `uv`, Hatchling, pytest, Ruff | Dependency/environment management, package builds, tests, and linting. |

The target repository is data, not a Python dependency of the framework. The
framework snapshots it, loads only the configured instructions and cases, asks
the runtime LLMs to solve and judge, and writes all mutable evidence outside
both repositories. Codex authors cases directly before runtime evaluation.

## How an experiment runs

Every scored workflow separates authoring, solver, and judge roles through
portable stages. The roles may share a model only when the workspace explicitly
configures that choice:

```mermaid
flowchart TB
    subgraph Definition["Versioned experiment definition"]
        Workspace["workspace.yaml"]
        AuthoringPolicy["case-authoring.md"]
        CapabilityCatalog["cases/coverage.yaml"]
        Cases["Reviewed cases + rubrics"]
        Splits["Grouped train / test / challenge splits"]
        Workspace --> AuthoringPolicy
        Workspace --> CapabilityCatalog
        Workspace --> Cases
        Cases --> Splits
    end

    subgraph Authoring["Case authoring"]
        Repo["Target + reference repositories"] --> Snapshot["Immutable external snapshots"]
        Workspace --> Snapshot
        Snapshot --> SourceSet["Content-addressed authoring source set"]
        SourceSet --> Author["Configured author stage + case-authoring skill"]
        AuthoringPolicy --> Author
        CapabilityCatalog --> Author
        Author --> Cases
    end

    subgraph Baseline["Baseline evaluation"]
        Cases --> JudgeValidation["1. Validate judge ordering"]
        JudgeValidation --> Solver["2. DSPy solver LLM"]
        Solver --> Candidate["Candidate response"]
        Candidate --> Judge["3. DSPy LLM judge"]
        Cases -->|"rubric + expected result"| Judge
        Judge --> BaselineResults["External scores + call evidence"]
    end

    subgraph Optimization["DSPy optimization experiment"]
        Splits --> Train["Train cases"]
        Train --> Folds["Grouped K-fold cross-validation"]
        Folds --> Candidates["Fit and score baseline + LabeledFewShot candidates"]
        Candidates --> Select["Select highest mean out-of-fold score"]
        Select --> Refit["Refit selected configuration on all train cases"]
        Refit --> Test["Load untouched test cases"]
        Splits --> Test
        Test --> Final["Optimized solver + LLM judge"]
        Final --> OptimizedResults["External held-out results"]
    end
```

- Codex first decomposes every complete selected skill into the versioned
  `cases/coverage.yaml` capability catalog, then combines the versioned
  `case-authoring.md` policy with locked target and
  reference-repository snapshots. It writes reviewed case packages directly to
  the case bank, with source/policy/catalog/content hashes. Each case has one
  primary capability; readiness requires every catalog capability to reach its
  configured primary-case depth. This step requires no model
  endpoint. Diagnostic cases give substantial rubric weight to correctly
  identifying a real error instead of blindly following a faulty premise.
- The solver sees the task and locked instruction context, never the rubric or
  expected result.
- The LLM judge is the only semantic scorer. Framework code only validates its
  typed result, aggregates votes, and computes weighted arithmetic.
- Optimization compares candidates by mean out-of-fold score. It has no
  numerical acceptance cutoff, permanent validation split, or test-access gate.

There is no raw prompt engine, deterministic evaluator plugin, legacy reader,
or user-facing engine selector.

## Install

DSPy is a required base dependency:

```bash
uv sync --python 3.12
uv run ms-agent-eval --help
```

## Create a workspace

Use the guided setup to inspect the repository and select its instructions and
skills interactively. The same flow configures the direct API/model values,
runtime, repetitions, initial case coverage, and optional optimization:

```bash
uv run ms-agent-eval setup
```

For automation and CI, provide the complete configuration explicitly:

```bash
uv run ms-agent-eval init \
  --id example-evaluation \
  --repo https://github.com/example/project \
  --ref v1.2.3 \
  --global-instructions AGENTS.md \
  --skills-directory .agents/skills \
  --cases cases \
  --endpoint llama-cpp-17-18081 http://192.168.1.17:18081/v1 2 \
  --endpoint llama-cpp-17-18080 http://192.168.1.17:18080/v1 2 \
  --endpoint llama-server-10-11436 http://192.168.1.10:11436/v1 4 \
  --solver-model solver-model \
  --solver-deployment-revision solver-deployment-v1 \
  --solver-reasoning-effort high \
  --judge-model judge-model \
  --judge-deployment-revision judge-deployment-v1 \
  --judge-reasoning-effort none
```

The generated schema-v11 `workspace.yaml` has these top-level concerns:

1. `executors` and `models`: reusable transport and model definitions.
2. `stages`: the executor, model, and budget assigned to each portable stage.
3. `evaluation`: repository, global instructions, exactly one skill selection,
   case-authoring policy/sources, case directory, split policy, and LLM judge.
4. `experiments`: solver program, runtime, normal evaluation, and optional DSPy
   optimization.

Use `skills.files` instead of `skills.directory` when evaluating only an exact
list of `SKILL.md` files. Both-or-neither is rejected.

Optional tutorials, real projects, and separately versioned contract sources
belong under `evaluation.case_authoring.sources`. Each URL/ref is independently
snapshotted outside Git; selected paths reach Codex under
`references/<source-id>/...` and participate in one combined provenance hash.

Case-bank readiness is also workspace configuration. New workspaces default to
at least eight reviewed cases and four distinct leakage groups per skill:

```yaml
evaluation:
  cases:
    directory: cases
    coverage:
      catalog: cases/coverage.yaml
      minimum_cases_per_skill: 8
      minimum_leakage_groups_per_skill: 4
```

The generated catalog starts as `draft`. Codex must read every selected skill,
record all independently testable capabilities, and mark it `complete` before
scored-run readiness. Requested case counts are lower bounds; the authoring
campaign expands until every capability has its required primary examples.
Every new workspace also includes `cases/README.md`, which explains this
contract beside the catalog. See the general
[capability coverage contract](docs/evaluation/capability-coverage.md) for the complete
schema, readiness calculation, authoring procedure, and provenance rules.

## Configure executors, models, and stages

API locations and model names are separate durable configuration. Stages bind
one executor to an optional model:

```yaml
executors:
  lan-inference:
    type: endpoint
    adapter: openai_compatible
    endpoints:
      - id: llama-cpp-17-18081
        base_url: http://192.168.1.17:18081/v1
        max_concurrent_requests: 2
      - id: llama-cpp-17-18080
        base_url: http://192.168.1.17:18080/v1
        max_concurrent_requests: 2
      - id: llama-server-10-11436
        base_url: http://192.168.1.10:11436/v1
        max_concurrent_requests: 4

models:
  solver:
    name: qwen3.6:27b-coding
    deployment_revision: qwen3.6-27b-coding-local-2026-09-13
    reasoning_effort: high
    parameters: {temperature: 0.2}
  judge:
    name: qwen3.6:27b-coding
    deployment_revision: qwen3.6-27b-coding-local-2026-09-13
    reasoning_effort: none
    parameters: {temperature: 0.0}

stages:
  solve:
    executor: lan-inference
    model: solver
    reuse: {policy: exact}
    budget: {model_calls: 1, tokens: 32000, wall_seconds: 600}
  judge:
    executor: lan-inference
    model: judge
    reuse: {policy: exact}
    budget: {model_calls: 3, tokens: 64000, wall_seconds: 600}
```

The coordinator runs ready packets through a bounded local thread pool. Each
endpoint enforces its own request limit, so this example can issue at most eight
requests concurrently (2 + 2 + 4). The model's `deployment_revision` attests that
every endpoint in the pool serves the same semantic deployment; heterogeneous
deployments require separate executors and model identities.

Judge validation uses that same pool at request granularity. Each fixture and
judge repetition is an independent request; five fixtures with three repetitions
produce 15 concurrent-ready votes, followed by deterministic median/majority
aggregation. The terminal reports every completed vote and its selected endpoint.

Only credentials remain process-local. When a provider requires a key, add
`api_key_env: OPENAI_API_KEY` to that endpoint item and define that secret in
the ignored `.env` file. Runtime state defaults to
`~/ms_agent_eval/<workspace-id>`; `workspace.data_root` may explicitly select
another external path.

Every stage is content-addressed. `exact` reuses a previously validated result
with the same semantic inputs, program, deployment, parameters, execution
environment, and output contract. `refresh` deliberately executes again.
`replicate` is available for solver variance studies and requires an explicit
sample count. A model alias is not a safe cache boundary, so
`deployment_revision` must change whenever local weights, quantization,
tokenizer, chat template, or serving behavior changes. See
[Incremental execution and reuse](docs/execution/incremental-execution.md).
Endpoint topology, scheduling, and provenance are documented in
[Pooled endpoint execution](docs/execution/openai-compatible-api.md).

Starting a run also freezes its selected workspace configuration and every
loaded case, split, judge-validation, catalog, policy, and source-snapshot identity
into an immutable revision bundle. `run resume <run-id>` always continues that
stored revision—even after relevant workspace edits—while `run baseline`
compiles the edited workspace as a new revision and reuses only compatible work.

## Full flow

```bash
uv run ms-agent-eval validate --workspace workspace.yaml
# Ask Codex to use the case-authoring skill with this workspace and campaign goal.
uv run ms-agent-eval inspect --workspace workspace.yaml
# Render the real DSPy requests and check native token fit/capacity without generation.
uv run ms-agent-eval preflight baseline --workspace workspace.yaml
# While coverage is incomplete, exercise judge validation, solving, and judging on
# train cases without producing benchmark metrics.
uv run ms-agent-eval run baseline --pilot --workspace workspace.yaml
# After validation reports ready_for_scored_run: true:
uv run ms-agent-eval run baseline --workspace workspace.yaml
uv run ms-agent-eval run optimize-few-shot --workspace workspace.yaml
```

`preflight` makes zero generation calls and creates no run or result artifacts. It
shows the configured endpoint slots, the exact-fit and dispatchable parallel
capacity, endpoint context windows, largest rendered request, and minimum
dispatch waves. Judge requests are reported as deferred until their solver
responses exist; this is explicit because candidate-response size is part of
the real judge request.

A pilot is immutable and explicitly records `benchmark_valid: false`. It never
loads test or challenge cases, cannot run optimization, retains per-case judge
diagnostics, uses one diagnostic judge vote per fixture/case, and omits
aggregate mean-score fields. Resume or operate its packets with
the same `--pilot` flag.

Every full run is resumable orchestration over the same public stage commands:

```bash
uv run ms-agent-eval run baseline --through solve --workspace workspace.yaml
uv run ms-agent-eval stage list --run <run-id> --workspace workspace.yaml
uv run ms-agent-eval run resume <run-id> --workspace workspace.yaml
```

For an external harness, prepare or claim a packet and submit a JSON result with
`stage prepare`, `stage claim`, and `stage submit`. Packet, result, transition,
model, executor, and usage evidence is append-only under the external data root.

Generated snapshots, solver calls, judge calls, compiled
state, results, and reports remain outside Git. Only `workspace.yaml`, reviewed
case packages, `case-authoring.md`, split assignments, and reviewed
reference-labelled judge validation inputs are versioned. The Codex
case-authoring campaign creates the cases, grouped splits, and judge-validation
fixtures together; judge validation is not a later manual phase.

Ask Codex for narrow campaigns covering different capabilities, sources,
difficulties, and archetypes until `validate` reports that every skill satisfies the configured
case and leakage-group minima. Those minima prevent a one-case-per-skill bank
from being treated as evaluation- or training-ready; they remain a floor, not a
guarantee of enough data for a production fine-tune.

Start with the [documentation map](docs/index.md) or jump directly to
[Getting started](docs/experiments/getting-started.md). The task-oriented guides
separate [experiment setup](docs/experiments/index.md),
[training and optimization](docs/training/index.md),
[evaluation](docs/evaluation/index.md), and
[execution](docs/execution/index.md).

## Storage layout

There are two locations with different ownership. The Git repository contains
reusable code and reviewable experiment definitions:

```text
repository/
├── .agents/skills/             framework authoring workflows
├── src/ms_agent_eval/          repository-agnostic library and CLI
├── experiments/
│   └── <workspace>/
│       ├── workspace.yaml      target, instructions, cases, models, runtime
│       ├── case-authoring.md   versioned Codex authoring policy
│       ├── .env.example        optional credential-name template
│       ├── cases/              coverage contract, reviewed cases, and splits
│       └── judge-validation/  authored and reviewed judge fixtures
├── tests/                      generic framework acceptance tests
├── docs/                       general guides and architecture records
└── .implementation_tasks/      local ignored implementation plans
```

Mutable and generated data belongs outside Git under the workspace data root:

```text
~/ms_agent_eval/<workspace-id>/
├── operations/                 internal, machine-oriented execution data
│   ├── snapshots/              immutable target and reference snapshots
│   ├── blobs/sha256/           content-addressed responses, traces, and state
│   ├── manifests/              locks, model calls, runs, and scores
│   └── tmp/                    atomic staging
└── summary/runs/<run-id>/
    ├── summary.md              deterministic run overview
    ├── results.csv             one row per judged case repetition
    ├── results.json            canonical derived report
    └── cases/                  readable per-case evidence
```

Every terminal experiment creates this report bundle automatically, without an
LLM summarization call. Existing runs can be rendered again with
`ms-agent-eval report build --run <run-id> --workspace <workspace.yaml>`.

There is no root-level `cases/`, `sdk/`, or `runs/` directory. Cases belong to
their experiment workspace; repository snapshots and every generated run
artifact belong to the external data root. Adding a target means adding a new
workspace, not changing `src/ms_agent_eval` or copying an SDK into this repo.

## Verify

```bash
uv run ruff check src tests
uv run pytest
uv lock --check
uv build
```
