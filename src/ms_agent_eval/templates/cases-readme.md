# Case Bank and Coverage Contract

This directory contains the versioned evaluation dataset for this workspace.
It is an experiment input, not generated runtime output.

## Files

```text
cases/
├── README.md
├── coverage.yaml
├── splits.yaml
└── <skill-id>/<case-id>/
    ├── case.yaml
    ├── prompt.md
    ├── expected/response.md
    ├── expected/artifacts/    optional
    └── rubric.yaml
```

`coverage.yaml` is mandatory. It is the complete semantic decomposition of the
exact skills selected in `workspace.yaml`. Keep it `draft` while any selected
skill has not been fully decomposed; mark it `complete` only after all skills
and their independently testable capabilities have been reviewed.

The catalog schema is:

```yaml
schema_version: 1
status: complete
default_minimum_primary_cases: 3
skills:
  exact/skill-id:
    capabilities:
      - id: stable-capability-id
        title: Human-readable behavior
        description: Observable decision or behavior this capability tests.
        # minimum_primary_cases: 4  # optional override
```

Every schema-v4 case has exactly one cataloged primary capability and at most
two supporting capabilities:

```yaml
coverage:
  primary: stable-capability-id
  supporting: []
  archetype: construction
  difficulty: easy
```

Only primary cases count toward capability depth. Supporting tags record
context only. A skill is ready only when every capability reaches its required
primary-case depth and the per-skill case and distinct-leakage-group floors in
`workspace.yaml` are also met.

Requested case counts are lower bounds. Do not satisfy the catalog with
paraphrases: variants that share a source example, fixture, defect, solution,
or expected artifact belong to one leakage group and must remain in one split.

The active Codex session authors cases using the workspace's
`case-authoring.md` and repository-local `case-authoring` skill. Run
`ms-agent-eval inspect --workspace workspace.yaml` to see exact remaining gaps.
No solver call is allowed while the coverage contract is incomplete.

Every promoted case binds its provenance to the exact catalog hash. After any
catalog change, review and rehash affected cases before evaluation.
