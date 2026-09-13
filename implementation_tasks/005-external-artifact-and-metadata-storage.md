# 005 — External Artifact and Metadata Storage

Status: Implemented on 2026-08-19
Priority: P0 / persistence
Depends on: tasks 002–004
Unblocks: tasks 006 and 009–012

## Outcome

The framework now separates immutable byte/manifests from transactional run
metadata through neutral `ArtifactStore` and `MetadataStore` protocols. The
initial local implementations use a content-addressed external filesystem and
SQLite. No raw prompt, response, snapshot, run, or evaluation artifact needs to
be written into the framework or evaluated repository.

## Filesystem Artifact Store

`FilesystemArtifactStore` provides:

- streamed SHA-256 blob ingestion through a temporary file;
- fsync followed by atomic publication;
- deduplication by content id;
- immutable, namespaced canonical-JSON manifests;
- idempotent writes when key and bytes match;
- rejection when an existing manifest key has different bytes;
- traversal-safe manifest keys and artifact references;
- size and SHA-256 verification on every read;
- explicit tamper detection.

Local layout:

```text
${MS_AGENT_EVAL_DATA_ROOT}/
├── blobs/sha256/<digest>
├── manifests/experiments/<run-id>/experiment.lock.json
├── metadata/agent-eval.sqlite
└── tmp/
```

The configured root is rejected when it is inside the Git workspace. The
workspace's `external_data_root_env` (default `MS_AGENT_EVAL_DATA_ROOT`) is the
configuration-level environment reference; CLI callers can use `--data-root`
as an explicit local override. Secret values are not placed in locks.

## SQLite Metadata Store

`SQLiteMetadataStore` initializes version-1 tables for experiment runs, jobs,
and job artifact references with foreign keys, a run/status index, WAL mode,
and a 30-second busy timeout.

Experiment creation is one `BEGIN IMMEDIATE` transaction that inserts the
run-to-lock binding and every planned job. A duplicate run rolls back without
partially adding jobs.

Job changes implement the Task 003 compare-and-swap contract:

- select exact `(run_id, job_id)` inside an immediate transaction;
- require expected status and version;
- apply the domain state-machine validation;
- update with both status and version in the SQL predicate;
- require exactly one updated row;
- commit or roll back the complete transition.

Artifact metadata has a foreign key to a real run/job and stores content id,
media type, byte length, and store-relative path. It never treats host paths as
portable run identity.

## CLI

```text
agent-eval experiment create <experiment-id> --workspace <workspace.yaml>
  [--data-root <external-directory>]
```

This command plans and hashes the experiment, creates a UUID run, publishes the
immutable lock externally, and transactionally initializes all job states in
SQLite. Its JSON output contains only the run identity and content-addressed
lock reference.

Cross-store atomicity is handled through immutable idempotency: a lock blob or
manifest published before a metadata failure is an unreferenced immutable
object that can be garbage-collected; it cannot expose a partially mutable run.

## Verification Evidence

On CPython 3.12.8:

```text
ruff check packages/agent-eval-core
All checks passed!

pytest -q packages/agent-eval-core/tests
35 passed
```

Tests cover blob deduplication, verified reads, byte tampering, immutable
manifest conflicts, traversal rejection, atomic run initialization, legal
status transitions, stale compare-and-swap failure, rollback on duplicate run,
foreign-key artifact validation, resume from persisted state, CLI run creation,
and rejection of workspace-local storage.

## Acceptance Criteria

- [x] Artifact and metadata stores are separate protocols.
- [x] Raw data is stored below an externally configured root.
- [x] Blobs and manifests are content-hashed, verified, and atomically published.
- [x] Manifest keys are immutable and traversal-safe.
- [x] Experiment run/job creation is transactional.
- [x] Job status/version transitions are transactional compare-and-swap updates.
- [x] Completed jobs loaded from SQLite are skipped by resume planning.
- [x] Artifact records reference real jobs and portable content identities.
- [x] No storage path is hardcoded to Main Sequence or repository-local runs.

## Deferred Boundary

S3-compatible artifacts and PostgreSQL metadata implement the same protocols
later if shared multi-machine operation is needed. They are not required for
the local Docker execution and migration proof in tasks 006–009.
