# 004 — Safe GitHub Resolver, Exact Locators, and Immutable Snapshots

Status: Implemented on 2026-08-19
Priority: P0 / source integrity
Depends on: tasks 002–003
Unblocks: tasks 005–009

## Outcome

The framework can resolve a configured GitHub tag or full commit, materialize
the exact commit without hooks/submodules/LFS, select instruction material only
through declared roots or entries, and publish a content-addressed snapshot
outside the Git workspace. A published snapshot can be verified later without
network access or the original checkout.

## Source Resolution

The initial `GitHubSourceProvider` accepts only credential-free HTTPS
`github.com/<owner>/<repository>` URLs and persisted refs of type `tag` or
`commit`.

- Commits must be full lowercase 40-character SHAs.
- Tag names are validated before reaching Git and reject option-like names,
  traversal-like `..`, reflog syntax, lock suffixes, control/invalid ref
  characters, and empty path components.
- `git ls-remote --tags` receives arguments as an argv list, never a shell
  string.
- Annotated tags use their peeled `^{}` commit; lightweight tags use the direct
  commit.
- Git runs with terminal prompting and system/global configuration disabled and
  with hooks redirected to the null path.
- Initial safe mode rejects submodules and Git LFS.
- Materialization fetches the exact resolved SHA with no tags, checks out a
  detached `FETCH_HEAD`, and verifies `HEAD` equals the lock.

## Exact Instruction Locators

Snapshot extraction supports:

- ordered exact global-context paths;
- explicitly listed unit files;
- directory locators with configured root, filename, recursion, includes,
  excludes, symlink policy, and logical-id prefix;
- exact-count and required-logical-id assertions;
- multiple roots with deterministic prefixes.

Hidden directories have no special behavior. The synthetic proof selects
`.agents/skills` only because the target names it, while an unconfigured second
skills directory is ignored. Two roots producing the same `(bundle_id,
unit_id)` fail; prefixes make the same roots valid.

Absolute/traversing configuration paths are rejected by the schema. At
extraction time every path component is checked for symlinks, the resolved file
must remain below the checkout, roots must have the configured type, and empty
matches fail unless explicitly allowed.

## Immutable External Snapshot

Every selected file records:

- upstream repository-relative source path;
- immutable snapshot-relative path;
- byte length and SHA-256 hash.

Every instruction unit additionally binds its bundle, source locator, and
logical unit id. The lock records target specification hash, extraction
configuration hash, complete inventory hash, requested ref, canonical URL, and
resolved commit. Snapshot ids are deterministic from target, commit, and
extraction hash; storage directories are addressed by the complete lock hash.

Publication stages data beneath `${MS_AGENT_EVAL_DATA_ROOT}/tmp`, writes canonical
JSON, atomically renames into `${MS_AGENT_EVAL_DATA_ROOT}/snapshots/<lock-hash>`,
and verifies the lock, exact file inventory, file sizes, and hashes. Existing
identical snapshots are verified and reused, never refreshed in place.

The store rejects a data root inside the configuration workspace when the
workspace boundary is supplied.

## CLI

```text
agent-eval target resolve <target-id> --workspace <workspace.yaml>
agent-eval target snapshot <target-id> --workspace <workspace.yaml>
  --data-root <external-directory>
agent-eval snapshot verify --lock <snapshot.lock.json>
  --data-root <external-directory>
```

## Verification Evidence

On CPython 3.12.8:

```text
ruff check packages/agent-eval-core
All checks passed!

pytest -q packages/agent-eval-core/tests
26 passed
```

Coverage includes annotated/lightweight resolution logic, unsafe tags,
configuration-only hidden-root selection, unconfigured-root exclusion,
multi-root collision and prefix behavior, inventory assertions, symlink
rejection, external-root policy, offline verification after checkout deletion,
and tamper detection.

## Acceptance Criteria

- [x] GitHub tags resolve to exact commits with annotated tags dereferenced.
- [x] Persisted commits are full immutable SHAs.
- [x] Git uses argv execution and cannot receive unsafe tag options.
- [x] No hook, submodule, or LFS execution occurs.
- [x] Unit discovery is scoped to explicitly configured roots/entries.
- [x] `.agents/skills` works only when configured.
- [x] Multiple roots require collision-free logical ids.
- [x] Snapshot contents and locks live outside the Git workspace.
- [x] Publication is immutable, atomic, content-addressed, and idempotent.
- [x] Verification is network-independent and detects byte tampering.

## Task 007 Boundary

This task implements the generic mechanism. Task 007 supplies the first-party
Main Sequence target, resolves public tag `v4.4.5`, records its real commit and
20-unit inventory, and performs the required upstream-versus-current snapshot
byte-equivalence audit. No Main Sequence path or count is present in core.
