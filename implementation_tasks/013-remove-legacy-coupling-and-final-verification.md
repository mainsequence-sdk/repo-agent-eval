# 013 — Remove Legacy Coupling and Complete the Refactor

Status: Superseded by task 017; retained only as historical implementation evidence
Priority: P0
Depends on: tasks 002–012

> Superseded cleanup state: task 015 removed the schema-v0 root trees after
> proving byte equivalence and preserving the minimal regression fixtures.

## Outcome

The root project is now `ms-agent-eval`, requires Python 3.12+, and has no
runtime dependency on `mainsequence` or any evaluated library. Development
dependencies point to the five workspace packages. Core still installs alone
with only PyYAML; DSPy and Ollama remain optional package boundaries.

The installed-package snapshot generator, repository-local run creator, direct
Ollama case runner, monolithic evaluator, and hardcoded orchestration generator
were retired. Their outputs remain preserved in schema-v0 history and Git
history; new source acquisition, execution, evaluation, and reporting go through
the generic packages and external data plane.

Stale committed `sdk_agent_training.egg-info` metadata was removed. `uv.lock` is
regenerated from the Python 3.12+ workspace and contains no `mainsequence`
distribution. Generated package metadata is build output, not source.

## Compatibility and Data Safety

- Existing authored `cases/v1` and `cases/v2` content is retained.
- Namespaced pack copies remain the current definition path.
- Existing `sdk/` and `runs/sdk/` bytes are retained read-only.
- Task 012 exported both legacy trees externally with complete path/hash
  inventories before legacy execution paths were removed.
- The raw legacy program and historical request-parity test remain, preserving
  replay evidence without preserving the unsafe runner.

## Documentation

The root README, structure, conventions, source, and Ollama guides now describe
the neutral configuration-driven workflow. MainSequence is presented only as a
first-party experiment workspace with its own evaluator. No guide instructs users to install an
evaluated library, infer an installed SDK version, or commit new run results.

## Final Acceptance

The final verification must establish:

- `uv sync` selects CPython 3.12+ and installs all workspace packages;
- the root lock contains no `mainsequence` package;
- core imports in an isolated environment without DSPy/Ollama/target packages;
- all Ruff and production tests pass;
- the optional live Docker integration passes on the pinned Python 3.12 image;
- Main Sequence workspace validation/planning and evaluator calibration pass;
- Main Sequence optimization preflight fails honestly until coverage exists;
- `git status` contains no generated runtime results from the verification.

The live Ollama gate remains external when no endpoint/model is configured and
must be reported as such rather than marked passed.

Final evidence:

```text
CPython: 3.12.8
uv workspace resolution: 79 packages, no mainsequence distribution
Ruff: all checks passed
production tests: 75 passed, 1 optional Docker test skipped
live pinned Docker acceptance: 1 passed
workspace configuration: valid (1 target, 2 suites, 2 splits, 1 experiment)
Main Sequence evaluator calibration: valid (74 cases; 1 active, 5 manual, 68 not evaluable)
all six workspace distributions: sdist and wheel build passed
```
