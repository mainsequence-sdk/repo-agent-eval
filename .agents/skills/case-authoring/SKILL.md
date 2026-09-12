---
name: case-authoring
description: Use when creating, updating, reviewing, or planning evaluation cases in MS Agent Eval experiment workspaces, including case.yaml, prompts, expected responses, rubrics, case-bank metadata, and cases grounded in immutable repository snapshots or training-source documents.
---

# Case Authoring

## Purpose

Use the current Codex session as the executor of the configured `author` stage.
Read the versioned experiment policy and immutable repository evidence, then
create reviewed case packages and the judge-calibration corpus directly in the
experiment workspace. An endpoint executor uses the typed DSPy author program;
a Codex harness consumes the same portable packet and result contract.

The experiment has three model-driven roles: author, solver, and judge. Keep
their stage inputs and all evaluation results isolated. Solver and judge may use
the same configured model without collapsing their roles.

## Read First

Read the workspace's `workspace.yaml`, its configured
`evaluation.case_authoring.instructions` file, `README.md`,
`docs/getting-started.md`, `docs/structure.md`, and `docs/conventions.md`.
Read the configured `evaluation.cases.coverage.catalog` before selecting any
case. A draft or incomplete catalog is an authoring blocker, not permission to
sample an arbitrary subset of a skill.
Read the existing `evaluation.judge.calibration.directory`, its
`manifest.yaml`, and every referenced candidate before authoring or replacing
calibration fixtures.
Resolve the configured target and every `evaluation.case_authoring.sources`
repository to immutable external snapshots before considering case content.

The skill paths are exactly the `evaluation.instructions.skills` selection in
the manifest. Never guess `.agents/skills`, `agent_scaffold/skills`, or another
location. If `directory` is configured, use every recursively discovered
`SKILL.md`. If `files` is configured, use exactly those files.

## Non-negotiable Rules

- The active Codex session authors the cases as the configured author-stage
  harness. Do not silently replace its locked model or executor identity.
- A case-authoring campaign is incomplete until Codex creates or updates the
  configured judge-calibration manifest and candidate files. Do not return
  after creating cases and leave calibration as a later manual task.
- Treat the configured authoring file as durable policy and the user's request
  as the narrower campaign objective.
- Treat every requested case count as a lower bound. Author at least three
  materially distinct cases per selected skill and expand further whenever the
  capability catalog has unmet primary-coverage requirements.
- Before authoring, decompose each complete selected skill into capability
  units from its purpose, required decisions, numbered build rules,
  prohibitions, review rules, stop conditions, and required supporting docs.
  Record those units in the capability catalog; never infer breadth from case
  count alone.
- Give each case with exactly one cataloged primary capability and no more than
  two supporting capabilities. A capability is covered only by primary cases;
  supporting tags provide context but not readiness credit.
- Ground cases in the immutable target/reference source set, not an installed
  package, mutable checkout, branch, or model memory.
- Preserve reference labels exactly as
  `references/<source-id>/<repository-path>` and bind provenance to the combined
  authoring source-set hash.
- Never use solver responses, judge votes, optimizer traces, scores, or held-out
  feedback during authoring.
- Never send a rubric or expected response to the solver.
- When a case contains a grounded faulty test, false premise, broken
  configuration, contradiction, or misleading symptom, require an
  `error_identification` criterion weighted at 0.35–0.50. Do not add it when no
  real defect is present.
- Do not add evaluator methods, evaluator status fields, Python judge plugins,
  keyword checks, or checklist scoring code. The DSPy judge LLM is the only
  semantic judge.
- Assign leakage groups semantically. Apply the configured deterministic split
  policy after cases exist; never choose or change a group to obtain a desired
  train/development/test/challenge placement.
- Do not copy source repositories, model calls, or run results into Git.

## Workflow

### 1. Inspect the locked workspace

Run:

```bash
ms-agent-eval inspect --workspace path/to/workspace.yaml
```

Confirm every source URL/ref/commit/snapshot, the combined source-set hash,
global instructions, exact skill ids and paths, case coverage, split groups,
solver identity, judge identity, and calibration corpus. An incomplete status
caused only by missing cases or calibration is expected while authoring.

### 2. Build or audit the capability catalog

Read every selected skill completely. Create one stable capability id for each
independently testable decision or behavior, merge only true semantic
duplicates, and set `minimum_primary_cases` to the number of independent
examples required. The default is three. Mark the catalog `complete` only when
every selected skill has been decomposed. Validation must report all remaining
capability gaps explicitly.

### 3. Read exact evidence

For each requested skill, read the complete locked `SKILL.md`, then the smallest
set of target/reference files needed for the selected capability. Treat target
instructions as normative. References provide scenarios and concrete examples;
they do not redefine the evaluated SDK.

### 4. Author atomic packages

Create this shape directly:

```text
cases/<skill-id>/<case-id>/
├── case.yaml
├── solver/
│   ├── prompt.md
│   └── attachments/     optional solver-visible fixtures
└── judge/
    ├── rubric.yaml
    └── expected/
        ├── response.md
        └── artifacts/   optional judge-only evidence
```

The prompt must be self-contained for a response-only solver unless the
workspace explicitly selects an isolated execution runtime. The expected
response is judge reference evidence, not an exact-string oracle. The rubric
defines semantic quality and all weights sum to one.

Use case schema version 5. The `solver/` namespace is the complete public case
surface. The solver must never receive `case.yaml` or any file under `judge/`.
The coordinator materializes these namespaces into separate stage views; never
mount the canonical case package into a model container. Classify the case
explicitly:

```yaml
schema_version: 5
solver:
  prompt: solver/prompt.md
  # attachments: solver/attachments
judge:
  rubric: judge/rubric.yaml
  expected_response: judge/expected/response.md
  # expected_artifacts: judge/expected/artifacts
coverage:
  primary: one-capability-id
  supporting: [optional-capability-id]
  archetype: construction
  difficulty: easy
```

Allowed archetypes are `construction`, `review`, `diagnosis`, `migration`, and
`evidence`; difficulty is `easy` or `complex`. Provenance is:

```yaml
provenance:
  authoring_agent: codex
  authoring_instructions_hash: sha256:...
  capability_catalog_hash: sha256:...
  source_snapshot_hash: sha256:...
  case_content_hash: sha256:...
```

Compute `authoring_instructions_hash` from the exact configured authoring file,
use the inspected combined source-set hash, and compute `case_content_hash` from
the framework's case identity. It is acceptable to write `pending` while
calculating the final package hash, but never leave a promoted case pending.

### 5. Update grouped splits mechanically

For each new leakage group, use the workspace split seed and proportions to
derive its split with the framework's SHA-256 mapping. Preserve existing group
assignments. Related variants share one group and therefore one split.

### 6. Create or update judge calibration

After case packages and grouped splits exist, create a versioned calibration
corpus in the configured `evaluation.judge.calibration.directory`. This is a
mandatory authoring deliverable, not generated run output and not a task for the
runtime judge.

Write `manifest.yaml` with `schema_version: 2` plus candidate Markdown files.
Create at least one fixture for every required label: `strong`, `partial`,
`incorrect`, `contradictory`, and `adversarial`. Every fixture must have a
unique id, reference an existing promoted case, name a relative candidate file,
and declare a score range within `[0, 1]`:

```yaml
schema_version: 2
fixtures:
  - id: strong-example
    case: representative-case-id
    candidate: strong.md
    label: strong
    score_range: [0.9, 1.0]
```

Write controlled candidate responses that exercise the actual rubric and hard
failures of their referenced cases. `strong` must pass; all other labels must
fail. `partial` must deserve a score strictly below every strong fixture but
strictly above every `incorrect`, `contradictory`, and `adversarial` fixture.
The adversarial candidate must contain a realistic attempt to manipulate or
override the judge without exposing secrets or executable instructions. Choose
non-overlapping score ranges that permit this strict ordering.

Use only train/development cases for calibration whenever the workspace has an
optimization experiment; never reference test/challenge cases. Do not use
actual solver responses, judge outputs, or held-out feedback as fixtures.
Calibration files are reviewed experiment inputs and belong in Git; calibration
execution outputs belong only in external run storage. If a referenced case or
rubric changes, review and update its fixtures. If no eligible representative
case exists, author and split one rather than weakening this requirement.

### 7. Validate the complete contract

```bash
ms-agent-eval validate --workspace path/to/workspace.yaml
```

Validation must prove that every case skill exists, every source path is locked,
every group owns exactly one split, every expected result and rubric exists,
weights sum to one, provenance matches current package bytes and authoring
policy/catalog, every required capability has enough primary cases, solver and
judge identities differ, and judge calibration contains valid `strong`,
`partial`, `incorrect`, `contradictory`, and `adversarial` reference-labelled
fixtures. Do not finish the authoring campaign while validation reports a
missing calibration label, unknown case, unsafe candidate path, invalid score
range, or held-out split reference.

## Dataset Governance

Optimization sees train and development only. A compiled state-only JSON
artifact is published before the untouched test set is loaded. Never use test or
challenge results to revise cases, prompts, demonstrations, or optimizer
settings.

Examples from pinned projects may seed derivative synthetic cases only when
their provenance and shared leakage group are retained. Distillation and
fine-tuning exports use reviewed train/development cases only, remain outside
Git, and never contain held-out prompts, answers, responses, or judge feedback.

## Final Response

Report the workspace, resolved source commits and source-set hash, authored case
ids by skill, exact source paths, leakage groups and assigned splits,
calibration fixture ids with labels and referenced case splits, validation
performed, remaining coverage gaps, and any case or fixture rejected during
self-review.
