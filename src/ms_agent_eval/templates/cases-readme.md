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
    ├── solver/
    │   ├── prompt.md
    │   └── attachments/       optional solver-visible fixtures
    └── judge/
        ├── rubric.yaml
        └── expected/
            ├── response.md
            └── artifacts/     optional judge-only evidence
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

Every schema-v6 case has exactly one cataloged primary capability and at most
two supporting capabilities:

```yaml
coverage:
  primary: stable-capability-id
  supporting: []
  archetype: construction
```

Only primary cases count toward capability depth. Supporting tags record
context only. A skill is ready only when every capability reaches its required
primary-case depth and the per-skill case and distinct-leakage-group floors in
`workspace.yaml` are also met.

Do not author subjective difficulty labels. Completed reports classify response
length from the exact hidden expected-response token count.

Requested case counts are lower bounds. Do not satisfy the catalog with
paraphrases: variants that share a source example, fixture, defect, solution,
or expected artifact belong to one leakage group and must remain in one split.

The configured author stage authors cases using the workspace's
`case-authoring.md` and repository-local `case-authoring` skill. The same
campaign must create the sibling `judge-validation/` manifest and candidates;
the case bank is not an independently complete authoring deliverable. Run
`ms-agent-eval inspect --workspace workspace.yaml` to see exact remaining gaps.
No solver call is allowed while the coverage contract is incomplete.

Every reviewed case binds its provenance to the exact catalog hash. After any
catalog change, review and rehash affected cases before evaluation.

The canonical package is coordinator-owned and is never mounted wholesale into
a model runtime. A solver view contains only `solver/` material plus locked
global and skill context. A judge view contains the task, candidate, rubric
criteria, reference evidence, and skill context; judge-validation labels remain
coordinator-only. Rubrics have no pass thresholds.
