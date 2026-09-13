# Capability Coverage Contract

Every MS Agent Eval workspace has a mandatory semantic coverage contract at
`cases/coverage.yaml`. It defines what the selected skills contain and how many
independent primary cases must test each capability before the experiment may
run.

This is part of the general experiment blueprint. It is not specific to the
MainSequence SDK example, and it is not a generated report. The catalog is a
versioned, human-reviewed experiment input.

## Why case counts are not enough

“Twelve cases for a skill” does not prove that the skill is covered. Twelve
prompts can repeat the same behavior while ignoring most of the skill. Coverage
therefore has three separate gates:

1. **Capability breadth** — every selected skill appears in the catalog and is
   decomposed into all independently testable behaviors.
2. **Capability depth** — every capability has enough materially distinct
   primary cases. The default is three.
3. **Corpus diversity** — every skill meets the workspace-level minimum number
   of reviewed cases and distinct leakage groups.

All three gates must pass. Supporting capability tags, paraphrases, or a large
total case count cannot compensate for a missing primary capability.

## The three connected records

### 1. Workspace readiness floors

`workspace.yaml` points to the catalog and declares additional per-skill
floors:

```yaml
evaluation:
  cases:
    directory: cases
    coverage:
      catalog: cases/coverage.yaml
      minimum_cases_per_skill: 8
      minimum_leakage_groups_per_skill: 4
```

These numbers are lower bounds for corpus size and diversity. They do not list
the capabilities and do not prove semantic completeness.

### 2. Capability catalog

`cases/coverage.yaml` owns the semantic decomposition:

```yaml
schema_version: 1
status: complete
default_minimum_primary_cases: 3
skills:
  data_publishing/time_index_table_updates:
    capabilities:
      - id: updater-storage-separation
        title: Separate updater execution from storage
        description: >-
          Keep update execution behavior distinct from the persisted table's
          schema, index, and identity.
      - id: multidimensional-index-progress
        title: Track multidimensional progress
        description: >-
          Compute read and update progress for every index dimension without
          collapsing entity-specific state.
        minimum_primary_cases: 4
```

The fields mean:

| Field | Contract |
| --- | --- |
| `schema_version` | Must be `1`. Other shapes are rejected. |
| `status` | `draft` while decomposition is incomplete; `complete` only after every selected skill has been fully reviewed. |
| `default_minimum_primary_cases` | Positive integer applied to capabilities without an override. New workspaces default to `3`. |
| `skills` | Mapping keyed by the exact configured skill id discovered from the immutable target snapshot. |
| `capabilities` | Independently testable decisions or behaviors owned by that skill. A complete skill must have at least one. |
| `id` | Stable lowercase identifier, unique within its skill. |
| `title` | Short human-readable name. |
| `description` | Observable behavior and decision boundary the cases must distinguish. |
| `minimum_primary_cases` | Optional positive per-capability depth override. |

`status: complete` is a reviewed assertion about the decomposition, not an
automatic consequence of adding entries. Validation rejects unknown skill ids,
duplicate capability ids, empty capabilities in a complete catalog, and cases
that cite capabilities outside their exact skill.

### 3. Per-case coverage classification

Every schema-v5 `case.yaml` connects one case to the catalog:

```yaml
coverage:
  primary: multidimensional-index-progress
  supporting:
    - updater-storage-separation
  archetype: diagnosis
  difficulty: complex
```

Each case has exactly one primary capability and no more than two supporting
capabilities. Only the primary capability counts toward required depth.
Supporting entries record relevant context; they never earn readiness credit.

Allowed archetypes are `construction`, `review`, `diagnosis`, `migration`, and
`evidence`. Difficulty is `easy` or `complex`. These classifications help
authors diversify the corpus but do not replace capability coverage.

## How to build the catalog

For every exact selected skill:

1. Read the complete locked `SKILL.md` and every supporting file it requires.
2. Extract independently testable units from its purpose, required decisions,
   numbered rules, prohibitions, review checks, stop conditions, and referenced
   contracts.
3. Merge only true semantic duplicates. Do not use one broad capability such
   as `understands-the-skill` to hide several independent behaviors.
4. Give every capability a stable id and a description of observable success.
5. Choose the primary-case depth. Use the default three unless genuinely
   independent contexts, risks, or failure modes require more.
6. Confirm that every configured skill is present, then change the catalog from
   `draft` to `complete`.

A request such as “create one case per skill” is a campaign lower bound, not
permission to under-cover the catalog. The authoring campaign continues until
every capability reaches its primary-case requirement and the per-skill
case/group floors are also met.

## What counts as materially distinct

Primary cases for the same capability must exercise different decision
boundaries, evidence, failure modes, deliverables, or architectural contexts.
Changing names, prose, or sample numbers is not a new semantic example.

Cases derived from the same source example, defect, fixture, solution, or
expected artifact share one leakage group. Leakage groups are assigned to
train, test, or challenge as a unit, preventing near-duplicate
knowledge from crossing protected splits.

## Readiness calculation

For every selected skill `s` and capability `c`, the framework calculates:

```text
primary_count(s, c) = number of cases for s whose coverage.primary is c
missing(s, c) = max(0, required_primary_cases(c) - primary_count(s, c))
```

The case bank is valid only when:

```text
catalog.status == complete
AND every configured skill exists in the catalog
AND every missing(s, c) == 0
AND cases(s) >= minimum_cases_per_skill
AND distinct_leakage_groups(s) >= minimum_leakage_groups_per_skill
```

This case-bank gate is necessary but not sufficient for a scored run. Immutable
source resolution, case provenance, split integrity, distinct solver/judge
identities, and judge validation must also pass.

Run:

```bash
ms-agent-eval inspect --workspace path/to/workspace.yaml
ms-agent-eval validate --workspace path/to/workspace.yaml
```

The report exposes catalog status and hash, missing skills, total and satisfied
capabilities, missing primary-case count, per-skill case/group counts, and every
capability gap. An incomplete result during authoring is expected and prevents
solver calls.

## Provenance and catalog changes

Every reviewed case stores the SHA-256 hash of the exact capability catalog in
`provenance.capability_catalog_hash`. Changing a capability id, description,
depth, or any other catalog byte changes that hash. Existing cases must then be
reviewed, reclassified when necessary, and rehashed before they can be used.

This intentionally prevents a case authored against an older definition of
coverage from silently passing under a changed experiment contract.

## Ownership and storage

Version these files with the experiment:

```text
workspace.yaml
case-authoring.md
cases/README.md
cases/coverage.yaml
cases/splits.yaml
cases/<skill>/<case>/...
judge-validation/...
```

Do not commit repository snapshots, model calls, optimizer state, runs,
reports, or exported fine-tuning datasets. Those remain under the external
workspace data root.
