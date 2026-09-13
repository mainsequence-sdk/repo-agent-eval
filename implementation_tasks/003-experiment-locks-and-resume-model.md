# 003 — Experiment Definitions, Immutable Locks, and Resume Model

Status: Implemented on 2026-08-19
Priority: P0 / execution identity
Depends on: task 002
Unblocks: tasks 004–006 and 009–012

## Outcome

Editable experiment definitions now expand into fully deterministic job plans
that bind target, snapshot, instruction bundle, suite, compatibility mapping,
split manifest, program, provider, runtime, storage, optimizer, repetition, and
all source-document hashes. A serialized lock can be reloaded only when every
job identity and the whole-lock hash still match.

The core also defines an implementation-independent lifecycle and resume model.
Completed jobs are skipped, unseen jobs execute, budget-exhausted jobs remain
terminal, failed jobs retry only under policy, and apparently running jobs can
be reclaimed only through an explicit decision. Persisting these compare-and-
swap transitions transactionally is task 005's SQLite responsibility.

## Implemented Scope

### Exact matrix resolution

`compatibilities` is an explicit experiment axis. During planning, invalid
cross-product combinations are removed only by declared identity relationships:

- snapshot `target_id` must equal target id;
- bundle id must exist in the target definition;
- compatibility `snapshot_id` and `suite_id` must equal the selected records;
- compatibility suite version must equal the selected suite version;
- compatibility must cover every suite case exactly once;
- compatibility cannot change a case's `(bundle_id, unit_id)`;
- every mapped `(bundle_id, unit_id)` must exist in the immutable snapshot;
- a referenced split manifest must assign every suite case exactly once.

There is no repository-path fallback and no global case-id lookup.

### Immutable experiment lock

The generated lock records:

- schema and planner versions;
- experiment id, kind, and specification hash;
- selected storage and optional optimizer ids;
- SHA-256 hash of the workspace and every selected configuration document;
- ordered jobs with immutable identities and content hashes;
- a content hash over the complete lock identity.

Each job id combines its stable matrix ordinal with the first twelve hex
characters of its content hash. Reloading verifies the job id, job hash,
planner version, and full experiment lock hash. Any edited provider, target,
repetition, or other bound field invalidates the lock.

### Lifecycle and resume semantics

`JobState.transition()` enforces:

```text
planned -> running
failed -> running
running -> completed | failed | budget_exhausted
completed -> terminal
budget_exhausted -> terminal
```

Transitions include an expected status, monotonic version, attempt number,
structured error kind, and UTC update timestamp. This supplies the compare-and-
swap contract used by the transactional metadata backend in task 005.

Every experiment run uses a UUID-based id and is bound to one exact lock hash.
`plan_resume()` rejects unknown jobs and job-hash mismatches before returning an
ordered action for each locked job:

- `execute`: never started, planned, eligible failed retry, or explicitly
  reclaimed running job;
- `skip`: completed;
- `blocked`: budget exhausted, failed under no-retry policy, or running without
  explicit reclamation.

## Verification Evidence

On CPython 3.12.8:

```text
ruff check packages/agent-eval-core
All checks passed!

pytest -q packages/agent-eval-core/tests
15 passed
```

Tests cover deterministic two-target planning, exact compatibility and split
resolution, JSON lock round-trip, tamper rejection, collision-resistant run
ids, legal/illegal transitions, completed-job skipping, missing-job execution,
foreign identity rejection, and explicit reclamation of running work.

## Acceptance Criteria

- [x] The editable matrix contains no resolved execution state.
- [x] Planning produces one deterministic, immutable lock.
- [x] Compatibility and split identities are bound before execution.
- [x] Serialized locks detect job-level and whole-lock tampering.
- [x] Run instances use UUID identities and bind one exact lock hash.
- [x] Resume never duplicates a completed job.
- [x] Running work is not reclaimed implicitly.
- [x] State transitions expose a compare-and-swap version contract.
- [x] No model, repository code, or Docker command runs during planning.

## Deferred Boundary

The lifecycle is intentionally storage-neutral. Task 005 implements atomic
artifact publication and SQLite transactions against these contracts. Task 006
uses them while controlling Docker jobs; task 009 uses them around model calls.
