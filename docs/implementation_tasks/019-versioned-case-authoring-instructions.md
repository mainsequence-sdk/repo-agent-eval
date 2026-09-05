# 019 — Versioned Case-Authoring Instructions

Status: Complete

## Problem

The case-builder previously had only a short Python docstring plus a dynamic
`--coverage` input. That was not a sufficient experiment contract. It left the
most important authoring decisions implicit: evidence authority, grounding,
task quality, reference-answer construction, rubric design for the LLM judge,
duplicate avoidance, leakage grouping, and the typed output shape.

Putting all of those rules in CLI help or framework code would make them hard to
review and impossible to vary per experiment. Treating `--coverage` as the full
prompt would also mix two different concerns:

- durable policy describing how cases must be authored; and
- a one-call request describing which behavior or gap to cover.

## Decision

Every schema-v4 workspace must configure a versioned Markdown policy:

```yaml
schema_version: 4

evaluation:
  case_builder:
    instructions: case-authoring.md
```

The referenced file is a required, non-empty, regular file inside the workspace.
Its exact decoded content replaces the static DSPy `CaseBuilder` signature
instructions. The existing typed inputs remain unchanged:

```text
global_context
skill_context
source_context
coverage_request
existing_case_summaries
```

This preserves the separation between permanent policy and dynamic coverage.
The policy is not sent as an ordinary input field and there is no raw-prompt or
legacy execution path.

## Authoring policy contents

The generated policy documents:

1. the case-builder role and its five inputs;
2. the evidence hierarchy and immutable-snapshot boundary;
3. prohibitions on mutable, external, held-out, judge, and optimizer evidence;
4. realistic case design and duplicate avoidance;
5. solver-facing prompt construction;
6. expert reference responses and optional expected artifacts;
7. weighted rubric and hard-failure design for the LLM judge only;
8. source-path grounding and leakage-group semantics;
9. the exact seven-field typed DSPy output contract; and
10. a final self-review checklist.

The policy explicitly rejects deterministic semantic judges, keyword checks,
regex/AST scoring, evaluator plugins, and exact-byte comparison except when
exact bytes are themselves a documented task requirement.

## Identity and provenance

`program_state()` already records the instructions of every DSPy predictor.
Therefore applying the Markdown content through `Signature.with_instructions()`
causes the existing `program_hash()` to cover the complete authoring policy.
That hash flows into:

- the external builder request and trace;
- each validated draft manifest;
- promoted `case.yaml` builder provenance;
- experiment locks; and
- validation and inspection reports.

Editing `case-authoring.md` consequently changes the active builder program
hash. Draft promotion and scored workspace compilation reject cases created by
the previous policy. Cases must be rebuilt and explicitly promoted. There is no
compatibility shim or fallback default for existing manifests.

## Workspace generation

Both `ms-agent-eval setup` and `ms-agent-eval init` create:

```text
<workspace>/
├── workspace.yaml
├── case-authoring.md
├── cases/
│   └── splits.yaml
└── judge-calibration/
    └── manifest.yaml
```

The authoring template is shipped inside the `ms-agent-eval` package, so the
CLI works after wheel installation. Setup includes the file in its conflict
check and review list and refuses to overwrite it.

The MainSequence example contains a real versioned copy at
`experiments/mainsequence-sdk/case-authoring.md`; it does not hardcode any
MainSequence behavior. Repository-specific evidence continues to come only from
the configured immutable snapshot.

## Implementation

- Bump the workspace manifest to schema v4.
- Add required `evaluation.case_builder.instructions` parsing.
- Resolve and validate the authoring path through `WorkspaceRepository`.
- Require `create_case_builder_program(instructions)` and remove the no-argument
  construction path.
- Load the policy for building, promotion checks, workspace inspection, and
  experiment locking.
- Generate the policy file from both CLI workspace creation paths.
- Report the resolved policy path beside the builder program hash.
- Update the bundled case-authoring and MainSequence versioning skills.
- Update the README, getting-started guide, structure, conventions, example, and
  documentation index.

## Acceptance criteria

- A manifest without `case_builder.instructions` is rejected.
- Absolute or escaping instruction paths are rejected by schema parsing.
- Missing, symlinked, or empty instruction files are rejected before builder or
  experiment execution.
- Changing only the policy content changes the DSPy builder program hash.
- Builder drafts record the program hash derived from that policy.
- Promotion and experiment locking reject drafts/cases built with stale policy.
- `init` and guided `setup` create the policy and never partially write it after
  cancellation.
- The policy asset is included in the built wheel.
- The MainSequence example and all active documentation use schema v4.
- Ruff, pytest, lock verification, package build, and both edited skill
  validators pass.

## No compatibility layer

Schema v3 is intentionally unsupported after this change. Users must regenerate
or explicitly migrate a workspace by adding a reviewed `case-authoring.md` and
updating the manifest to schema v4. Silently injecting a framework default into
an older experiment would make its builder provenance misleading.

## Completion evidence

- Ruff passed for `src` and `tests`.
- All 42 tests passed, including missing, symlinked, empty, non-UTF-8,
  byte-change, stale-provenance, CLI-init, and guided-setup coverage.
- `uv lock --check` passed.
- The source distribution and wheel built successfully.
- A wheel-level smoke test loaded the packaged Markdown policy and generated a
  schema-v4 template.
- The case-authoring and MainSequence experiment-version skills both passed the
  skill validator.
