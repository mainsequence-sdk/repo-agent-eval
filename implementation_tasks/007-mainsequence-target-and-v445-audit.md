# 007 — Main Sequence Target and v4.4.5 Equivalence Audit

Status: Implemented on 2026-08-19
Priority: P0 / migration proof
Depends on: tasks 004–006
Unblocks: task 008

## Outcome

Main Sequence is now a first-party experiment-pack target rather than a core
framework assumption. The pack declares the public GitHub repository, tag
`v4.4.5`, exact global context, exact `agent_scaffold/skills` root, and a
20-unit inventory assertion. The generic resolver created a real external
snapshot at the public commit and proved every upstream instruction file is
byte-identical to the existing normalized `sdk/4.4.5` copy.

## Configuration

Created:

```text
experiments/mainsequence-sdk/workspace.yaml
experiments/mainsequence-sdk/targets/mainsequence-sdk.yaml
```

The target specifies only data:

```text
repository: https://github.com/mainsequence-sdk/mainsequence-sdk
tag: v4.4.5
global context: agent_scaffold/AGENTS.md
unit root: agent_scaffold/skills
unit filename: SKILL.md
exact count: 20
required ids: all 20 known logical ids
```

No corresponding path, count, repository, or package import was added to
`agent-eval-core`.

## Real Source Lock

Git resolution and exact-commit fetch produced:

```text
resolved commit: 3b5a20a344cec0c960351dc3c601d32a66a8b46e
snapshot id: mainsequence-sdk-3b5a20a344ce-dbeab527cb38
snapshot lock: sha256:fbb65b6b3e6fa1526be6be491acddf8129ebc82b7024635fa6c13c9d2886b221
inventory: 1 global context + 20 instruction units
```

The clone and extracted files remain under the external temporary data root and
are not Git-tracked. The compact generated lock is committed at:

```text
experiments/mainsequence-sdk/snapshots/
  mainsequence-sdk-3b5a20a344ce-dbeab527cb38.json
```

It contains only source identities, paths, sizes, and hashes needed for
planning/audit. Reloading recomputes and verifies its inventory and whole-lock
hash.

## Equivalence Result

Detailed report:

```text
experiments/mainsequence-sdk/docs/v4.4.5-snapshot-equivalence.md
```

Results:

- 20/20 upstream `agent_scaffold/skills/<id>/SKILL.md` files match their
  normalized `sdk/4.4.5/skills/<id>/source/SKILL.md` bytes;
- upstream and normalized `AGENTS.md` match;
- no missing or extra configured instruction units;
- no packaging transformation, local drift, or locator error was detected.

The audit intentionally retains both path namespaces. Matching bytes do not
turn a normalized path into an upstream source identifier.

## Regression Tests

`tests/test_mainsequence_pack.py` asserts:

- repository URL, tag, context path, unit root, and count from target YAML;
- locked commit, lock hash, 20 units, 21 files, and exact upstream path shape;
- SHA-256 equivalence for global context and every current normalized skill.

Latest combined result on CPython 3.12.8:

```text
43 passed, 1 optional Docker test skipped
```

## Acceptance Criteria

- [x] Main Sequence exists as target-pack configuration, not core behavior.
- [x] Exact paths point to `agent_scaffold/AGENTS.md` and `agent_scaffold/skills`.
- [x] Public tag `v4.4.5` resolves to the recorded full commit.
- [x] Generic snapshot builder locks exactly 20 logical unit records.
- [x] Every unit has one upstream path, snapshot path, and content hash.
- [x] Upstream and normalized skill copies are compared byte-for-byte.
- [x] Difference classification is explicit; result is no difference.
- [x] Clone/extracted content and results remain outside Git.
- [x] Regression tests fail on target drift, lock tampering, or normalized drift.
