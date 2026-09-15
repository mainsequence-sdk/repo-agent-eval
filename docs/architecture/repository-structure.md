# Repository Structure

## Library

`src/ms_agent_eval/` is one installable distribution:

```text
core/
  workspace.py             schema-v17 deployments, arms, recorder queues, reuse, and stages
  sources.py               GitHub tag/commit resolution
  snapshots.py             immutable external instruction snapshots
  authoring.py              immutable evidence context for Codex authoring
  evaluation.py            cases, rubrics, judge validation, LLM-judge aggregation
  planning.py              generated indexes and experiment locks
  stages.py                immutable packets/results and append-only lifecycle
  lifecycle.py             canonical run-level lifecycle events
  recorder.py              recorder-neutral batches, adapters, workers, and bindings
  recorder_queue.py        durable intents, coalescing, leases, retries, and cursors
  recording_projection.py deterministic evidence-to-recorder projection
  endpoint_pool.py         threaded replica permits and cross-process slot locks
  coordinator.py           resumable staged evaluate/optimize orchestration
  stage_executors.py       transport-neutral harness rendering contract
  runner.py                public full-run facade over the coordinator
  execution.py             digest-pinned isolated Docker target execution
  storage.py               external content-addressed artifacts
  reporting.py             current-schema summaries and regressions
programs/dspy/              typed solver and judge programs
providers/openai_compatible/ observed DSPy LM binding
adapters/codex_cli.py       isolated, subscription-backed Codex CLI harness
adapters/mlflow.py          optional MLflow 3.16 recorder adapter
```

There is no engine selector, raw program package, target-specific library code,
evaluator plugin registry, deterministic judge, or legacy compatibility layer.

## Experiment workspace

An experiment is deliberately small:

```text
experiments/<name>/
├── workspace.yaml         the only framework configuration
├── case-authoring.md      durable Codex authoring instructions
├── .env.example           optional credential-name template
├── cases/
│   ├── README.md           local explanation of the coverage contract
│   ├── coverage.yaml      required capabilities and primary-case depth
│   ├── splits.yaml        group-to-split assignments
│   └── <skill>/<case>/    coordinator metadata plus solver/ and judge/ surfaces
└── judge-validation/
    ├── manifest.yaml      authored reference labels for ordinal validation
    └── *.md               judge-validation candidate responses
```

The manifest has `evaluation` and `experiments` sections, explicitly points to
`case-authoring.md`, and may declare immutable reference repositories under
`evaluation.case_authoring.sources`. The Markdown file is versioned experiment
policy, not generated data: its exact content participates in case-authoring
provenance. Snapshot ids, skill
indexes, case indexes, compatibility maps, normalized program hashes, model
hashes, judge validation hashes, and experiment locks are generated evidence—not
additional configuration directories.

`evaluation.cases.coverage` points to the capability catalog and sets additional
readiness floors for authored cases and distinct leakage groups per skill.
Codex decomposes every selected skill into testable capabilities, gives each
case one primary capability and at most two supporting capabilities, and
reviews existing packages to keep a campaign semantically diverse. Numeric
counts cannot hide a missing semantic capability.

The same Codex authoring campaign populates `judge-validation/` after cases and
grouped splits exist. Those controlled candidate responses are versioned
experiment inputs. Actual judge-validation executions and judge outputs stay in the
external data plane.

The complete general contract is documented in
[Capability coverage](../evaluation/capability-coverage.md). Repository-specific catalogs are
instances of that blueprint, not alternate schemas.

The evaluated repository owns the solver instruction inventory. Authoring-only
reference repositories supply tutorials, real-project examples, or external
contracts. They are not copied into the experiment directory, and their files
are exposed through unambiguous `references/<source-id>/...` labels.

The selected skill source is exactly one of:

```yaml
skills:
  directory: .agents/skills
```

or:

```yaml
skills:
  files:
    - .agents/skills/one/SKILL.md
    - .agents/skills/two/SKILL.md
```

## External data plane

The default is `~/ms_agent_eval/<workspace-id>`:

```text
operations/                internal, machine-oriented execution data
  blobs/sha256/            immutable requests, responses, traces, and state
  manifests/               locks, judge validation, evaluations, and run results
  recorders/               delivery events, bindings, receipts, and errors
  snapshots/               immutable target and reference repository content
  tmp/                     atomic staging
summary/runs/<run-id>/     derived Markdown, CSV, JSON, and per-case reports
```

No generated result is committed. Codex-authored prompts, expected responses,
rubrics, and provenance are experiment inputs, so their case packages are
versioned in the workspace.

## Repository root

```text
.agents/skills/            local authoring workflows
src/                       installable library
experiments/               compact versioned workspaces
tests/                     generic synthetic tests
docs/                      general guides and architecture records
.implementation_tasks/     local ignored implementation plans
```

There are no root-level cases, runs, reports, runtime profiles, packages, SDK
copies, or spike directories.

## Documentation tree

```text
docs/
├── index.md                task-oriented documentation map
├── experiments/            setup, requirements, sources, and first run
├── training/               DSPy optimization, split boundaries, and outputs
├── evaluation/             coverage, readiness, judging, and results
├── execution/              executors and portable stage operation
├── architecture/           system design, repository layout, and ADRs
└── reference/              stable terminology and conventions
```

Historical implementation tasks are intentionally a sibling of `docs/`, not a
documentation section or source of the current behavioral contract.
