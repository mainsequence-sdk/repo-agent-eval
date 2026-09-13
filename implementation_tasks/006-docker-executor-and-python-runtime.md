# 006 — Docker Executor, Python Runtime, Isolation, and Evidence

Status: Implemented on 2026-08-19
Priority: P0 / untrusted execution boundary
Depends on: tasks 003–005
Unblocks: tasks 007, 009, and 010

## Outcome

Repository code can now run through a neutral `ExecutionBackend` only inside a
locked Docker runtime. The first profile uses Python 3.12 plus uv, pins the
multi-platform image by SHA-256 digest, disables runtime networking, applies
resource/security controls, and exports stdout, stderr, output files, and a
normalized evidence manifest to external content-addressed storage.

No framework model, evaluator, or source module imports target code on the
host.

## Locked Runtime Profile

Committed profile:

```text
tests/fixtures/runtime/python-uv-3.12.yaml
```

It currently pins:

```text
ghcr.io/astral-sh/uv:python3.12-bookworm-slim
sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58
```

Runtime schemas reject Docker tags without an `@sha256:<64 hex>` digest. The
profile records Python version, user, network mode, CPU, memory, PID, timeout,
and maximum output limits. Python 3.12 and uv were both executed in the pinned
container during the integration test.

## Container Isolation

Every execution uses argv-based `docker run` with:

- a random, non-user-controlled container name and `--rm`;
- `--network none`;
- CPU, memory, PID, and wall-time limits;
- read-only container root filesystem;
- all Linux capabilities dropped;
- `no-new-privileges:true`;
- unprivileged `10001:10001` user;
- constrained `noexec,nosuid,nodev` temporary filesystem;
- one staged target mount and one external output mount;
- no Docker socket, host network, host PID namespace, privileged flag, or
  workspace mount.

The executor rejects non-`none` networking until a separately designed
allowlisted proxy exists. A timeout force-removes the named container and
returns a structured `execution_timeout` result.

Target execution gets a writable disposable copy of repository content. A
`trusted_evaluator` role is a separate container invocation whose target input
is read-only. This prevents target dependencies or mutations from entering the
trusted evaluator process.

Input trees and output trees reject symlinks. Commands are NUL-free argv, never
shell text. Environment variable names and single-line values are validated;
values are supplied through a mode-0600 temporary env file and never included
in command evidence. Evidence records environment names only.

## Bounded Evidence Export

Docker stdout/stderr stream to temporary files instead of accumulating without
bound in the controlling process. Reads are capped at the runtime maximum;
truncation becomes `output_limit_exceeded`. The exported output tree is checked
for symlinks and byte size before being archived. The result contains only
portable `ArtifactReference` values for:

- stdout;
- stderr;
- optional output tar archive;
- canonical execution evidence manifest.

The evidence manifest records the image digest, role, argv, effective security
controls and limits, environment names, exit/status/error classification, and
content references. Host paths and environment values are omitted.

## Verification Evidence

Unit suite on CPython 3.12.8:

```text
ruff check packages/agent-eval-core
All checks passed!

pytest -q packages/agent-eval-core/tests
40 passed, 1 Docker integration skipped
```

Explicit Docker integration against Docker Engine 28.3.2:

```text
MS_AGENT_EVAL_RUN_DOCKER_TESTS=1 pytest .../test_execution.py -m docker
1 passed
```

The live probe asserted:

- interpreter version begins with `3.12`;
- `uv --version` succeeds;
- a connection attempt with `--network none` is blocked;
- output is exported through `/workspace/output` and recovered from the
  external tar artifact.

## Acceptance Criteria

- [x] Target commands execute in Docker, never the host framework process.
- [x] Runtime image is immutable by digest and contains Python 3.12 plus uv.
- [x] Runtime network defaults to and currently requires `none`.
- [x] CPU, memory, PID, time, log, and output limits are applied.
- [x] Root filesystem, capabilities, privileges, user, and tmpfs are constrained.
- [x] Target and trusted-evaluator roles use separate invocations and mount modes.
- [x] Secrets are not emitted in argv or evidence.
- [x] Timeout/nonzero/output-limit failures are structured and externally retained.
- [x] Integration test verifies the real pinned image and daemon behavior.

## Deferred Boundary

Build-time dependency networking and provider/platform allowlists require a
controlled proxy and distinct build phase. The present backend intentionally
fails closed instead of treating unrestricted Docker networking as a default.
