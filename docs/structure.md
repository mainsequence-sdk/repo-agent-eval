# Repository Structure

## Library

`src/ms_agent_eval/` is one installable distribution:

```text
core/
  workspace.py             schema-v4 manifest and three-role identity
  sources.py               GitHub tag/commit resolution
  snapshots.py             immutable external instruction snapshots
  case_builder.py           external drafts and explicit promotion
  evaluation.py            cases, rubrics, calibration, LLM-judge aggregation
  planning.py              generated indexes and experiment locks
  runner.py                shared evaluate/optimize DSPy path
  execution.py             digest-pinned isolated Docker target execution
  storage.py               external content-addressed artifacts
  reporting.py             current-schema summaries and regressions
programs/dspy/              typed builder, solver, and judge programs
providers/openai_compatible/ observed DSPy LM binding
```

There is no engine selector, raw program package, target-specific library code,
evaluator plugin registry, deterministic judge, or legacy compatibility layer.

## Experiment workspace

An experiment is deliberately small:

```text
experiments/<name>/
├── workspace.yaml         the only framework configuration
├── case-authoring.md      durable DSPy case-builder instructions
├── .env.example           optional credential-name template
├── cases/
│   ├── splits.yaml        group-to-split assignments
│   └── <skill>/<case>/    promoted prompts, expectations, rubrics, provenance
└── judge-calibration/
    ├── manifest.yaml      human labels and accepted score ranges
    └── *.md               calibration candidate responses
```

The manifest has `evaluation` and `experiments` sections and explicitly points
to `case-authoring.md`. The Markdown file is versioned experiment policy, not
generated data: its exact content becomes the case-builder DSPy signature
instructions and participates in builder provenance. Snapshot ids, skill
indexes, case indexes, compatibility maps, normalized program hashes, model
hashes, calibration hashes, and experiment locks are generated evidence—not
additional configuration directories.

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
snapshots/                 immutable selected repository content
case-drafts/               validated/rejected builder packages and provenance
tmp/                       atomic staging
```

No generated result is committed. Only explicit case promotion crosses from the
external data plane into the experiment workspace.

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
