# 016 — Canonical Experiment Workspace Layout

Status: Superseded by task 017
Priority: P0 / ownership and repository-layout correction

## Outcome

Experiment assets now live under one workspace instead of being scattered
across repository-root folders. Suite metadata, split governance, and authored
cases are co-located by version:

```text
experiments/<workspace>/
├── workspace.yaml
├── .env.example
├── targets/
├── snapshots/
├── sources/
├── suites/
│   └── <version>/
│       ├── suite.yaml
│       ├── split.json
│       └── units/<unit>/cases/<case>/
├── compatibility/
├── programs/
├── providers/
├── runtimes/
├── evaluators/<evaluator>/
│   ├── evaluator.yaml
│   ├── plugin.py
│   └── calibration/
├── optimizers/
├── storage/
└── plans/
```

Generated snapshots, runs, model calls, evaluations, compiled programs,
databases, and reports remain outside Git. The CLI resolves the data root from
an explicit argument, the process environment, or the ignored `.env` beside
`workspace.yaml`, then falls back to `~/ms_agent_eval/<workspace-id>`.
`.env.example` documents an optional local override.

## Library boundary

The generic wheel now provides the evaluator contract and trusted plugin loader.
The MainSequence evaluator was removed from `src/` and moved into its experiment
workspace. Plans select an evaluator profile explicitly; the evaluator id and
composite evaluator-tree hash are therefore part of every immutable experiment
lock. Suite hashes likewise cover authored prompts, expected responses, rubrics,
unit metadata, and the suite index; split documents retain their own hash.

## Clean break

No compatibility aliases remain for:

- the removed target-specific Python import;
- the removed target-specific console command;
- standalone split catalogs;
- `skills/` as the authored-suite directory name;
- `training_sources/`;
- the old data-root and Docker-test environment prefixes.

## Validation

Workspace validation loads every indexed case, checks required case files,
requires suite ids to match case ids, requires complete co-located split
assignments, validates evaluator module paths, and ensures storage placeholders
use the workspace's configured data-root environment variable.

Acceptance requires:

```bash
uv run ms-agent-eval config validate \
  --workspace experiments/mainsequence-sdk/workspace.yaml
uv run ms-agent-eval evaluator validate mainsequence-rules-v1 \
  --suite mainsequence-agent-skills-v2 \
  --workspace experiments/mainsequence-sdk/workspace.yaml
uv run ruff check src tests experiments/mainsequence-sdk/evaluators
uv run pytest
uv lock --check
uv build
```

Observed evidence on CPython 3.12.8:

- Ruff passed for the library, tests, and experiment evaluator.
- Workspace and evaluator validation passed (129 indexed cases total; 74 in v2).
- Pytest passed with 79 tests and one optional Docker test skipped.
- The live Docker-only check could not run because the local daemon was
  unavailable; preflight failed before container execution.
- The wheel contains no target extension and exposes only `ms-agent-eval`.
- A clean base install contains PyYAML, no DSPy, and no MainSequence package.
