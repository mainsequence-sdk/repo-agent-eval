# 014 — Single `ms-agent-eval` Distribution and Standard `src` Layout

Status: Implemented and tightened on 2026-08-19
Priority: P0 / repository architecture correction
Supersedes: the multi-distribution layout introduced by tasks 002 and 009

## Outcome

The repository publishes one Python distribution named `ms-agent-eval`. All
reusable, target-neutral code lives under `src/ms_agent_eval/`:

```text
src/ms_agent_eval/
├── core/                    schemas, planning, execution, storage, evaluator loading
├── programs/
│   ├── raw/                 exact message rendering
│   └── dspy/                DSPy programs and governed optimization
└── providers/
    └── ollama/              Ollama transport and DSPy binding
```

Target-specific evaluators are not part of the wheel. The MainSequence evaluator
is owned by `experiments/mainsequence-sdk/evaluators/mainsequence/` and selected
through an explicit, hash-locked evaluator profile.

## Packaging decisions

- Distribution: `ms-agent-eval`.
- Import namespace: `ms_agent_eval`.
- Baseline: Python 3.12 or newer.
- Required dependency: PyYAML only.
- Optional DSPy dependency: `ms-agent-eval[dspy]`.
- Only installed CLI: `ms-agent-eval`.
- Build backend: Hatchling with one wheel rooted at `src/ms_agent_eval`.

No compatibility aliases or transitional entry points remain. The base wheel
does not import DSPy, an evaluated target, or any experiment evaluator.

## Verification

```bash
uv lock --check
uv build
uv run ruff check src tests
uv run pytest
```

A clean Python 3.12 environment must install the base wheel, import the generic
library, and contain neither DSPy nor a MainSequence package.
