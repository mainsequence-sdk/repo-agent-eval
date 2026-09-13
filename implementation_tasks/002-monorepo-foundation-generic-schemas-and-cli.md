# 002 — Monorepo Foundation, Generic Schemas, and CLI Shell

Status: Implemented on 2026-08-19
Priority: P0 / foundational
Depends on: task 001 decision and accepted task 000 architecture
Unblocks: tasks 003–013

> Superseded layout: task 014 consolidated the temporary multi-distribution
> workspace into the single `ms-agent-eval` distribution under
> `src/ms_agent_eval/`. The details below record the state when task 002 was
> completed, not the current installation layout.

## Outcome

The repository now contains an independently installable `agent-eval-core`
package under `packages/agent-eval-core`. It owns neutral, versioned domain
models, workspace loading, deterministic hashing, experiment matrix planning,
and the initial `agent-eval` CLI. It neither imports nor depends on
`mainsequence`.

The package requires Python 3.12 or newer. A clean installation was verified
with CPython 3.12.8 using only `agent-eval-core` and PyYAML; the environment did
not contain `mainsequence`.

## Implemented Scope

### Monorepo foundation

- Added `packages/*` as uv workspace members.
- Added the `agent-eval-core` distribution with a `src` layout.
- Kept the legacy root project operational during migration.
- Added a package-specific README, pytest configuration, and Ruff tooling.

### Versioned neutral models

Implemented immutable models and validation for:

- workspace roots and external data-root policy;
- GitHub target, tag/commit refs, instruction bundles, ordered global context,
  explicit unit entries, and directory locators;
- immutable snapshot locks and locked instruction-unit records;
- suites, grouped split manifests, and snapshot-to-suite compatibility maps;
- raw or DSPy-neutral program specifications and compiled JSON-state manifests;
- provider, runtime, optimizer, and storage profiles;
- benchmark/optimization experiments, planned jobs, and experiment locks;
- model-call, program-result, run, evaluator-identity, and evaluation-result
  records.

Every authored document has `schema_version: 1`. Generated snapshot, split,
compatibility, program, and experiment identities use deterministic canonical
JSON SHA-256 hashes. Declared hashes on generated lock documents are verified
when loading.

### Safety and validation invariants

- Repository paths must be relative POSIX paths and reject absolute paths,
  traversal, NULs, and drive-qualified paths.
- Persisted commit refs require a full lowercase 40-character SHA.
- GitHub sources require an HTTPS `github.com` repository URL.
- Instruction roots and entries are explicit; core contains no fallback roots
  such as `.agents/skills` or `agent_scaffold/skills`.
- Empty suites and empty explicit unit sources are rejected.
- Duplicate unit, bundle, case, and split identifiers are rejected.
- A group cannot occur in more than one train/development/test/challenge split.
- Benchmark experiments cannot declare an optimizer; optimization experiments
  must declare one.
- Artifact storage in the initial local profile must be rooted at the external
  `MS_AGENT_EVAL_DATA_ROOT`.

### Planning and CLI

Implemented:

```text
agent-eval config validate --workspace <workspace.yaml>
agent-eval experiment plan <experiment-id> --workspace <workspace.yaml>
  [--output <external-lock.json>]
```

The planner expands the Cartesian matrix, removes invalid target/snapshot and
target/bundle pairs, assigns deterministic job ids, hashes every selected
configuration document, and emits an immutable experiment lock without
executing any job. CLI output is machine-readable JSON. File output uses an
fsync plus atomic replace.

## Test Fixture

The package includes a target-neutral workspace fixture with:

- target `alpha`, whose configured instruction root is `.agents/skills`;
- target `beta`, whose instruction is an explicit `docs/agent/coding.md` file;
- immutable snapshot locks for each target;
- a suite, protected split manifest, and two compatibility maps;
- raw program, fake provider, response-only runtime, optimizer, external
  storage profile, and two-target benchmark experiment.

The plan contains exactly two valid jobs:

```text
(alpha, alpha-v1)
(beta, beta-v1)
```

This proves that a hidden skill root is configuration data rather than a core
default and that two differently structured repositories use one planner.

## Verification Evidence

On 2026-08-19:

```text
ruff check packages/agent-eval-core
All checks passed!

pytest -q packages/agent-eval-core/tests
9 passed

clean install interpreter: CPython 3.12.8
agent-eval-core: 0.1.0
mainsequence: absent
```

The root uv lock resolves the new workspace member as
`agent-eval-core==0.1.0`.

## Deferred by Design

Task 002 does not fetch repositories, create snapshots, persist job state,
execute containers, call models, evaluate responses, or optimize programs.
Those are the bounded responsibilities of tasks 003–011. The models introduced
here are contracts for those implementations and may be extended only through
versioned, backward-compatible changes during the migration.

## Acceptance Criteria

- [x] Neutral package installs without `mainsequence`.
- [x] Python floor is 3.12 and is tested on CPython 3.12.8.
- [x] Generic workspace fixtures validate.
- [x] No Main Sequence behavior or path default exists in core source.
- [x] Two differently structured targets expand through one matrix planner.
- [x] Planning performs no target execution or model call.
- [x] Generated locks have deterministic, JSON-safe content hashes.
- [x] Existing legacy scripts and case-bank edits remain untouched.
