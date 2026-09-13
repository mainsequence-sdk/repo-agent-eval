# 008 — Namespaced Suites, Compatibility, Splits, and Case Migration

Status: Implemented on 2026-08-19
Priority: P0 / data migration
Depends on: task 007
Unblocks: tasks 009–011

## Outcome

The legacy v1 and v2 case banks now have byte-identical copies inside the
`mainsequence-agent-skills` experiment pack, while their authored source
locations remain untouched. Generated neutral suite catalogs replace implicit
global case discovery. Immutable compatibility maps bind every case to an exact
locked `(bundle_id, unit_id)`, and immutable grouped split manifests provide an
explicit benchmark/optimization data boundary.

The migration copied the user's current working-tree case bytes, including
uncommitted authored additions. It did not rewrite, normalize, or delete the
legacy case bank.

## Namespaced Layout

```text
experiments/mainsequence-sdk/suites/
├── mainsequence-agent-skills-v1.yaml
├── mainsequence-agent-skills-v2.yaml
├── v1/                         # byte-identical legacy copy
├── v2/                         # byte-identical legacy copy
└── training_sources/           # byte-identical target-specific references
```

Generated control documents live in:

```text
compatibility/<snapshot-id>--<suite-id>.yaml
splits/<suite-id>-split-v1.json
```

The suite catalog is the only resolution index. Each case reference contains a
namespaced case directory plus logical `bundle_id` and `unit_id`; it cannot
substitute an arbitrary target-repository path.

## Mechanical Migration Evidence

| Suite | Cases | Files copied | Copy result |
|---|---:|---:|---|
| v1 | 55 | 252 | every relative path and SHA-256 identical |
| v2 | 74 | 338 | every relative path and SHA-256 identical |
| training sources | — | 2 | every relative path and SHA-256 identical |

The committed migration tool refuses to overwrite an existing namespaced copy
when its full relative-path/hash tree differs from the legacy source. This
forces review before refreshing a migrated suite and protects authored work.

## Compatibility Maps

Both suites bind to snapshot:

```text
mainsequence-sdk-3b5a20a344ce-dbeab527cb38
```

Every compatibility entry maps one case to `agent-scaffold` and one of the 20
unit ids in the verified source lock. Validation proves that:

- suite, compatibility, and split case-id sets are identical;
- every mapped `(bundle_id, unit_id)` exists exactly in the snapshot;
- compatibility cannot change the unit declared by the suite;
- suite version equals the compatibility version;
- no global case-id search or fallback path is used.

Generated compatibility hashes:

```text
v1 sha256:f61bf9aa13d451f66d2508ee401cfa5a54d29b7d164aa4cfc4debb85688df14d
v2 sha256:c904e348dc95c53d75e28d4b9254ae21157eb5d77930bc004ce8430ee1d6fcd5
```

## Grouped Split Policy

Cases for the same instruction unit are one indivisible group. Group ids are
non-semantic SHA-derived identifiers, so target unit paths do not become
filesystem inputs. A deterministic exhaustive assignment balances case counts
while never splitting a unit across train/development/test.

v2 is eligible for governed optimization:

| Split | Cases |
|---|---:|
| train | 52 |
| development | 11 |
| test | 11 |

Split hash:

```text
sha256:4aa19f09a5c341282b89d211f02d446e6c2c1648f8ab2f07527a906c7ddfe40e
```

The 11 test cases remain unavailable to optimization services and are loaded
only after a compiled artifact exists.

v1 has only two represented instruction units, so it cannot form three honest
unit-isolated partitions. Its immutable split is 50 train / 5 test and has no
development set. v1 is therefore benchmark/migration-history data and is not
eligible for DSPy optimization.

## Verification Evidence

On CPython 3.12.8:

```text
agent-eval config validate --workspace .../mainsequence-agent-skills/workspace.yaml
compatibility: 2
snapshots: 1
splits: 2
suites: 2
status: valid

ruff check packages/agent-eval-core tests experiments/.../tools
All checks passed!

pytest -q packages/agent-eval-core/tests tests
46 passed, 1 optional Docker test skipped
```

## Acceptance Criteria

- [x] Legacy v1/v2 data remains in place and unmodified by migration.
- [x] Namespaced copies are byte-identical and file-count identical.
- [x] Training-source copies are byte-identical.
- [x] Suite catalogs provide one exact location per case.
- [x] Compatibility covers every case and resolves only locked logical units.
- [x] No case can inject a target repository path during resolution.
- [x] Split documents are immutable and group-leakage validation is enforced.
- [x] v2 has distinct train/development/test data with unit-level isolation.
- [x] v1 is explicitly excluded from optimization due to missing honest dev data.
- [x] Migration is deterministic and refuses silent overwrite drift.
