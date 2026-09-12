# Repository Structure

## Library

`src/ms_agent_eval/` is one installable distribution:

```text
core/
  workspace.py             schema-v6 manifest and two runtime LLM roles
  sources.py               GitHub tag/commit resolution
  snapshots.py             immutable external instruction snapshots
  authoring.py              immutable evidence context for Codex authoring
  evaluation.py            cases, rubrics, calibration, LLM-judge aggregation
  planning.py              generated indexes and experiment locks
  runner.py                shared evaluate/optimize DSPy path
  execution.py             digest-pinned isolated Docker target execution
  storage.py               external content-addressed artifacts
  reporting.py             current-schema summaries and regressions
programs/dspy/              typed solver and judge programs
providers/openai_compatible/ observed DSPy LM binding
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
│   └── <skill>/<case>/    promoted prompts, expectations, rubrics, provenance
└── judge-calibration/
    ├── manifest.yaml      human labels and accepted score ranges
    └── *.md               calibration candidate responses
```

The manifest has `evaluation` and `experiments` sections, explicitly points to
`case-authoring.md`, and may declare immutable reference repositories under
`evaluation.case_authoring.sources`. The Markdown file is versioned experiment
policy, not generated data: its exact content participates in case-authoring
provenance. Snapshot ids, skill
indexes, case indexes, compatibility maps, normalized program hashes, model
hashes, calibration hashes, and experiment locks are generated evidence—not
additional configuration directories.

`evaluation.cases.coverage` points to the capability catalog and sets additional
readiness floors for authored cases and distinct leakage groups per skill.
Codex decomposes every selected skill into testable capabilities, gives each
case one primary capability and at most two supporting capabilities, and
reviews existing packages to keep a campaign semantically diverse. Numeric
counts cannot hide a missing semantic capability.

The complete general contract is documented in
[Capability coverage](capability-coverage.md). Repository-specific catalogs are
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
blobs/sha256/              immutable requests, responses, traces, and state
manifests/                 locks, calibration, evaluations, and run results
snapshots/                 immutable target and reference repository content
tmp/                       atomic staging
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
docs/                      guides and historical implementation records
```

There are no root-level cases, runs, reports, runtime profiles, packages, SDK
copies, or spike directories.
