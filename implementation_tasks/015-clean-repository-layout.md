# 015 — Final Repository Layout Cleanup

Status: Implemented on 2026-08-19
Priority: P0 / repository hygiene
Depends on: tasks 008, 012, 014

## Outcome

The transitional migration layout was removed. The active repository now has
one library source tree, one experiment-workspace root, one test tree, and one
documentation tree:

```text
.agents/
src/
experiments/
tests/
docs/
pyproject.toml
uv.lock
README.md
```

Removed from the active root:

- `spikes/`: disposable DSPy feasibility project and its 203 MB virtual
  environment;
- `cases/`: byte-identical duplicate of the suites in the MainSequence
  experiment workspace;
- `sdk/`: schema-v0 copied repository instructions superseded by configured
  GitHub sources, external snapshots, and compact committed locks;
- `runs/` and `reports/`: generated data that belongs outside Git;
- `runtime-profiles/`: the real profile belongs to its experiment workspace and
  the generic executor copy is a test fixture;
- `experiment-packs/`: renamed to the clearer `experiments/` root;
- duplicated `docs/training_sources/`;
- one-time suite migration scripts.

The typo `docs/implementaiton_task/` was corrected to
`docs/implementation_tasks/`.

## Preservation Evidence

Before cleanup, recursive byte comparisons passed for:

- top-level `cases/v1` versus the MainSequence workspace `suites/v1`;
- top-level `cases/v2` versus the MainSequence workspace `suites/v2`;
- top-level training-source plans versus workspace training-source plans;
- the root Python 3.12 runtime profile versus the workspace runtime profile.

The old schema-v0 run had six inputs needed by regression tests. They were
retained under `tests/fixtures/legacy-run-v0/`: manifest, evaluation, historical
response, rendered system/user prompts, and canonical Ollama request. The
unused provider response and run directory were removed from the workspace.

Because broad permanent deletion was rejected by the safety gate, the legacy
trees other than the explicitly disposable spike were moved to
`/private/tmp/ms-agent-eval-repo-cleanup-backup-20260819/`. Tracked files remain
recoverable from Git history as well.

## Source-Reference Migration

Case metadata no longer cites deleted `sdk/4.4.5/...` paths. It cites immutable
GitHub blob URLs at commit
`3b5a20a344cec0c960351dc3c601d32a66a8b46e`. Training notes resolve inside
`experiments/mainsequence-sdk/sources/`.

Repository-local skills were updated so future case and version workflows use
the experiment workspace, external snapshots, and external run storage. They
explicitly prohibit recreating the transitional root directories.

## Acceptance Checks

```bash
uv lock --check
uv run ruff check src tests
uv run pytest
uv build
uv run ms-agent-eval config validate \
  --workspace experiments/mainsequence-sdk/workspace.yaml
```

Also require:

- both local skill folders pass `quick_validate.py`;
- no active code, tests, root documentation, or skills reference removed live
  paths;
- the committed suite counts remain v1 = 55 and v2 = 74;
- the base wheel installs and imports on CPython 3.12 without DSPy or the
  MainSequence SDK.

## Acceptance Evidence

- Workspace validation: valid, with 1 target, 1 snapshot, 2 suites, 2 splits,
  and 2 compatibility documents.
- Ruff: passed for `src` and `tests`.
- Tests: 75 passed and 1 optional Docker test skipped in the default run.
- Live Docker boundary: 1 passed.
- Both repository-local skills: valid.
- Build: one `ms_agent_eval-0.1.0` sdist and wheel.
- Clean CPython 3.12.8 base-wheel import: passed with DSPy and the MainSequence
  SDK absent.
